"""Read-only analysis of one completed/partial quality pilot; no model calls."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tokenguard.quality_protocol import pair_assessment


def read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('Input exceeds recorded process budget')
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def analyze(folder: Path) -> dict:
    record = json.loads((folder / 'experiment.json').read_text(encoding='utf-8'))
    plan = json.loads((folder / 'quality-plan.json').read_text(encoding='utf-8'))
    runs = []
    pair_rows = []
    for trial in record['trials']:
        if not re.fullmatch(r'[0-9]{3}-(baseline|optimized)', trial['trial_id']):
            raise ValueError('Invalid trial name')
        root = folder / 'trials' / trial['trial_id']
        result_path = root / 'quality-result.json'
        if not result_path.exists():
            runs.append({**trial, 'state': 'not_run'})
            pair_rows.append({**trial, 'not_run': True})
            continue
        row = json.loads(result_path.read_text(encoding='utf-8'))
        if any(row.get(k) != trial[k] for k in ('trial_id', 'pair_id', 'arm', 'session_id')):
            raise ValueError('Trial identity differs')
        pair_rows.append(row)
        events = read_rows(root / 'agent.stream.jsonl')
        finals = [e for e in events if e.get('type') == 'result']
        final = finals[0] if len(finals) == 1 and finals[0].get('session_id') == trial['session_id'] else {}
        tool_messages = []
        tool_errors = 0
        for event in events:
            if event.get('type') != 'user':
                continue
            content = event.get('message', {}).get('content', [])
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get('type') == 'tool_result':
                    tool_messages.append(json.dumps(block.get('content', ''), ensure_ascii=False))
                    tool_errors += bool(block.get('is_error'))
        compressed = [r for r in read_rows(root / 'tg-data/audit-v04.jsonl')
                      if r.get('event') == 'compression' and r.get('session_id') == trial['session_id'] and
                      r.get('experiment_id') == record['experiment_id']]
        seen_pairs = {match for text in tool_messages for match in re.findall(
            r'\[TokenGuard artifact=([0-9a-f]{64}); compression_id=([0-9a-f]{32})\]', text)}
        own_pairs = {(r['artifact_id'], r['compression_id']) for r in compressed}
        visible = len(seen_pairs & own_pairs)
        grade = row['grade']
        statuses = grade.get('tests_status', {})
        runs.append({**trial, 'state': 'completed', 'code_resolved': bool(grade.get('resolved')),
                     'evaluation_valid': grade.get('evaluation_valid', False),
                     'task_completed_and_valid': row['task_success'],
                     'agent_end_reason': final.get('subtype', row.get('execution_error') or 'unknown'),
                     'agent_exit_code': row['agent_exit_code'],
                     'f2p_success': len(statuses.get('FAIL_TO_PASS', {}).get('success', [])),
                     'p2p_success': len(statuses.get('PASS_TO_PASS', {}).get('success', [])),
                     'agent_elapsed_ms': row['agent_elapsed_ms'], 'tool_calls': row['tool_calls'],
                     'tool_error_results': tool_errors, 'permission_denial_count': row.get('permission_denial_count'),
                     'compressions': row['compression']['compressions'],
                     'compressed_messages_in_host_transcript': visible,
                     'unattributed_host_metadata_pairs': len(seen_pairs - own_pairs),
                     'model_metadata_citations': row['model_cited_matching_metadata_count'],
                     'recovery_count': row['compression']['recovery_count'],
                     'saved_stdout_chars': row['compression']['saved_chars'],
                     'changed_files': row['source_changes']['changed_files'],
                     'only_allowed_changes': row['source_changes']['only_allowed_changes'],
                     'test_protocol_satisfied': row.get('protocol_evidence', {}).get('agent_test_protocol_satisfied', 'unavailable'),
                     'native_compressions_in_matching_bash_results': row.get('protocol_evidence', {}).get(
                         'native_compressions_in_matching_bash_results', 'unavailable'),
                     'seed_compressions': row.get('seed_compression', {}).get('compressions', 0),
                     'seed_recoveries': row.get('seed_compression', {}).get('recovery_count', 0),
                     'cli_aggregate_usage_unverified': row['cli_aggregate_usage_unverified']})
    summary = {}
    for arm in ('baseline', 'optimized'):
        own = [r for r in runs if r['arm'] == arm]
        done = [r for r in own if r['state'] == 'completed']
        summary[arm] = {'planned': len(own), 'completed': len(done),
                        'code_resolved': sum(r['code_resolved'] for r in done),
                        'tasks_completed_and_valid': sum(r['task_completed_and_valid'] for r in done),
                        'max_turns_exhausted': sum(r['agent_end_reason'] == 'error_max_turns' for r in done),
                        'compressions': sum(r['compressions'] for r in done),
                        'host_transcript_compressed_messages': sum(r['compressed_messages_in_host_transcript'] for r in done),
                        'recoveries': sum(r['recovery_count'] for r in done)}
    pairs = []
    for pair in sorted({r['pair_id'] for r in pair_rows}):
        group = {r['arm']: r for r in pair_rows if r['pair_id'] == pair}
        pairs.append({'pair_id': pair, **pair_assessment(group.get('baseline', {'not_run': True}),
                     group.get('optimized', {'not_run': True}), plan.get('protocol', 'agent_driven'))})
    conclusion = 'insufficient_evidence'
    if all(summary[a]['completed'] == summary[a]['planned'] for a in summary):
        if plan.get('protocol') == 'controlled_initial_log' and not all(p['comparison_evidence_satisfied'] for p in pairs):
            conclusion = 'controlled_protocol_evidence_incomplete'
        elif plan.get('protocol') != 'controlled_initial_log' and summary['optimized']['compressions'] == 0:
            conclusion = 'compression_not_exercised'
        elif not all(p['comparison_evidence_satisfied'] for p in pairs):
            conclusion = 'test_or_visibility_evidence_incomplete; historical_runs_not_retroactively_certified'
        elif all(summary[a]['code_resolved'] == summary[a]['planned'] for a in summary):
            conclusion = 'all_observed_patches_resolved; general_non_inferiority_not_established'
        elif all(summary[a]['code_resolved'] == 0 for a in summary):
            conclusion = 'both_arms_unresolved; no_quality_preservation_conclusion'
        else:
            conclusion = 'mixed_outcomes; inspect_each_pair_and_budget_before_causal_attribution'
    result = {'schema': 'tokenguard.quality-analysis/v1', 'experiment_id': record['experiment_id'],
              'instance_id': plan['instance_id'], 'summary': summary, 'runs': runs,
              'protocol': plan.get('protocol', 'agent_driven'), 'pairs': pairs,
              'interpretation': conclusion, 'model_api_calls_from_analysis': 0,
              'actual_provider_usage': 'unavailable', 'actual_billed_cost': 'unavailable',
              'limits': [f"single public task, {len({r['pair_id'] for r in runs})} paired rounds, no general non-inferiority proof",
                         'public benchmark contamination/memorization is not controlled',
                         'CLI usage aggregates are unverified and are not provider invoices',
                         'host transcript markers and model citations are distinct visibility evidence',
                         'code resolution and successful agent completion are separate outcomes']}
    (folder / 'quality-analysis.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    fields = ['trial_id', 'arm', 'state', 'code_resolved', 'task_completed_and_valid', 'agent_end_reason',
              'f2p_success', 'p2p_success', 'agent_elapsed_ms', 'tool_calls', 'compressions',
              'compressed_messages_in_host_transcript', 'recovery_count', 'test_protocol_satisfied',
              'native_compressions_in_matching_bash_results', 'seed_compressions', 'seed_recoveries']
    with (folder / 'quality-analysis.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for run in runs:
            writer.writerow({k: run.get(k, 'unavailable') for k in fields})
    lines = ['# Real task quality analysis', '', plan['instance_id'], '',
             '| Trial | Code resolved | Agent complete | End reason | F2P / 4 | P2P / 171 | Compressions | Transcript matches |',
             '| --- | --- | --- | --- | ---: | ---: | ---: | ---: |']
    for run in runs:
        lines.append('| ' + ' | '.join(str(run.get(k, 'unavailable')) for k in
                     ('trial_id', 'code_resolved', 'task_completed_and_valid', 'agent_end_reason',
                      'f2p_success', 'p2p_success', 'compressions', 'compressed_messages_in_host_transcript')) + ' |')
    lines += ['', 'Protocol: ' + plan.get('protocol', 'agent_driven'), '',
              'Pair evidence (all planned pairs, including failures):', '',
              '| Pair | Evidence satisfied | Exposed | Missing evidence |', '| --- | --- | --- | --- |']
    for pair in pairs:
        lines.append(f"| {pair['pair_id']} | {pair['comparison_evidence_satisfied']} | {pair['compression_exposed']} | "
                     + ', '.join(pair['reasons']) + ' |')
    lines += ['', 'Interpretation: ' + conclusion, '',
              'Task-specific raw CLI aggregates (UNVERIFIED; not an API/billing savings calculation):', '',
              '| Trial | CLI input | CLI cache read | CLI cache creation | CLI output |',
              '| --- | ---: | ---: | ---: | ---: |']
    for run in runs:
        usage = run.get('cli_aggregate_usage_unverified', {})
        lines.append('| ' + run['trial_id'] + ' | ' + ' | '.join(str(usage.get(k, 'unavailable')) for k in
                     ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens', 'output_tokens')) + ' |')
    lines += ['', 'Limitations:', *('- ' + item for item in result['limits']), '']
    (folder / 'quality-analysis.md').write_text('\n'.join(lines), encoding='utf-8')
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('experiment', type=Path)
    args = parser.parse_args()
    result = analyze(args.experiment.resolve())
    print(json.dumps({'summary': result['summary'], 'interpretation': result['interpretation'],
                      'report': str(args.experiment.resolve() / 'quality-analysis.md')}, indent=2))


if __name__ == '__main__':
    main()
