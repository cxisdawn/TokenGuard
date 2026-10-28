"""Complete planned denominators and bounded, validated offline fixture reports."""
import csv
import json
import math
from pathlib import Path
import re
import statistics


COUNTS = ('tool_calls', 'original_tool_chars', 'visible_tool_chars', 'estimated_original_tool_tokens',
          'estimated_visible_tool_tokens', 'compressions', 'recovery_count', 'recovered_chars',
          'indexed_recoveries', 'index_build_bytes', 'recovery_scanned_chars', 'integrity_build_bytes', 'integrity_checked_bytes')
OPTIONAL = ('compressions', 'recovery_count', 'recovered_chars', 'indexed_recoveries',
            'index_build_bytes', 'recovery_scanned_chars', 'integrity_build_bytes', 'integrity_checked_bytes')
UNKNOWN_USAGE = ('total_task_tokens', 'actual_api_input_tokens', 'actual_api_output_tokens',
                 'cache_read_tokens', 'cache_write_tokens', 'cache_hit_ratio', 'actual_billed_cost', 'api_latency_ms')
HASH = re.compile(r'[0-9a-f]{64}')


def unrun(config: dict) -> dict:
    return {**config, 'mode': 'offline_mock', 'status': 'not_run', 'success': None,
            'failure_details_preserved': None, 'failure_recovery_verified': None,
            'initial_snapshot_sha256': None, 'final_code_sha256': None,
            'paired_final_code_equal': None, 'comparison_eligible': False,
            'audit_complete': None, 'observed_audit': None, 'failure_reason': None,
            **dict.fromkeys(COUNTS), 'recovery_latency_ms': None, 'elapsed_ms': None,
            'estimated_tool_token_reduction_ratio': None, 'estimate_method': None,
            **dict.fromkeys(UNKNOWN_USAGE, 'unavailable')}


def checked_worker(raw: object, config: dict, snapshot: str, final: str) -> dict:
    if not isinstance(raw, dict) or any(type(raw.get(key)) is not type(value) or raw[key] != value
                                        for key,value in config.items()):
        raise ValueError('Worker identity differs from plan')
    for key in ('success', 'failure_details_preserved', 'audit_complete'):
        if type(raw.get(key)) is not bool:
            raise ValueError('Worker boolean missing or invalid')
    for key in ('initial_snapshot_sha256', 'final_code_sha256'):
        if not isinstance(raw.get(key), str) or not HASH.fullmatch(raw[key]):
            raise ValueError('Worker snapshot hash invalid')
    if raw['initial_snapshot_sha256'] != snapshot or raw['final_code_sha256'] != final:
        raise ValueError('Worker source hashes disagree with controller')
    for key in COUNTS:
        value = raw.get(key)
        if key in OPTIONAL and value is None:
            continue
        if type(value) is not int or not 0 <= value <= 2**63-1:
            raise ValueError('Worker measurement missing or invalid')
    value = raw.get('recovery_latency_ms')
    if value is not None and (type(value) not in (int, float) or not 0 <= value <= 2**63-1 or not math.isfinite(value)):
        raise ValueError('Worker recovery latency invalid')
    if raw.get('failure_recovery_verified') is not None and type(raw['failure_recovery_verified']) is not bool:
        raise ValueError('Worker recovery outcome invalid')
    if raw['audit_complete'] and any(raw.get(key) is None for key in ('compressions', 'recovery_count', 'recovered_chars')):
        raise ValueError('Complete audit lacks required counts')
    if (not isinstance(raw.get('observed_audit'), dict) or
            raw['observed_audit'].get('data_available') is not raw['audit_complete']):
        raise ValueError('Worker audit completeness differs')
    if raw['audit_complete']:
        audit_fields = {'compressions': 'compressions', 'recovery_count': 'recovery_count',
                        'recovered_chars': 'returned_chars', 'indexed_recoveries': 'indexed_recoveries',
                        'index_build_bytes': 'index_build_bytes', 'recovery_scanned_chars': 'scanned_chars',
                        'integrity_build_bytes': 'integrity_build_bytes', 'integrity_checked_bytes': 'integrity_checked_bytes',
                        'recovery_latency_ms': 'recovery_latency_ms'}
        for field, observed in audit_fields.items():
            actual = raw['observed_audit'].get(observed)
            if type(raw.get(field)) is not type(actual) or raw.get(field) != actual:
                raise ValueError('Worker measurement differs from audit observation')
    row = unrun(config)
    # Whitelist fields; arbitrary worker extras cannot enter a claimed measurement.
    for key in (*COUNTS, 'success', 'failure_details_preserved', 'failure_recovery_verified',
                'initial_snapshot_sha256', 'final_code_sha256', 'audit_complete',
                'observed_audit', 'recovery_latency_ms'):
        row[key] = raw.get(key)
    original, visible = row['estimated_original_tool_tokens'], row['estimated_visible_tool_tokens']
    row['estimated_tool_token_reduction_ratio'] = 1-visible/original if original else None
    row['estimate_method'] = raw.get('estimate_method') if isinstance(raw.get('estimate_method'), str) else 'unavailable'
    row['status'] = 'completed' if row['audit_complete'] else 'evidence_incomplete'
    if not row['audit_complete']:
        row.update({key: None for key in OPTIONAL})
        row['recovery_latency_ms'] = None
        row['failure_reason'] = 'AuditIntegrityError'
    # Strict serialization protects nested observations too.
    json.dumps(row, allow_nan=False)
    return row


def export(folder: Path, experiment: str, prompt: str, rows: list[dict], cases: tuple[str, ...], rounds: int) -> dict:
    groups = {}
    for row in rows:
        groups.setdefault((row['case'], row['round']), {})[row['variant']] = row
    for pair in groups.values():
        before, after = pair['baseline'], pair['optimized']
        complete = all(row['status'] == 'completed' for row in (before, after))
        equal = before['final_code_sha256'] == after['final_code_sha256'] if complete else None
        eligible = (complete and equal is True and before['initial_snapshot_sha256'] == after['initial_snapshot_sha256']
                    and all(row['success'] is True and row['failure_details_preserved'] is True
                            and (row['case'] != 'large_artifact_recovery' or row['failure_recovery_verified'] is True)
                            for row in (before, after)))
        for row in (before, after):
            row['paired_final_code_equal'], row['comparison_eligible'] = equal, eligible
    aggregate = []
    for case in cases:
        baseline = [r for r in rows if r['case'] == case and r['variant'] == 'baseline']
        optimized = [r for r in rows if r['case'] == case and r['variant'] == 'optimized']
        eligible = sum(r['comparison_eligible'] for r in optimized)
        base_chars = sum(r['visible_tool_chars'] for r in baseline) if eligible == rounds else None
        opt_chars = sum(r['visible_tool_chars'] for r in optimized) if eligible == rounds else None
        def timing(own: list[dict], stddev: bool = False):
            values = [r['elapsed_ms'] for r in own if r['status'] == 'completed' and r['elapsed_ms'] is not None]
            return ((statistics.pstdev(values) if stddev else statistics.mean(values))
                    if len(values) == rounds else None)
        aggregate.append({'case': case, 'pairs': rounds, 'eligible_pairs': eligible,
            'baseline_successes': sum(r['success'] is True for r in baseline),
            'optimized_successes': sum(r['success'] is True for r in optimized),
            'unknown_outcomes': sum(r['success'] is None for r in (*baseline, *optimized)),
            'mean_character_reduction_after_recovery': 1-opt_chars/base_chars if base_chars else None,
            'baseline_mean_ms': timing(baseline), 'optimized_mean_ms': timing(optimized),
            'optimized_time_stddev_ms': timing(optimized, True)})
    result = {'schema': 'tokenguard.offline-mock/v2', 'experiment_id': experiment, 'mode': 'offline_mock',
              'prompt': prompt, 'planned_trials': len(rows),
              'attempted_trials': sum(r['status'] != 'not_run' for r in rows),
              'completed_trials': sum(r['status'] == 'completed' for r in rows),
              'aggregate': aggregate, 'rows': rows, 'model_api_calls': 0,
              'estimate_scope': 'within-arm raw/visible tool outputs including recovery; not paired task tokens',
              'comparison_scope': 'all planned pairs must pass task and evidence checks; no success-only subset',
              'baseline_order': 'AB on odd rounds, BA on even rounds',
              'delivery_scope': 'in-process fixture handle; does not measure native hook pipe/host delivery'}
    (folder/'report.json').write_text(json.dumps(result, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    def shown(value):
        return 'unavailable' if value is None else json.dumps(value, allow_nan=False) if isinstance(value, (dict, list)) else value
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with (folder/'report.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: shown(row.get(key)) for key in fields} for row in rows)
    lines = ['# TokenGuard offline mock A/B', '',
             'Deterministic local fixtures; no model or billing experiment.',
             'All planned trials remain visible. Missing or incomplete evidence is unavailable.', '',
             '|Case|Round|Arm|Status|Task check|Tool chars|Recoveries|Audit complete|',
             '|---|---:|---|---|---|---:|---:|---|']
    for row in rows:
        lines.append('|'+'|'.join(str(shown(row.get(key))) for key in
                     ('case','round','variant','status','success','visible_tool_chars','recovery_count','audit_complete'))+'|')
    lines += ['', 'Recovery I/O is separate from returned characters. Full integrity builds read the entire artifact;',
              'block checks reread selected complete blocks. These counters exclude sidecar I/O and OS cache effects.', '',
              '|Case|Round|Arm|Scan chars|Index build bytes|Full integrity bytes|Block-check bytes|',
              '|---|---:|---|---:|---:|---:|---:|']
    for row in rows:
        lines.append('|'+'|'.join(str(shown(row.get(key))) for key in
                     ('case','round','variant','recovery_scanned_chars','index_build_bytes',
                      'integrity_build_bytes','integrity_checked_bytes'))+'|')
    lines += ['', 'Paired character changes include selective recovery; task/API tokens and billed costs are unavailable.', '']
    for row in aggregate:
        ratio = row['mean_character_reduction_after_recovery']
        lines.append(f"- {row['case']}: {format(ratio, '.2%') if ratio is not None else 'unavailable'} character reduction; "
                     f"{row['eligible_pairs']}/{rounds} eligible planned pairs.")
    lines += ['', 'Timing includes local startup and I/O; failures and incomplete coverage cannot establish speedup.',
              'Per-arm heuristic estimates include recovery in both raw and visible totals; use paired character comparison for the shared baseline.',
              'Fixture edits are scripted. AI coding quality, host acceptance and actual costs are unmeasured.', '']
    (folder/'report.md').write_text('\n'.join(lines), encoding='utf-8')
    return result
