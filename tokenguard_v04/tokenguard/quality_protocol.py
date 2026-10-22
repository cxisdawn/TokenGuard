"""Bounded local evidence for quality experiments; never launches an agent."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from .visible_tests import canonical_path

GUIDE = re.compile(r'\[TokenGuard artifact=([0-9a-f]{64}); compression_id=([0-9a-f]{32})\]')
TEST_RUN = re.compile(r'\[TokenGuard test_run=([0-9a-f]{32})\]')
MODES = ('agent_driven', 'controlled_initial_log')
LIMIT = 16 * 1024 * 1024


def read_bytes(path: Path) -> bytes:
    path = canonical_path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > LIMIT:
        raise ValueError('Evidence missing, linked or oversized')
    with path.open('rb') as stream:
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError('Evidence exceeds limit')
    return data


def file_sha(path: Path) -> str:
    return hashlib.sha256(read_bytes(path)).hexdigest()


def text_blocks(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(b['text'] for b in content if isinstance(b, dict) and
                         b.get('type') == 'text' and isinstance(b.get('text'), str))
    return ''


def pytest_test_count(stdout: str) -> int:
    summaries = re.findall(r'^=+ (.*\b(?:passed|failed|error|errors|skipped|xfailed)\b.*) in .*=+\s*$',
                           stdout, re.MULTILINE)
    return sum(int(n) for n, _ in re.findall(
        r'\b(\d+) (passed|failed|errors?|skipped|xfailed|xpassed)\b', summaries[-1])) if summaries else 0


def trace_tools(events: list[dict], session: str) -> tuple[dict, dict, list[str]]:
    calls, results, errors = {}, {}, []
    for event in events:
        if not isinstance(event, dict) or event.get('session_id', session) != session:
            errors.append('mixed_or_invalid_session')
            continue
        message = event.get('message')
        content = message.get('content', []) if isinstance(message, dict) else []
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if event.get('type') == 'assistant' and block.get('type') == 'tool_use':
                key = block.get('id')
                if not isinstance(key, str):
                    errors.append('invalid_tool_id')
                elif key in calls and calls[key] != block:
                    errors.append('conflicting_tool_use')
                else:
                    calls[key] = block
            if event.get('type') == 'user' and block.get('type') == 'tool_result':
                key = block.get('tool_use_id')
                if not isinstance(key, str):
                    errors.append('invalid_result_id')
                elif key in results and results[key] != block:
                    errors.append('conflicting_tool_result')
                else:
                    results[key] = block
    return calls, results, sorted(set(errors))


def receipt(path: Path, session: str, experiment: str) -> dict:
    row = json.loads(read_bytes(path))
    run_id = path.stem
    if not isinstance(row, dict) or not re.fullmatch('[0-9a-f]{32}', run_id) or any((
            row.get('schema') != 'tokenguard.visible-test/v1', row.get('run_id') != run_id,
            row.get('session_id') != session, row.get('experiment_id') != experiment)):
        raise ValueError('Receipt scope mismatch')
    for kind in ('stdout', 'stderr'):
        raw = read_bytes(path.with_suffix('.' + kind + '.txt'))
        if hashlib.sha256(raw).hexdigest() != row.get(kind + '_sha256') or len(raw) != row.get(kind + '_bytes'):
            raise ValueError('Receipt log changed')
    return row


def evidence(events: list[dict], audits: list[dict], receipt_dir: Path, *, session: str,
             experiment: str, initial_source: str, final_source: str | None,
             required_command: str | None, image_id: str | None = None,
             expected_tests: int | None = None) -> dict:
    calls, results, errors = trace_tools(events, session)
    bash = {key: call for key, call in calls.items() if call.get('name') == 'Bash'}
    exact = [key for key, call in bash.items() if required_command is not None and
             isinstance(call.get('input'), dict) and call['input'].get('command') == required_command]
    matched = []
    visible_runs = {}
    for key, block in results.items():
        if key not in bash:
            continue
        text = text_blocks(block.get('content'))
        pairs = set(GUIDE.findall(text))
        for row in audits:
            if (row.get('event') == 'compression' and row.get('session_id') == session and
                    row.get('experiment_id') == experiment and row.get('tool_use_id') == key and
                    row.get('source') == 'claude_hook' and
                    (row.get('artifact_id'), row.get('compression_id')) in pairs):
                matched.append((row['artifact_id'], row['compression_id']))
        for run_id in TEST_RUN.findall(text):
            visible_runs.setdefault(run_id, set()).add(key)
    verified = []
    invalid = 0
    receipt_dir = canonical_path(receipt_dir)
    if receipt_dir.exists():
        paths = sorted(receipt_dir.glob('*.json'))
        if len(paths) > 1000:
            raise ValueError('Too many receipts')
        for path in paths:
            try:
                row = receipt(path, session, experiment)
                if row.get('origin') != 'agent' or (image_id is not None and row.get('image_id') != image_id):
                    raise ValueError('Unexpected receipt origin or image')
                stdout = read_bytes(path.with_suffix('.stdout.txt')).decode('utf-8', errors='replace')
                # Require a complete pytest summary, not an import error/command mention.
                count = pytest_test_count(stdout)
                summary = count > 0 and (expected_tests is None or count == expected_tests)
                stable = row.get('source_before') == row.get('source_after')
                complete = (row.get('exit_code') in (0, 1) and not row.get('timed_out') and
                            not row.get('outputs_truncated') and row.get('full_suite_requested') is True and
                            row.get('arguments') == [] and stable and summary)
                verified.append({'run_id': row['run_id'], 'exit_code': row['exit_code'],
                                 'complete_full_suite': complete,
                                 'initial_source': row.get('source_before') == initial_source,
                                 'final_source': final_source is not None and row.get('source_after') == final_source,
                                 'observed_tests': count,
                                 'tool_result_matched': row['run_id'] in visible_runs,
                                 'exact_command_matched': any(k in exact for k in visible_runs.get(row['run_id'], ()))})
            except (OSError, ValueError, TypeError, KeyError):
                invalid += 1
    initial = any(r['complete_full_suite'] and r['initial_source'] and r['tool_result_matched'] and
                  r['exact_command_matched'] for r in verified)
    final = any(r['complete_full_suite'] and r['final_source'] and r['tool_result_matched'] and
                r['exact_command_matched'] for r in verified)
    # Distinct executions in actual tool invocation order. If source is unchanged,
    # one receipt cannot satisfy both prescribed observations.
    positions = {key: n for n, key in enumerate(calls)}
    eligible = [r for r in verified if r['complete_full_suite'] and r['tool_result_matched'] and r['exact_command_matched']]
    compliant = any(a['run_id'] != b['run_id'] and a['initial_source'] and b['final_source'] and
                    min(positions[k] for k in visible_runs[a['run_id']] if k in exact) <
                    min(positions[k] for k in visible_runs[b['run_id']] if k in exact)
                    for a in eligible for b in eligible)
    return {'schema': 'tokenguard.quality-evidence/v1', 'trace_errors': errors,
            'required_command_mentions': len(exact), 'receipts_verified': len(verified),
            'invalid_receipts': invalid, 'test_runs': verified,
            'initial_test_observed': initial, 'final_test_observed': final,
            'agent_test_protocol_satisfied': compliant and not errors and invalid == 0,
            'final_test_command_observed': final and not errors and invalid == 0,
            'native_compressions_in_matching_bash_results': len(set(matched)),
            'receipt_security': 'same-user local evidence; not tamper-proof or an OS sandbox'}


def pair_assessment(baseline: dict, optimized: dict, protocol: str) -> dict:
    """Keep failures/budget exhaustion in the denominator; expose missing evidence."""
    reasons = []
    if baseline.get('not_run') or optimized.get('not_run'):
        reasons.append('pair_incomplete')
    for arm, row in (('baseline', baseline), ('optimized', optimized)):
        if not row.get('grade', {}).get('evaluation_valid'):
            reasons.append(arm + '_independent_grade_invalid')
        if not row.get('source_changes', {}).get('only_allowed_changes'):
            reasons.append(arm + '_protected_source_changed')
    exposure = optimized.get('protocol_evidence', {}).get('native_compressions_in_matching_bash_results', 0) > 0
    if protocol == 'controlled_initial_log':
        seeds = [r.get('seed_delivery') or {} for r in (baseline, optimized)]
        seed_ok = (all(s.get('verified_before_launch') for s in seeds) and
                   seeds[0].get('original_stdout_sha256') == seeds[1].get('original_stdout_sha256') and
                   seeds[1].get('compressed') is True and seeds[0].get('compressed') is False)
        exposure = seed_ok
        if not seed_ok:
            reasons.append('controlled_seed_not_verified')
    else:
        for arm, row in (('baseline', baseline), ('optimized', optimized)):
            if not row.get('protocol_evidence', {}).get('agent_test_protocol_satisfied'):
                reasons.append(arm + '_test_protocol_not_verified')
    if not exposure:
        reasons.append('compression_not_observed')
    if baseline.get('compression', {}).get('compressions', 0):
        reasons.append('baseline_compression_contamination')
    valid = not reasons
    return {'protocol': protocol, 'comparison_evidence_satisfied': valid, 'compression_exposed': exposure,
            'reasons': reasons, 'baseline_code_resolved': baseline.get('grade', {}).get('resolved', False),
            'optimized_code_resolved': optimized.get('grade', {}).get('resolved', False),
            'observed_code_quality_drop': (not optimized.get('grade', {}).get('resolved', False))
            if valid and baseline.get('grade', {}).get('resolved') else 'not_evaluable',
            'general_non_inferiority': 'not_established'}
