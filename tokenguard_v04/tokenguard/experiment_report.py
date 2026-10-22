"""Quality, measured local overhead and optional task usage in separate fields."""
import csv
import json
from pathlib import Path

from . import usage, usage_compare
from .experiment import load_experiment
from .experiment_modes import baseline_mode,hook_mode


def local_pairs(runs: list[dict]) -> list[dict]:
    pairs=[]
    for pair_id in sorted({r['pair_id'] for r in runs}):
        selected={r['arm']:r for r in runs if r['pair_id']==pair_id}
        before,after=selected['baseline'],selected['optimized']
        passed=all(r.get('status')=='completed' and r['task_success'] for r in (before,after))
        metrics={}
        if passed:
            for field in ('elapsed_ms','total_execution_ms','hook_process_count','hook_process_elapsed_ms',
                          'original_stdout_chars','visible_stdout_chars'):
                b,o=before.get(field),after.get(field)
                metrics[field]=({'baseline':b,'optimized':o,'optimized_minus_baseline':o-b}
                                if type(b) is int and type(o) is int else 'unavailable')
        pairs.append({'pair_id':pair_id,'status':'quality_passed' if passed else 'incomplete_or_failed',
                      'metrics':metrics if passed else 'unavailable'})
    return pairs


def report(folder: Path) -> dict:
    record=load_experiment(folder)
    runs=[];bundles=[]
    for trial in record['trials']:
        root=folder/'trials'/trial['trial_id']
        row={**trial,'status':'not_run','task_success':False,'total_task_tokens':'unavailable',
             'baseline_mode':baseline_mode(record['config']),'hook_mode':hook_mode(record,trial),
             'actual_billed_cost':'unavailable','cache_read_tokens':'unavailable',
             'cache_write_tokens':'unavailable','cache_hit_ratio':'unavailable'}
        if (root/'result.json').exists():
            result=usage.load(root/'result.json')
            if any(result[k]!=trial[k] for k in ('trial_id','pair_id','arm','session_id')):
                raise ValueError('Trial result identity differs')
            if result.get('baseline_mode','disabled_hook')!=baseline_mode(record['config']):
                raise ValueError('Trial baseline mode differs')
            if result.get('hook_mode',hook_mode(record,trial))!=hook_mode(record,trial):
                raise ValueError('Trial Hook mode differs')
            row.update(result,status='completed')
        if (root/'measurement.json').exists():
            bundle=usage.load(root/'measurement.json')
            expected={**{k:record[k] for k in ('experiment_id','prompt_sha256','initial_snapshot_sha256','config_sha256')},
                      **{k:row[k] for k in ('session_id','arm','pair_id','agent_version','task_success','tests_passed','tests_unchanged')},
                      'model':record['task']['model'],'provider':record['task']['provider']}
            if bundle['manifest'].get('baseline_mode','disabled_hook')!=baseline_mode(record['config']):
                raise ValueError('Attached usage baseline mode differs')
            if 'baseline_mode' not in bundle['manifest']:
                # Legacy task protocol is known; generic imported usage remains unspecified.
                bundle={**bundle,'manifest':{**bundle['manifest'],'baseline_mode':baseline_mode(record['config'])}}
            if any(bundle['manifest'].get(k)!=v for k,v in expected.items()):
                raise ValueError('Attached usage identity/quality differs from run')
            bundle=usage_compare._checked(bundle)
            bundles.append(bundle)
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
            'successful_trials':sum(r['task_success'] for r in runs),'runs':runs,'usage_comparison':comparison,
            'actual_task_savings_claim':'not_established',
            'local_wall_clock_pairs':local_pairs(runs),
            'limitations':['mock agents are fixtures, not model-quality evidence',
                           'directories/processes are not an OS sandbox',
                           'provider routing and hidden/global/managed customizations are not certified identical',
                           'usage coverage/authenticity is caller-declared, not independently observed',
                           'Claude CLI cost estimates are not provider billing',
                           'Hook process time includes startup, imports, compression and local I/O; mock adapter only',
                           'no_hook omits runner-injected Hook; inherited user/managed hooks are not disabled',
                           'controller validation time is excluded from agent elapsed_ms']}
    (folder/'report.json').write_text(json.dumps(result,indent=2,ensure_ascii=True)+'\n',encoding='utf-8')
    fields=['trial_id','arm','baseline_mode','hook_mode','status','task_success','tests_passed','tests_unchanged','elapsed_ms','total_execution_ms','tool_calls',
            'hook_process_count','hook_process_elapsed_ms','hook_errors','original_stdout_chars','visible_stdout_chars',
            'bash_hook_invocations','compressions','saved_chars','recovery_count','total_task_tokens',
            'cache_read_tokens','cache_write_tokens','cache_hit_ratio','actual_billed_cost','usage_evidence_kind']
    with (folder/'report.csv').open('w',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
        for run in runs:
            flat={**run,**run.get('compression',{})}
            writer.writerow({k:json.dumps(flat[k],ensure_ascii=True) if isinstance(flat.get(k),dict) else flat.get(k,'unavailable') for k in fields})
    lines=['# Paired task experiment','',f"Task: {record['task']['task_id']}; requested model: {record['task']['model']}.",'',
           f"Baseline mode: {result['baseline_mode']}; optimized mode: enabled_hook.",'',
           'Mock fixture runs do not establish model quality or real task token/cost savings.',
           'Total tokens, cache and billing remain unavailable until explicit, complete usage is attached.','',
           '|Trial|Hook mode|Task success|Tests passed|Agent ms|Hook processes|Hook ms|Compressions|Recoveries|Task tokens|Usage evidence|',
           '|---|---|---|---|---:|---:|---:|---:|---:|---:|---|']
    for row in runs:
        stats=row.get('compression',{})
        lines.append(f"|{row['trial_id']}|{row['hook_mode']}|{row['task_success']}|{row.get('tests_passed','unavailable')}|{row.get('elapsed_ms','unavailable')}|{row.get('hook_process_count','unavailable')}|{row.get('hook_process_elapsed_ms','unavailable')}|{stats.get('compressions','unavailable')}|{stats.get('recovery_count','unavailable')}|{row['total_task_tokens']}|{row.get('usage_evidence_kind','unavailable')}|")
    lines.extend(['','Limitations:',*('- '+line for line in result['limitations'])])
    (folder/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    if isinstance(comparison,dict):
        usage_compare.export(comparison,folder/'usage-comparisons')
    return result
