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
    rows=[];experiments=[]
    for mode in BASELINE_MODES:
        # Keep experiments beside the study report to avoid adding a long Windows path level.
        experiment=prepare(BASE/'benchmark'/'fixture',BASE/'benchmark'/'task_fixture.json',folder.parent,rounds,mode)
        execute(experiment,execute_run=True)
        assessed=report(experiment)
        experiments.append(str(experiment))
        for arm in ('baseline','optimized'):
            selected=[r for r in assessed['runs'] if r['arm']==arm]
            valid=[r for r in selected if r['task_success']]
            def measured_total(field: str):
                values=[r.get(field) for r in selected]
                return sum(values) if all(type(v) is int for v in values) else 'unavailable'
            elapsed=[r.get('elapsed_ms') for r in selected]
            row={'baseline_mode':mode,'arm':arm,'trials':len(selected),'successful_trials':len(valid),
                 'hook_process_count':measured_total('hook_process_count'),
                 'hook_process_elapsed_ms':measured_total('hook_process_elapsed_ms'),
                 'median_agent_elapsed_ms':statistics.median(elapsed) if all(type(v) is int for v in elapsed) else 'unavailable',
                 'compressions':sum(r['compression']['compressions'] for r in selected),
                 'saved_chars':sum(r['compression']['saved_chars'] for r in selected),
                 'task_tokens':'unavailable','actual_billed_cost':'unavailable'}
            rows.append(row)
    result={'schema':'tokenguard.baseline-demo/v1','driver':'mock','rounds_per_baseline':rounds,
            'trials':sum(r['trials'] for r in rows),'successful_trials':sum(r['successful_trials'] for r in rows),
            'model_api_calls':0,'summary':rows,'experiments':experiments,'report_folder':str(folder),
            'measurement_scope':'local fixture adapter; not model quality, API tokens or cost',
            'aggregation_scope':'all completed runs including failures; missing measurements remain unavailable',
            'timing_caution':'descriptive only; baseline modes run in batches, no significance or speedup claim',
            'actual_task_savings_claim':'not_established'}
    (folder/'report.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    with (folder/'report.csv').open('w',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    lines=['# Local baseline mode study','',result['measurement_scope'],result['timing_caution'],'',
           '|Baseline mode|Arm|Successes/trials|Hook processes|Hook ms total|Median Agent ms|Compressions|Saved chars|',
           '|---|---|---:|---:|---:|---:|---:|---:|']
    for row in rows:
        lines.append(f"|{row['baseline_mode']}|{row['arm']}|{row['successful_trials']}/{row['trials']}|{row['hook_process_count']}|{row['hook_process_elapsed_ms']}|{row['median_agent_elapsed_ms']}|{row['compressions']}|{row['saved_chars']}|")
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
    return 0 if result['trials']==result['successful_trials'] else 1


if __name__=='__main__':
    raise SystemExit(main())
