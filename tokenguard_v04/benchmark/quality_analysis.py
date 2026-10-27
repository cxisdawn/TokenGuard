"""Read-only analysis of one completed/partial quality pilot; no model calls."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tokenguard import audit
from tokenguard.experiment import audit_scope
from tokenguard.quality_protocol import evidence, pair_assessment, read_bytes, text_blocks, trace_tools
from tokenguard.visible_tests import fingerprint


def mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def number(value: object) -> int | float | None:
    return value if type(value) in (int, float) and 0 <= value < float('inf') else None


def count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def status_count(grade: dict, category: str, errors: list[str]) -> int | None:
    statuses = grade.get('tests_status', {})
    if not isinstance(statuses, dict):
        errors.append('grade_status_schema_invalid')
        return None
    if category not in statuses:
        return None
    group = statuses[category]
    invalid = not isinstance(group, dict)
    sets = {}
    for key in ('success', 'failure'):
        if not invalid and key in group:
            items = group[key]
            if (not isinstance(items, list) or any(not isinstance(item, str) or not item for item in items) or
                    len(set(items)) != len(items)):
                invalid = True
            else:
                sets[key] = set(items)
    invalid = invalid or bool(sets.get('success', set()) & sets.get('failure', set()))
    invalid = invalid or (grade.get('resolved') is True and bool(sets.get('failure')))
    if invalid:
        errors.append('grade_status_schema_invalid')
        return None
    return len(group['success']) if 'success' in group else None


def current_protocol(folder: Path, root: Path, record: dict, plan: dict, trial: dict,
                     row: dict, events: list[dict], audits: list[dict], errors: list[str]) -> dict:
    """Recheck receipts and source bytes; a cached verdict cannot certify a run."""
    unknown = {'agent_test_protocol_satisfied': False, 'final_test_command_observed': False,
               'native_compressions_in_matching_bash_results': 0, 'evidence_available': False}
    try:
        files = record['task']['editable_files']
        initial = fingerprint(folder / 'snapshot', files)
        if (initial != plan.get('initial_source_sha256') or
                not isinstance(plan.get('required_command'), str) or not plan['required_command'] or
                not isinstance(plan.get('image_id'), str) or not plan['image_id'] or
                count(plan.get('visible_expected_tests')) in (None, 0)):
            raise ValueError('Incomplete protocol inputs')
        final = (fingerprint(root / 'worktree', files)
                 if mapping(row.get('source_changes')).get('only_allowed_changes') is True else None)
        observed = evidence(events, audits, root / 'test-receipts', session=trial['session_id'],
                            experiment=record['experiment_id'], initial_source=initial, final_source=final,
                            required_command=plan['required_command'], image_id=plan['image_id'],
                            expected_tests=plan['visible_expected_tests'])
        if observed['invalid_receipts']:
            errors.append('test_receipts_invalid')
        return {**observed, 'evidence_available': True}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        errors.append('current_protocol_evidence_unavailable:' + type(exc).__name__)
        return unknown


def read_rows(path: Path, errors: list[str] | None = None) -> list[dict]:
    errors = errors if errors is not None else []
    if not path.exists():
        return []
    try:
        raw = read_bytes(path).decode('utf-8')
    except (OSError, ValueError) as exc:
        errors.append(path.name + ':' + type(exc).__name__)
        return []
    rows = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError('Invalid row')
            rows.append(row)
        except ValueError:
            if path.name + ':invalid_row' not in errors:
                errors.append(path.name + ':invalid_row')
        if len(rows) >= 100000:
            errors.append(path.name + ':row_limit')
            break
    return rows


def current_audit_integrity(root: Path, trial: dict, record: dict, compression: dict,
                            seed: dict, errors: list[str]) -> bool:
    """Cached totals cannot make damaged current audit evidence complete."""
    path = root / 'tg-data/audit-v04.jsonl'
    valid = True
    diagnostic_path = path.with_name('diagnostics-v04.jsonl')
    try:
        with audit_scope(path.parent):
            diagnostic = audit.diagnostics(trial['session_id'], record['experiment_id'], limit=1)
        if diagnostic['data_available'] is not True:
            errors.append(diagnostic_path.name + ':integrity_incomplete')
            valid = False
    except (OSError, ValueError, TypeError, KeyError) as exc:
        errors.append(diagnostic_path.name + ':' + type(exc).__name__)
        valid = False
    if seed.get('data_available', True) is not True:
        errors.append(path.name + ':cached_seed_unavailable')
        valid = False
    if any(error.startswith(path.name + ':') for error in errors):
        return False
    if not path.exists():
        # No audit file is expected for a clean zero-compression baseline.
        if any((count(value.get('compressions')) or 0) > 0 or
               (count(value.get('recovery_count')) or 0) > 0 for value in (compression, seed)):
            errors.append(path.name + ':missing_observed_records')
            return False
        return valid
    try:
        with audit_scope(path.parent):
            current = audit.summary(trial['session_id'], record['experiment_id'],
                                    source='claude_hook', record_limit=0)
            current_seed = audit.summary(trial['session_id'], record['experiment_id'],
                                         source='manual', record_limit=0)
        if any(value['data_available'] is not True for value in (current, current_seed)):
            errors.append(path.name + ':integrity_incomplete')
            return False
        if any(count(compression.get(key)) is not None and compression[key] != current[key]
               for key in ('compressions', 'recovery_count')):
            errors.append(path.name + ':cached_counts_differ')
            valid = False
        if any((count(seed.get(key)) is not None and seed[key] != current_seed[key]) or
               (count(seed.get(key)) is None and current_seed[key] > 0)
               for key in ('compressions', 'recovery_count')):
            errors.append(path.name + ':cached_seed_counts_differ')
            valid = False
        return valid
    except (OSError, ValueError, TypeError, KeyError) as exc:
        errors.append(path.name + ':' + type(exc).__name__)
        return False


def analyze(folder: Path, *, write: bool = True) -> dict:
    record = json.loads(read_bytes(folder / 'experiment.json'))
    plan = json.loads(read_bytes(folder / 'quality-plan.json'))
    report_errors = []
    stopped = None
    stopped_path = folder / 'execution-stopped.json'
    if stopped_path.exists():
        try:
            stopped = json.loads(read_bytes(stopped_path))
            if not isinstance(stopped, dict):
                raise ValueError('Invalid execution-stopped record')
        except (OSError, ValueError, TypeError) as exc:
            stopped = None
            report_errors.append('execution-stopped.json:' + type(exc).__name__)
    runs = []
    pair_rows = []
    for trial in record['trials']:
        if not re.fullmatch(r'[0-9]{3}-(baseline|optimized)', trial['trial_id']):
            raise ValueError('Invalid trial name')
        root = folder / 'trials' / trial['trial_id']
        result_path = root / 'quality-result.json'
        if not result_path.exists():
            attempted = (root / 'agent-started.json').exists() or (root / 'agent.stream.jsonl').exists()
            missing = {**trial, 'analysis_evidence_errors': ['result_missing_after_launch_attempt'] if attempted else []}
            runs.append({**missing, 'state': 'evidence_invalid' if attempted else 'not_run'})
            pair_rows.append({**missing, 'not_run': True})
            continue
        try:
            row = json.loads(read_bytes(result_path))
            if not isinstance(row, dict) or any(row.get(k) != trial[k]
                    for k in ('trial_id', 'pair_id', 'arm', 'session_id')):
                raise ValueError('Trial identity or shape differs')
        except (OSError, ValueError, TypeError) as exc:
            invalid = {**trial, 'analysis_evidence_errors': ['quality-result.json:' + type(exc).__name__]}
            runs.append({**invalid, 'state': 'evidence_invalid'})
            pair_rows.append({**invalid, 'not_run': True})
            continue
        if row.get('not_run'):
            pair_rows.append({**trial, 'not_run': True, 'stage_errors': row.get('stage_errors', [])})
            runs.append({**trial, 'state': 'blocked_before_launch' if row.get('blocked_before_launch') else 'not_run',
                         'stage_errors': row.get('stage_errors', [])})
            continue
        stream_errors = []
        events = read_rows(root / 'agent.stream.jsonl', stream_errors)
        finals = [e for e in events if e.get('type') == 'result']
        final = finals[0] if len(finals) == 1 and finals[0].get('session_id') == trial['session_id'] else {}
        if not final or any(e.get('session_id', trial['session_id']) != trial['session_id'] for e in events):
            stream_errors.append('stream_session_or_final_mismatch')
        stream_readable = not stream_errors
        analysis_errors = [*report_errors, *stream_errors]
        tool_messages = []
        tool_errors = 0
        for event in events:
            if event.get('type') != 'user':
                continue
            message = event.get('message')
            content = message.get('content', []) if isinstance(message, dict) else []
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get('type') == 'tool_result':
                    tool_messages.append(json.dumps(block.get('content', ''), ensure_ascii=False))
                    tool_errors += bool(block.get('is_error'))
        audits = read_rows(root / 'tg-data/audit-v04.jsonl', analysis_errors)
        scoped = [r for r in audits
                      if r.get('event') == 'compression' and r.get('session_id') == trial['session_id'] and
                      r.get('experiment_id') == record['experiment_id'] and
                      isinstance(r.get('artifact_id'), str) and isinstance(r.get('compression_id'), str)]
        compressed = [r for r in scoped if r.get('source') == 'claude_hook']
        seen_pairs = {match for text in tool_messages for match in re.findall(
            r'\[TokenGuard artifact=([0-9a-f]{64}); compression_id=([0-9a-f]{32})\]', text)}
        own_pairs = {(r.get('artifact_id'), r.get('compression_id')) for r in compressed}
        manual_pairs = {(r.get('artifact_id'), r.get('compression_id')) for r in scoped if r.get('source') == 'manual'}
        visible = len(seen_pairs & own_pairs)
        calls, tool_results, trace_errors = trace_tools(events, trial['session_id'])
        stream_errors.extend(trace_errors)
        analysis_errors.extend(trace_errors)
        matched_native = set()
        for entry in compressed:
            key = entry.get('tool_use_id')
            if not isinstance(key, str) or calls.get(key, {}).get('name') != 'Bash':
                continue
            guide = (entry.get('artifact_id'), entry.get('compression_id'))
            text = text_blocks(tool_results.get(key, {}).get('content'))
            if guide in re.findall(r'\[TokenGuard artifact=([0-9a-f]{64}); compression_id=([0-9a-f]{32})\]', text):
                matched_native.add(guide)
        observed = current_protocol(folder, root, record, plan, trial, row, events, scoped, analysis_errors)
        # Visibility can still be measured even if historical source/receipt inputs are missing.
        observed['native_compressions_in_matching_bash_results'] = len(matched_native)
        grade = row.get('grade')
        grade_shape = (isinstance(grade, dict) and type(grade.get('resolved')) is bool and
                       type(grade.get('evaluation_valid')) is bool)
        if not grade_shape:
            analysis_errors.append('grade_schema_invalid')
            grade = {}
        f2p, p2p = (status_count(grade, category, analysis_errors)
                    for category in ('FAIL_TO_PASS', 'PASS_TO_PASS'))
        evaluation_valid = (grade_shape and grade.get('evaluation_valid') is True and
                            'grade_status_schema_invalid' not in analysis_errors)
        source = mapping(row.get('source_changes'))
        allowed = source.get('only_allowed_changes') is True
        resolved = grade.get('resolved') if evaluation_valid and allowed else None
        compression, seed = mapping(row.get('compression')), mapping(row.get('seed_compression'))
        audit_valid = current_audit_integrity(root, trial, record, compression, seed, analysis_errors)
        available = (audit_valid and compression.get('data_available', True) is True and
                     all(count(compression.get(k)) is not None for k in ('compressions', 'recovery_count')))
        seed_available = (audit_valid and seed.get('data_available', True) is True and
                          all(count(seed.get(k)) is not None for k in ('compressions', 'recovery_count')))
        success = None
        if resolved is not None:
            if (resolved is False or row.get('task_success') is False or row.get('stage_errors') or
                    row.get('timed_out') is True or row.get('outputs_truncated') is True or
                    (type(row.get('agent_exit_code')) is int and row['agent_exit_code'] != 0) or
                    final.get('is_error') is True or str(final.get('subtype', '')).startswith('error')):
                success = False
            elif (row.get('task_success') is True and type(row.get('agent_exit_code')) is int and
                  row['agent_exit_code'] == 0 and final.get('subtype') == 'success' and
                  final.get('is_error') is False and not stream_errors):
                success = True
        pair_rows.append({**trial, 'analysis_evidence_errors': analysis_errors,
                          'stage_errors': row.get('stage_errors', []),
                          'grade': {'evaluation_valid': evaluation_valid, 'resolved': resolved},
                          'source_changes': {'only_allowed_changes': allowed},
                          'compression': {**compression, 'data_available': available},
                          'seed_compression': {**seed, 'data_available': seed_available},
                          'protocol_evidence': observed, 'seed_delivery': mapping(row.get('seed_delivery'))})
        runs.append({**trial, 'state': 'completed', 'code_resolved': resolved,
                     'evaluation_valid': evaluation_valid,
                     'execution_status': row.get('execution_status', 'legacy'),
                     'stage_errors': row.get('stage_errors', []),
                     'analysis_evidence_errors': analysis_errors,
                     'compression_data_available': available,
                     'seed_data_available': seed_available,
                     'task_completed_and_valid': success,
                     'agent_end_reason': final.get('subtype', row.get('execution_error') or 'unknown'),
                     'agent_exit_code': row.get('agent_exit_code'),
                     'f2p_success': f2p, 'p2p_success': p2p,
                     'agent_elapsed_ms': number(row.get('agent_elapsed_ms')), 'tool_calls': count(row.get('tool_calls')),
                     'tool_error_results': tool_errors if stream_readable else None,
                     'permission_denial_count': count(row.get('permission_denial_count')),
                     'compressions': count(compression.get('compressions')) if available else None,
                     'compressed_messages_in_host_transcript': visible if stream_readable else None,
                     'manual_guides_echoed_in_tool_results': len(seen_pairs & manual_pairs) if stream_readable else None,
                     'unattributed_host_metadata_pairs': len(seen_pairs - own_pairs - manual_pairs) if stream_readable else None,
                     'model_metadata_citations': count(row.get('model_cited_matching_metadata_count')),
                     'recovery_count': count(compression.get('recovery_count')) if available else None,
                     'saved_stdout_chars': count(compression.get('saved_chars')) if available else None,
                     'changed_files': source.get('changed_files'),
                     'only_allowed_changes': allowed,
                     'test_protocol_satisfied': observed['agent_test_protocol_satisfied'],
                     'protocol_evidence_rechecked': observed['evidence_available'],
                     'native_compressions_in_matching_bash_results': len(matched_native) if not stream_errors else None,
                     'seed_compressions': count(seed.get('compressions')) if seed_available else None,
                     'seed_recoveries': count(seed.get('recovery_count')) if seed_available else None,
                     'seed_original_stdout_sha256': mapping(row.get('seed_delivery')).get('original_stdout_sha256'),
                     'seed_delivered_prompt_sha256': mapping(row.get('seed_delivery')).get('prompt_sha256'),
                     'cli_aggregate_usage_unverified': mapping(row.get('cli_aggregate_usage_unverified'))})
    summary = {}
    for arm in ('baseline', 'optimized'):
        own = [r for r in runs if r['arm'] == arm]
        done = [r for r in own if r['state'] == 'completed']
        graded = [r for r in done if r['evaluation_valid'] and r['only_allowed_changes']]
        known_compressions = [r['compressions'] for r in done if r['compressions'] is not None]
        known_recoveries = [r['recovery_count'] for r in done if r['recovery_count'] is not None]
        known_visible = [r['compressed_messages_in_host_transcript'] for r in done
                         if r['compressed_messages_in_host_transcript'] is not None]
        summary[arm] = {'planned': len(own), 'completed': len(done),
                        'valid_grades': len(graded), 'ungraded_or_incomplete': len(own) - len(graded),
                        'evidence_invalid': sum(r['state'] == 'evidence_invalid' for r in own),
                        'code_resolved': sum(r['code_resolved'] is True for r in graded),
                        'tasks_completed_and_valid': sum(r['task_completed_and_valid'] is True for r in graded),
                        'max_turns_exhausted': sum(r['agent_end_reason'] == 'error_max_turns' for r in done),
                        'compressions': sum(known_compressions) if known_compressions else None,
                        'compression_measurements': len(known_compressions),
                        'host_transcript_compressed_messages': sum(known_visible) if known_visible else None,
                        'host_transcript_measurements': len(known_visible),
                        'infrastructure_failed': sum(bool(r['stage_errors']) for r in done),
                        'blocked_before_launch': sum(r['state'] == 'blocked_before_launch' for r in own),
                        'analysis_evidence_incomplete': sum(bool(r.get('analysis_evidence_errors')) for r in own),
                        'compression_data_complete': len(done) == len(own) and all(r['compression_data_available'] for r in done),
                        'seed_data_complete': len(done) == len(own) and all(r['seed_data_available'] for r in done),
                        'recoveries': sum(known_recoveries) if known_recoveries else None,
                        'recovery_measurements': len(known_recoveries)}
    pairs = []
    for pair in sorted({r['pair_id'] for r in pair_rows}):
        group = {r['arm']: r for r in pair_rows if r['pair_id'] == pair}
        pairs.append({'pair_id': pair, **pair_assessment(group.get('baseline', {'not_run': True}),
                     group.get('optimized', {'not_run': True}), plan.get('protocol', 'agent_driven'))})
    conclusion = 'insufficient_evidence'
    if all(summary[a]['completed'] == summary[a]['planned'] for a in summary):
        if plan.get('protocol') == 'controlled_initial_log' and not all(p['comparison_evidence_satisfied'] for p in pairs):
            conclusion = 'controlled_protocol_evidence_incomplete'
        elif (plan.get('protocol') != 'controlled_initial_log' and
              summary['optimized']['compression_data_complete'] and summary['optimized']['compressions'] == 0 and
              summary['optimized']['analysis_evidence_incomplete'] == 0 and
              all(r.get('native_compressions_in_matching_bash_results') == 0
                  for r in runs if r['arm'] == 'optimized') and not report_errors):
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
              'execution_stopped': stopped, 'execution_stopped_available': not report_errors,
              'report_evidence_errors': report_errors,
              'interpretation': conclusion, 'model_api_calls_from_analysis': 0,
              'actual_provider_usage': 'unavailable', 'actual_billed_cost': 'unavailable',
              'limits': [f"single public task, {len({r['pair_id'] for r in runs})} paired rounds, no general non-inferiority proof",
                         'public benchmark contamination/memorization is not controlled',
                         'CLI usage aggregates are unverified and are not provider invoices',
                         'host transcript markers and model citations are distinct visibility evidence',
                         'code resolution and successful agent completion are separate outcomes',
                         'null means unknown; false code resolution requires a valid grade and allowed source changes',
                         'native protocol evidence is rechecked against current receipts, logs and editable source bytes',
                         'receipt evidence is local to the same OS user, not tamper-proof attestation']}
    if not write:
        return result
    (folder / 'quality-analysis.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    fields = ['trial_id', 'arm', 'state', 'code_resolved', 'task_completed_and_valid', 'agent_end_reason',
              'f2p_success', 'p2p_success', 'agent_elapsed_ms', 'tool_calls', 'compressions',
              'compressed_messages_in_host_transcript', 'recovery_count', 'test_protocol_satisfied',
              'native_compressions_in_matching_bash_results', 'seed_compressions', 'seed_recoveries',
              'execution_status', 'stage_errors', 'compression_data_available']
    fields += ['analysis_evidence_errors', 'manual_guides_echoed_in_tool_results']
    with (folder / 'quality-analysis.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for run in runs:
            writer.writerow({k: run.get(k, 'unavailable') for k in fields})
    lines = ['# Real task quality analysis', '', plan['instance_id'], '',
             '| Trial | Code resolved | Agent complete | End reason | Execution status | F2P passed | P2P passed | Compressions | Transcript matches |',
             '| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: |']
    for run in runs:
        lines.append('| ' + ' | '.join(str(run.get(k, 'unavailable')) for k in
                     ('trial_id', 'code_resolved', 'task_completed_and_valid', 'agent_end_reason',
                      'execution_status', 'f2p_success', 'p2p_success', 'compressions', 'compressed_messages_in_host_transcript')) + ' |')
    lines += ['', 'Protocol: ' + plan.get('protocol', 'agent_driven'), '',
              'Pair evidence (all planned pairs, including failures):', '',
              '| Pair | Evidence satisfied | Exposed | Missing evidence |', '| --- | --- | --- | --- |']
    for pair in pairs:
        lines.append(f"| {pair['pair_id']} | {pair['comparison_evidence_satisfied']} | {pair['compression_exposed']} | "
                     + ', '.join(pair['reasons']) + ' |')
    lines += ['', 'Interpretation: ' + conclusion,
              'Compression totals cover available observations only; see compression_data_complete.',
              'Stopped: ' + json.dumps(result['execution_stopped'], ensure_ascii=True), '',
              'Report evidence errors: ' + json.dumps(report_errors, ensure_ascii=True), '',
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
