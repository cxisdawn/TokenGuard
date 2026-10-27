"""Local fixture study: each baseline gets independent paired rounds; no models."""
import argparse
import csv
import json
from pathlib import Path
import statistics
import sys
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from tokenguard.experiment import BASE,execute,prepare
from tokenguard.experiment_modes import BASELINE_MODES
from tokenguard.experiment_report import report


def run(output: Path, rounds: int) -> dict:
    if type(rounds) is not int or not 1<=rounds<=20:
        raise ValueError('rounds must be 1..20')
    folder=output.resolve()/('baseline-mode-demo-'+uuid.uuid4().hex)
    folder.mkdir(parents=True,exist_ok=False)
    rows=[];experiments=[];errors=[];stopped=False
    planned=[{'baseline_mode':mode, 'arm':arm, 'round':repeat}
             for mode in BASELINE_MODES for arm in ('baseline','optimized') for repeat in range(1,rounds+1)]
    (folder/'plan.json').write_text(json.dumps({'trials':planned, 'model_api_calls':0},indent=2),encoding='utf-8')
    for mode in BASELINE_MODES:
        planned_runs=[{**r, 'status':'not_run', 'task_success':None} for r in planned if r['baseline_mode']==mode]
        assessed={'runs':planned_runs}
        if not stopped:
            stage='prepare'
            try:
                # Sibling experiment directories avoid a long Windows path level.
                experiment=prepare(BASE/'benchmark'/'fixture',BASE/'benchmark'/'task_fixture.json',folder.parent,rounds,mode)
                experiments.append(str(experiment))
                stage='execute'
                try:
                    execute(experiment,execute_run=True)
                except Exception as exc:
                    errors.append({'baseline_mode':mode,'stage':stage,'error_type':type(exc).__name__})
                    stopped=True
                stage='report'
                measured_report=report(experiment)
                measured_runs=measured_report.get('runs')
                if (not isinstance(measured_runs,list) or len(measured_runs)!=rounds*2
                        or any(not isinstance(r,dict) for r in measured_runs)
                        or any(sum(r.get('arm')==arm for r in measured_runs)!=rounds for arm in ('baseline','optimized'))):
                    raise ValueError('Report differs from planned trial count')
                assessed=measured_report
                if any(r.get('status')!='completed' for r in measured_runs):
                    stopped=True
            except Exception as exc:
                errors.append({'baseline_mode':mode,'stage':stage,'error_type':type(exc).__name__})
                planned_runs[0]['status']='infrastructure_failed'
                stopped=True
        for arm in ('baseline','optimized'):
            selected=[r for r in assessed['runs'] if r['arm']==arm]
            valid=[r for r in selected if r.get('task_success') is True]
            coverage = {}
            def measured(field: str, *, audit_metric: bool = False, median: bool = False):
                values = []
                for run in selected:
                    stats = run.get('compression')
                    stats = stats if isinstance(stats, dict) else {}
                    value = stats.get(field) if audit_metric else run.get(field)
                    if (run.get('status') == 'completed' and type(value) is int and 0 <= value <= 2**63-1
                            and (not audit_metric or stats.get('data_available') is True)):
                        values.append(value)
                coverage[field] = {'observed_trials': len(values), 'planned_trials': len(selected)}
                if not values or len(values) != len(selected):
                    return None
                return statistics.median(values) if median else sum(values)
            row={'baseline_mode':mode,'arm':arm,'trials':len(selected),'successful_trials':len(valid),
                 'not_run_trials':sum(r.get('status') == 'not_run' for r in selected),
                 'unknown_outcomes':sum(r.get('task_success') is None for r in selected),
                 'hook_process_count':measured('hook_process_count'),
                 'hook_process_elapsed_ms':measured('hook_process_elapsed_ms'),
                 'median_agent_elapsed_ms':measured('elapsed_ms', median=True),
                 'compressions':measured('compressions', audit_metric=True),
                 'saved_chars':measured('saved_chars', audit_metric=True),
                 'measurement_coverage':coverage,
                 'task_tokens':'unavailable','actual_billed_cost':'unavailable'}
            rows.append(row)
    result={'schema':'tokenguard.baseline-demo/v1','driver':'mock','rounds_per_baseline':rounds,
            'trials':sum(r['trials'] for r in rows),'successful_trials':sum(r['successful_trials'] for r in rows),
            'model_api_calls':0,'summary':rows,'experiments':experiments,'report_folder':str(folder),
            'execution_errors':errors,
            'measurement_scope':'local fixture adapter; not model quality, API tokens or cost',
            'aggregation_scope':'all planned trials including failures and unrun trials; incomplete measurements remain unavailable',
            'timing_caution':'descriptive only; baseline modes run in batches, no significance or speedup claim',
            'actual_task_savings_claim':'not_established'}
    (folder/'report.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    with (folder/'report.csv').open('w',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader()
        for row in rows:
            writer.writerow({key: ('unavailable' if value is None else json.dumps(value) if isinstance(value, dict) else value)
                             for key,value in row.items()})
    lines=['# Local baseline mode study','',result['measurement_scope'],result['timing_caution'],'',
           '|Baseline mode|Arm|Successes/trials|Hook processes|Hook ms total|Median Agent ms|Compressions|Saved chars|',
           '|---|---|---:|---:|---:|---:|---:|---:|']
    for row in rows:
        shown={key:'unavailable' if value is None else value for key,value in row.items()}
        lines.append(f"|{shown['baseline_mode']}|{shown['arm']}|{shown['successful_trials']}/{shown['trials']}|{shown['hook_process_count']}|{shown['hook_process_elapsed_ms']}|{shown['median_agent_elapsed_ms']}|{shown['compressions']}|{shown['saved_chars']}|")
    lines.extend(['','Hook ms includes process startup, imports, compression and local I/O.',
                  'Task API tokens and actual billing: unavailable. Model API calls: 0.'])
    (folder/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return result


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=BASE/'reports'/'baseline-modes')
    parser.add_argument('--rounds',type=int,default=3)
    args=parser.parse_args()
    if not 1<=args.rounds<=20:parser.error('rounds must be 1..20')
    result=run(args.output,args.rounds)
    print(json.dumps({k:v for k,v in result.items() if k not in ('summary','experiments')},indent=2))
    return 0 if not result['execution_errors'] and result['trials']==result['successful_trials'] else 1


if __name__=='__main__':
    raise SystemExit(main())
