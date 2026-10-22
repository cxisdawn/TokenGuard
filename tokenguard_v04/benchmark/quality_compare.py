"""Read-only, cohort-separated quality scorecard. No agents or provider calls."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import statistics
import sys
import uuid

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from quality_analysis import analyze
from tokenguard.experiment import load_experiment
from tokenguard.quality_protocol import MODES, file_sha, read_bytes
from tokenguard.quality_seed import checked_preflight, inside
from tokenguard.visible_tests import canonical_path

USAGE_FIELDS = ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens', 'output_tokens')
OUTCOMES = ('baseline_only_resolved', 'optimized_only_resolved', 'both_resolved', 'neither_resolved',
            'ungraded_or_incomplete')


def encoded(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'))


def cohort(record: dict, plan: dict) -> dict:
    """Conservative grouping: identical labels alone are insufficient."""
    task, config = record['task'], record['config']
    return {'task_id': task['task_id'], 'instance_id': plan['instance_id'], 'provider': task['provider'],
            'requested_model': task['model'], 'protocol': plan.get('protocol', 'agent_driven'),
            'protocol_declared': 'protocol' in plan,
            'max_turns': task['max_turns'], 'timeout_seconds': task['timeout_seconds'],
            'baseline_mode': config.get('baseline_mode', 'unspecified'),
            'prompt_sha256': record['prompt_sha256'], 'initial_snapshot_sha256': record['initial_snapshot_sha256'],
            'config_sha256': record['config_sha256'], 'tools_sha256': config['tools_sha256'],
            'image_id': plan.get('image_id', 'unavailable'),
            'instance_sha256': plan.get('instance_sha256', 'unavailable'),
            'harness_commit': plan.get('harness_commit', 'unavailable'),
            'compatibility_dependency': plan.get('compatibility_dependency')}


def valid_number(value: object) -> bool:
    return type(value) in (int, float) and value >= 0 and value < float('inf')


def measurement(values: list, planned: int) -> dict:
    known = [v for v in values if valid_number(v)]
    return {'known': len(known), 'planned': planned, 'complete': len(known) == planned,
            'sum': sum(known) if known else None, 'median': statistics.median(known) if known else None}


def outcome(baseline: dict, optimized: dict) -> str:
    if any(r.get('state') != 'completed' or r.get('evaluation_valid') is not True or
           r.get('only_allowed_changes') is not True for r in (baseline, optimized)):
        return 'ungraded_or_incomplete'
    b, o = baseline.get('code_resolved') is True, optimized.get('code_resolved') is True
    return ('both_resolved' if o else 'baseline_only_resolved') if b else (
        'optimized_only_resolved' if o else 'neither_resolved')


def arm_summary(runs: list[dict], arm: str) -> dict:
    own = [r for r in runs if r['arm'] == arm]
    done = [r for r in own if r['state'] == 'completed']
    graded = [r for r in done if r.get('evaluation_valid') is True and r.get('only_allowed_changes') is True]
    return {'planned': len(own), 'recorded': len(done), 'valid_grades': len(graded),
            'code_resolved': sum(r.get('code_resolved') is True for r in graded),
            'strict_task_success': sum(r.get('task_completed_and_valid') is True for r in graded),
            'not_run': sum(r['state'] != 'completed' for r in own),
            'blocked_before_launch': sum(r['state'] == 'blocked_before_launch' for r in own),
            'infrastructure_failed': sum(bool(r.get('stage_errors')) for r in done),
            'max_turns_exhausted': sum(r.get('agent_end_reason') == 'error_max_turns' for r in done),
            'native_compressions': measurement([r.get('compressions') if r.get('compression_data_available') else None
                                                for r in own], len(own)),
            'native_recoveries': measurement([r.get('recovery_count') if r.get('compression_data_available') else None
                                              for r in own], len(own)),
            'manual_seed_compressions': measurement([r.get('seed_compressions') for r in own], len(own)),
            'manual_seed_recoveries': measurement([r.get('seed_recoveries') for r in own], len(own)),
            'agent_elapsed_ms': measurement([r.get('agent_elapsed_ms') for r in own], len(own)),
            'tool_calls': measurement([r.get('tool_calls') for r in own], len(own)),
            'cli_usage_UNVERIFIED': {key: measurement([
                r.get('cli_aggregate_usage_unverified', {}).get(key) for r in own], len(own)) for key in USAGE_FIELDS}}


def scorecard(experiments: list[dict]) -> dict:
    """Summarize validated input records without pooling incompatible cohorts."""
    groups = {}
    for item in experiments:
        meta = cohort(item['record'], item['plan'])
        analysis = item['analysis']
        meta['controller_observations_sha256'] = (sorted({r['seed_original_stdout_sha256'] for r in analysis['runs']
            if isinstance(r.get('seed_original_stdout_sha256'), str)})
            if meta['protocol'] == 'controlled_initial_log' else None)
        key = encoded(meta)
        group = groups.setdefault(key, {'metadata': meta, 'experiment_ids': [], 'runs': [], 'pairs': []})
        group['experiment_ids'].append(item['record']['experiment_id'])
        for run in analysis['runs']:
            group['runs'].append({**run, 'experiment_id': item['record']['experiment_id']})
        for pair in analysis['pairs']:
            own = [r for r in analysis['runs'] if r['pair_id'] == pair['pair_id']]
            arms = {r['arm']: r for r in own}
            state = outcome(arms.get('baseline', {}), arms.get('optimized', {}))
            reasons = list(pair['reasons'])
            if state == 'ungraded_or_incomplete' and 'independent_code_outcome_unavailable' not in reasons:
                reasons.append('independent_code_outcome_unavailable')
            if meta['protocol'] == 'controlled_initial_log':
                if item.get('seed_inputs_verified') is not True:
                    reasons.append('controller_seed_input_evidence_unavailable')
                origins = [arms.get(a, {}).get('seed_original_stdout_sha256') for a in ('baseline', 'optimized')]
                if not origins[0] or origins[0] != origins[1]:
                    reasons.append('controlled_initial_observation_unknown_or_mismatched')
            elif arms.get('optimized', {}).get('native_compressions_in_matching_bash_results', 0) < 1:
                reasons.append('native_transcript_compression_not_observed')
            eligible = pair['comparison_evidence_satisfied'] is True and not reasons
            b, o = arms.get('baseline', {}), arms.get('optimized', {})
            times = (b.get('agent_elapsed_ms'), o.get('agent_elapsed_ms'))
            group['pairs'].append({
                'experiment_id': item['record']['experiment_id'], 'pair_id': pair['pair_id'],
                'observed_code_outcome': state, 'compression_exposed': pair['compression_exposed'],
                'compression_comparison_eligible': eligible, 'missing_evidence': reasons,
                'baseline_state': b.get('state', 'not_run'), 'optimized_state': o.get('state', 'not_run'),
                'baseline_strict_success': b.get('task_completed_and_valid'),
                'optimized_strict_success': o.get('task_completed_and_valid'),
                'baseline_end_reason': b.get('agent_end_reason'), 'optimized_end_reason': o.get('agent_end_reason'),
                'agent_elapsed_delta_ms_optimized_minus_baseline': times[1] - times[0]
                if all(valid_number(v) for v in times) else None})
    results = []
    ordered = sorted(groups.items(), key=lambda pair: (
        pair[1]['metadata']['task_id'], pair[1]['metadata']['provider'],
        pair[1]['metadata']['requested_model'], pair[1]['metadata']['protocol'],
        pair[1]['metadata']['max_turns'], pair[0]))
    for key, group in ordered:
        pairs = group['pairs']
        all_counts = Counter(p['observed_code_outcome'] for p in pairs)
        eligible = [p for p in pairs if p['compression_comparison_eligible']]
        subset = Counter(p['observed_code_outcome'] for p in eligible)
        reasons = Counter(reason for p in pairs if not p['compression_comparison_eligible']
                          for reason in set(p['missing_evidence']))
        results.append({'cohort_id': hashlib.sha256(key.encode()).hexdigest()[:16], **group['metadata'],
                        'experiment_ids': group['experiment_ids'], 'planned_pairs': len(pairs),
                        'all_planned_code_outcomes': {k: all_counts[k] for k in OUTCOMES},
                        'compression_evidence_subset': {'pairs': len(eligible), **{k: subset[k] for k in OUTCOMES}},
                        'ineligible_pairs': len(pairs) - len(eligible), 'missing_evidence_counts': dict(reasons),
                        'arms': {a: arm_summary(group['runs'], a) for a in ('baseline', 'optimized')},
                        'pairs': pairs, 'general_non_inferiority': 'not_established',
                        'actual_provider_usage': 'unavailable', 'actual_billed_cost': 'unavailable'})
    return {'schema': 'tokenguard.quality-scorecard/v1', 'experiments': len(experiments),
            'cohorts': results, 'model_api_calls': 0,
            'actual_provider_usage': 'unavailable', 'actual_billed_cost': 'unavailable',
            'general_non_inferiority': 'not_established',
            'limits': ['Descriptive counts only; repeats of one task are not independent tasks.',
                       'All planned pairs remain visible; the evidence subset is secondary and may be selected.',
                       'Cohorts are never pooled across protocol, budget, snapshot, prompt or pinned configuration.',
                       'Requested model labels and CLI usage are not certified provider routing or invoices.',
                       'Elapsed time covers agent execution only; local preflight and independent grading are excluded.',
                       'Unknown measurements remain null; known sums with incomplete coverage are not task totals.',
                       'No statistical non-inferiority or causal quality claim is made.']}


def evidence_hashes(folder: Path, record: dict) -> dict:
    names = ['experiment.json', 'quality-plan.json', 'execution-stopped.json', 'quality-preflight-latest.json']
    marker = folder / 'quality-preflight-latest.json'
    if marker.exists():
        try:
            raw = json.loads(read_bytes(marker))
            names.append(str(inside(folder, raw['path']).relative_to(folder)))
        except (OSError, ValueError, KeyError, TypeError):
            pass  # Invalid preflight remains ineligible; raw marker is still hashed.
    names += ['trials/' + trial['trial_id'] + '/' + relative for trial in record['trials']
              for relative in ('quality-result.json', 'agent.stream.jsonl', 'tg-data/audit-v04.jsonl')]
    hashes = {}
    for name in names:
        path = folder / name
        if not path.exists():
            hashes[name] = None
            continue
        try:
            hashes[name] = file_sha(path)
        except (OSError, ValueError) as exc:
            # The analysis already flags oversized/unsafe stream evidence. Keep
            # the sample visible and record that no full hash was available.
            stat = path.lstat()
            hashes[name] = {'sha256': None, 'error': type(exc).__name__,
                            'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
    return hashes


def collect(folders: list[Path]) -> tuple[list[dict], list[dict]]:
    if not 1 <= len(folders) <= 100:
        raise ValueError('Provide 1..100 explicit experiment directories')
    items, sources, ids, sessions = [], [], set(), set()
    for supplied in folders:
        folder = canonical_path(supplied)
        for name in ('experiment.json', 'task.json', 'settings.json', 'no-hook-settings.json', 'mcp.json'):
            canonical_path(folder / name)
        record = load_experiment(folder)
        if record['experiment_id'] in ids:
            raise ValueError('Duplicate experiment; copied directories are not independent samples')
        ids.add(record['experiment_id'])
        own_sessions = {t['session_id'] for t in record['trials']}
        if sessions & own_sessions:
            raise ValueError('Reused session across experiments')
        sessions.update(own_sessions)
        plan = json.loads(read_bytes(folder / 'quality-plan.json'))
        if not isinstance(plan, dict) or not isinstance(plan.get('instance_id'), str):
            raise ValueError('Invalid quality plan')
        if plan.get('protocol', 'agent_driven') not in MODES:
            raise ValueError('Unknown quality protocol')
        if plan.get('instance_sha256') and file_sha(folder / 'official-instance.json') != plan['instance_sha256']:
            raise ValueError('Task instance evidence changed')
        hashes = evidence_hashes(folder, record)
        analysis = analyze(folder, write=False)
        seed_inputs_verified = None
        if plan.get('protocol') == 'controlled_initial_log':
            try:
                checked = checked_preflight(folder, plan, record)
                deliveries = checked['seed_inputs']['deliveries']
                seed_inputs_verified = all(
                    run.get('seed_original_stdout_sha256') == deliveries[run['trial_id']]['original_stdout_sha256'] and
                    run.get('seed_delivered_prompt_sha256') == deliveries[run['trial_id']]['prompt_sha256']
                    for run in analysis['runs'] if run['state'] == 'completed')
            except (OSError, ValueError, KeyError, TypeError):
                seed_inputs_verified = False
        if evidence_hashes(folder, record) != hashes:
            raise ValueError('Input evidence changed while reading; retry after the experiment stops')
        if seed_inputs_verified:
            try:
                checked_preflight(folder, plan, record)
            except (OSError, ValueError, KeyError, TypeError):
                raise ValueError('Controlled evidence changed while reading') from None
        items.append({'record': record, 'plan': plan, 'analysis': analysis,
                      'seed_inputs_verified': seed_inputs_verified})
        sources.append({'experiment_id': record['experiment_id'], 'input_directory': str(folder), 'sha256': hashes})
    return items, sources


def write_report(result: dict, output: Path) -> Path:
    output = canonical_path(output)
    for source in result['sources']:
        if output.is_relative_to(Path(source['input_directory'])):
            raise ValueError('Output must be outside every input experiment')
    output.mkdir(parents=True, exist_ok=True)
    folder = output / ('quality-scorecard-' + uuid.uuid4().hex)
    folder.mkdir()
    (folder / 'scorecard.json').write_text(json.dumps(result, indent=2, ensure_ascii=True) + '\n', encoding='utf-8')
    fields = ['cohort_id', 'experiment_id', 'pair_id', 'observed_code_outcome', 'compression_exposed',
              'compression_comparison_eligible', 'missing_evidence', 'baseline_state', 'optimized_state',
              'baseline_strict_success', 'optimized_strict_success', 'baseline_end_reason', 'optimized_end_reason',
              'agent_elapsed_delta_ms_optimized_minus_baseline']
    with (folder / 'pairs.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for group in result['cohorts']:
            for row in group['pairs']:
                writer.writerow({'cohort_id': group['cohort_id'], **row,
                                 'missing_evidence': '; '.join(row['missing_evidence'])})
    lines = ['# Quality experiment scorecard', '', 'Descriptive observations; zero model calls.', '',
             'All planned pairs are retained. Different cohorts are not pooled.', '',
             '| Cohort | Task / model | Protocol | Max turns | Planned pairs | Evidence subset |',
             '| --- | --- | --- | ---: | ---: | ---: |']
    for group in result['cohorts']:
        lines.append(f"| {group['cohort_id']} | {group['task_id']} / {group['requested_model']} | "
                     f"{group['protocol']} | {group['max_turns']} | {group['planned_pairs']} | "
                     f"{group['compression_evidence_subset']['pairs']} |")
    for group in result['cohorts']:
        lines += ['', '## Cohort ' + group['cohort_id'], '',
                  '| Population | Baseline only resolved | Optimized only resolved | Both resolved | Neither resolved | Ungraded / incomplete |',
                  '| --- | ---: | ---: | ---: | ---: | ---: |']
        for label, counts in (('All planned', group['all_planned_code_outcomes']),
                              ('Compression evidence subset', group['compression_evidence_subset'])):
            lines.append('| ' + label + ' | ' + ' | '.join(str(counts[k]) for k in OUTCOMES) + ' |')
        lines += ['', '| Arm | Planned | Valid grades | Code resolved | Strict success | Max-turn exits | Not run |',
                  '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
        for arm, row in group['arms'].items():
            lines.append('| ' + arm + ' | ' + ' | '.join(str(row[k]) for k in
                         ('planned', 'valid_grades', 'code_resolved', 'strict_task_success', 'max_turns_exhausted', 'not_run')) + ' |')
        lines += ['', 'Missing evidence: ' + encoded(group['missing_evidence_counts']),
                  'Per-arm latency, recovery and unverified CLI usage coverage are in scorecard.json.']
    lines += ['', 'Provider usage and actual billed cost: unavailable.',
              'General quality preservation: not established.', '', *('- ' + line for line in result['limits']), '']
    (folder / 'scorecard.md').write_text('\n'.join(lines), encoding='utf-8')
    return folder


def compare(folders: list[Path], output: Path) -> Path:
    items, sources = collect(folders)
    return write_report({**scorecard(items), 'sources': sources}, output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('experiments', type=Path, nargs='+')
    parser.add_argument('--output', type=Path, default=BASE / 'reports/quality-scorecards')
    args = parser.parse_args()
    folder = compare(args.experiments, args.output)
    print(folder / 'scorecard.md')


if __name__ == '__main__':
    main()
