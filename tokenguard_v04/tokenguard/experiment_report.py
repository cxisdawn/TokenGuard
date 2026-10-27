"""Quality, measured local overhead and optional task usage in separate fields."""
import csv
import json
from pathlib import Path

from . import audit, usage, usage_compare
from .experiment import audit_scope, load_experiment
from .experiment_modes import baseline_mode,hook_mode
from .experiment_snapshot import changes, snapshot


LOCAL_METRICS = ('elapsed_ms', 'total_execution_ms', 'hook_process_count', 'hook_process_elapsed_ms',
                 'original_stdout_chars', 'visible_stdout_chars', 'hook_errors', 'tool_calls')


def _count(value: object) -> bool:
    return type(value) is int and value >= 0


def _checked_result(root: Path, record: dict, result: dict) -> dict:
    """Check local controller records without promoting missing evidence to success."""
    row = dict(result)
    errors = []
    for key in LOCAL_METRICS:
        if key in row and row[key] not in (None, 'unavailable') and not _count(row[key]):
            row[key] = None
            errors.append(key + ':invalid_measurement')
    cached = row.get('compression')
    audit_complete = isinstance(cached, dict) and cached.get('data_available') is True
    try:
        with audit_scope(root / 'tg-data'):
            current = audit.summary(row['session_id'], record['experiment_id'], record_limit=0)
            diagnostics = audit.diagnostics(row['session_id'], record['experiment_id'], limit=1)
        audit_complete = audit_complete and current['data_available'] is True and diagnostics['data_available'] is True
        if isinstance(cached, dict):
            for key in ('compressions', 'recovery_count', 'original_chars', 'optimized_chars', 'saved_chars',
                        'returned_chars', 'scanned_chars'):
                if (key not in cached or type(cached[key]) is not type(current[key]) or cached[key] != current[key]):
                    audit_complete = False
    except (OSError, ValueError, TypeError, KeyError):
        audit_complete = False
    if not audit_complete:
        errors.append('compression_evidence_unavailable_or_inconsistent')
    row['compression_data_available'] = audit_complete
    row['compression'] = {**(cached if isinstance(cached, dict) else {}), 'data_available': audit_complete}
    before = row.get('initial_validation')
    after = row.get('final_validation')
    source = row.get('source_changes')
    before = before if isinstance(before, dict) else {}
    after = after if isinstance(after, dict) else {}
    source = source if isinstance(source, dict) else {}
    source_binding = False
    try:
        current_check = changes(root / 'worktree', record['files_sha256'], record['task']['editable_files'])
        _, current_hashes = snapshot(root / 'worktree', record['task']['files'])
        source_binding = (current_check['only_allowed_changes'] is True and
                          isinstance(after.get('validated_files_sha256'), dict) and
                          current_hashes == after['validated_files_sha256'] and
                          after.get('validated_source_unchanged') is True)
    except (OSError, ValueError, TypeError, KeyError):
        pass
    row['current_source_binding_verified'] = source_binding
    if not source_binding:
        errors.append('current_candidate_validation_binding_unavailable')
    initial_ok = before.get('passed') is True
    if record['task']['expect_initial_failure']:
        initial_ok = (type(before.get('exit_code')) is int and before['exit_code'] == 1 and
                      _count(before.get('failures')) and before['failures'] > 0 and
                      type(before.get('errors')) is int and before['errors'] == 0 and
                      before.get('timed_out') is False and before.get('outputs_truncated') is False)
    final_ok = (after.get('passed') is True and type(after.get('exit_code')) is int and after['exit_code'] == 0 and
                after.get('timed_out') is False and after.get('outputs_truncated') is False and
                after.get('uses_pristine_protected_files') is True and
                all(_count(after.get(key)) for key in ('tests', 'failures', 'errors', 'skipped')) and
                after['tests'] > after['skipped'] and after['failures'] == after['errors'] == 0 and
                (record['task']['expected_tests'] is None or
                 (after['tests'] == record['task']['expected_tests'] and after['skipped'] == 0)))
    prerequisite = (row.get('tests_passed') is True and row.get('tests_unchanged') is True and
                    source.get('only_allowed_changes') is True and initial_ok and final_ok and
                    type(row.get('agent_exit_code')) is int and row['agent_exit_code'] == 0 and
                    row.get('timed_out') is False and row.get('execution_error') is None and
                    isinstance(row.get('stage_errors'), list) and not row['stage_errors'] and audit_complete and source_binding)
    claimed = row.get('task_success')
    known_failure = (claimed is False or row.get('tests_passed') is False or row.get('tests_unchanged') is False or
                     source.get('only_allowed_changes') is False or after.get('passed') is False or
                     (type(row.get('agent_exit_code')) is int and row['agent_exit_code'] != 0) or
                     row.get('timed_out') is True or bool(row.get('execution_error')) or
                     (isinstance(row.get('stage_errors'), list) and bool(row['stage_errors'])))
    if claimed is True and (not prerequisite or errors):
        errors.append('claimed_success_not_supported_by_controller_evidence')
    row['reported_task_success'] = claimed if type(claimed) is bool else None
    row['task_success'] = (None if not source_binding else False if known_failure else
                           True if claimed is True and prerequisite and not errors else None)
    row['quality_evidence_valid'] = prerequisite and not errors
    row['report_evidence_errors'] = errors
    return row


def local_pairs(runs: list[dict]) -> list[dict]:
    pairs=[]
    for pair_id in sorted({r['pair_id'] for r in runs}):
        selected={r['arm']:r for r in runs if r['pair_id']==pair_id}
        before,after=selected['baseline'],selected['optimized']
        passed=all(r.get('status')=='completed' and r.get('task_success') is True and
                   r.get('quality_evidence_valid') is True for r in (before,after))
        metrics={}
        if passed:
            for field in ('elapsed_ms','total_execution_ms','hook_process_count','hook_process_elapsed_ms',
                          'original_stdout_chars','visible_stdout_chars'):
                b,o=before.get(field),after.get(field)
                metrics[field]=({'baseline':b,'optimized':o,'optimized_minus_baseline':o-b}
                                if _count(b) and _count(o) else 'unavailable')
        pairs.append({'pair_id':pair_id,'status':'quality_passed' if passed else 'incomplete_or_failed',
                      'metrics':metrics if passed else 'unavailable'})
    return pairs


def report(folder: Path) -> dict:
    record=load_experiment(folder)
    runs=[];bundles=[]
    for trial in record['trials']:
        root=folder/'trials'/trial['trial_id']
        row={**trial,'status':'not_run','task_success':None,'compression_data_available':False,
             'quality_evidence_valid':False,'report_evidence_errors':[], 'total_task_tokens':'unavailable',
             'baseline_mode':baseline_mode(record['config']),'hook_mode':hook_mode(record,trial),
             'actual_billed_cost':'unavailable','cache_read_tokens':'unavailable',
             'cache_write_tokens':'unavailable','cache_hit_ratio':'unavailable'}
        if (root/'result.json').exists():
            try:
                result=usage.load(root/'result.json')
                if not isinstance(result, dict) or not all(key in result for key in ('trial_id', 'pair_id', 'arm', 'session_id')):
                    raise ValueError('Trial result must be an object')
            except (OSError, ValueError, TypeError) as exc:
                row.update(status='evidence_invalid', report_evidence_errors=['result.json:' + type(exc).__name__])
                runs.append(row)
                continue
            if any(result[k]!=trial[k] for k in ('trial_id','pair_id','arm','session_id')):
                raise ValueError('Trial result identity differs')
            if result.get('baseline_mode','disabled_hook')!=baseline_mode(record['config']):
                raise ValueError('Trial baseline mode differs')
            if result.get('hook_mode',hook_mode(record,trial))!=hook_mode(record,trial):
                raise ValueError('Trial Hook mode differs')
            row.update(_checked_result(root, record, result),status='completed')
        if (root/'measurement.json').exists():
            bundle=usage.load(root/'measurement.json')
            expected={**{k:record[k] for k in ('experiment_id','prompt_sha256','initial_snapshot_sha256','config_sha256')},
                      **{k:row[k] for k in ('session_id','arm','pair_id','agent_version','task_success','tests_passed','tests_unchanged')},
                      'model':record['task']['model'],'provider':record['task']['provider']}
            expected['task_success'] = row.get('reported_task_success')
            if bundle['manifest'].get('baseline_mode','disabled_hook')!=baseline_mode(record['config']):
                raise ValueError('Attached usage baseline mode differs')
            if 'baseline_mode' not in bundle['manifest']:
                # Legacy task protocol is known; generic imported usage remains unspecified.
                bundle={**bundle,'manifest':{**bundle['manifest'],'baseline_mode':baseline_mode(record['config'])}}
            if any(bundle['manifest'].get(k)!=v for k,v in expected.items()):
                raise ValueError('Attached usage identity/quality differs from run')
            bundle=usage_compare._checked(bundle)
            if not row['report_evidence_errors']:
                bundles.append(bundle)
            else:
                row['usage_comparison_exclusion'] = 'local_run_evidence_invalid'
            totals=bundle['complete_task_totals']
            row.update(usage_evidence_kind=bundle['manifest']['evidence_kind'],usage_coverage=bundle['manifest']['coverage'],
                       reported_billing=bundle['reported_billing'])
            if isinstance(totals,dict):
                cache=(totals['cache_read_tokens']/totals['input_tokens']
                       if isinstance(totals['cache_read_tokens'],int) and isinstance(totals['input_tokens'],int)
                       and totals['input_tokens']>0 else 'unavailable')
                billed=(bundle['reported_billing'] if bundle['manifest']['evidence_kind']=='provider_export'
                        and isinstance(bundle['reported_billing'],dict) else 'unavailable')
                row.update(total_task_tokens=totals['total_tokens'],cache_read_tokens=totals['cache_read_tokens'],
                           cache_write_tokens=totals['cache_write_tokens'],cache_hit_ratio=cache,
                           actual_billed_cost=billed)
        runs.append(row)
    comparison=usage_compare.compare(bundles) if len(bundles)>=2 else 'unavailable'
    result={'schema':'tokenguard.task-report/v1','experiment_id':record['experiment_id'],
            'baseline_mode':baseline_mode(record['config']),
            'task_id':record['task']['task_id'],'requested_model':record['task']['model'],
            'prepared_trials':len(runs),'completed_trials':sum(r['status']=='completed' for r in runs),
            'successful_trials':sum(r['task_success'] is True for r in runs),
            'unknown_task_outcomes':sum(r['task_success'] is None for r in runs),
            'runs':runs,'usage_comparison':comparison,
            'actual_task_savings_claim':'not_established',
            'local_wall_clock_pairs':local_pairs(runs),
            'limitations':['mock agents are fixtures, not model-quality evidence',
                           'directories/processes are not an OS sandbox',
                           'provider routing and hidden/global/managed customizations are not certified identical',
                           'usage coverage/authenticity is caller-declared, not independently observed',
                           'Claude CLI cost estimates are not provider billing',
                           'Hook process time includes startup, imports, compression and local I/O; mock adapter only',
                           'no_hook omits runner-injected Hook; inherited user/managed hooks are not disabled',
                           'controller validation time is excluded from agent elapsed_ms',
                           'task success requires consistent local controller records and current audit evidence; it is not tamper-proof attestation',
                           'unknown or malformed outcomes remain in planned denominators and are not measured failures']}
    (folder/'report.json').write_text(json.dumps(result,indent=2,ensure_ascii=True)+'\n',encoding='utf-8')
    fields=['trial_id','arm','baseline_mode','hook_mode','status','task_success','tests_passed','tests_unchanged','elapsed_ms','total_execution_ms','tool_calls',
            'hook_process_count','hook_process_elapsed_ms','hook_errors','original_stdout_chars','visible_stdout_chars',
            'bash_hook_invocations','compressions','saved_chars','recovery_count','total_task_tokens',
            'cache_read_tokens','cache_write_tokens','cache_hit_ratio','actual_billed_cost','usage_evidence_kind',
            'compression_data_available','execution_status','stage_errors','reported_task_success',
            'quality_evidence_valid','current_source_binding_verified','report_evidence_errors']
    with (folder/'report.csv').open('w',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
        for run in runs:
            flat={**run,**run.get('compression',{})}
            flat['compression_data_available'] = run['compression_data_available']
            writer.writerow({k:json.dumps(flat[k],ensure_ascii=True) if isinstance(flat.get(k),dict) else flat.get(k,'unavailable') for k in fields})
    lines=['# Paired task experiment','',f"Task: {record['task']['task_id']}; requested model: {record['task']['model']}.",'',
           f"Baseline mode: {result['baseline_mode']}; optimized mode: enabled_hook.",'',
           'Mock fixture runs do not establish model quality or real task token/cost savings.',
           'Total tokens, cache and billing remain unavailable until explicit, complete usage is attached.','',
           'When audit completeness is false, counts are partial observations or unavailable; they are not full totals.','',
           '|Trial|Hook mode|Task success|Tests passed|Agent ms|Hook processes|Hook ms|Compressions|Recoveries|Audit complete|Task tokens|Usage evidence|',
           '|---|---|---|---|---:|---:|---:|---:|---:|---|---:|---|']
    for row in runs:
        stats=row.get('compression',{})
        complete = row['compression_data_available']
        lines.append(f"|{row['trial_id']}|{row['hook_mode']}|{row['task_success']}|{row.get('tests_passed','unavailable')}|{row.get('elapsed_ms','unavailable')}|{row.get('hook_process_count','unavailable')}|{row.get('hook_process_elapsed_ms','unavailable')}|{stats.get('compressions','unavailable')}|{stats.get('recovery_count','unavailable')}|{complete}|{row['total_task_tokens']}|{row.get('usage_evidence_kind','unavailable')}|")
    lines.extend(['','Limitations:',*('- '+line for line in result['limitations'])])
    (folder/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    if isinstance(comparison,dict):
        usage_compare.export(comparison,folder/'usage-comparisons')
    return result
