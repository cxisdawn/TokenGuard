"""Prepare, preview, execute and assess paired task experiments. Offline by default."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from tokenguard.experiment import attach_usage,execute,prepare
from tokenguard.experiment_report import report
from tokenguard.experiment_preflight import check as preflight


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    prep=sub.add_parser('prepare')
    prep.add_argument('--project',type=Path,required=True)
    prep.add_argument('--task',type=Path,required=True)
    prep.add_argument('--output',type=Path,required=True)
    prep.add_argument('--rounds',type=int,default=1)
    run=sub.add_parser('run')
    run.add_argument('experiment',type=Path)
    run.add_argument('--driver',choices=['mock','claude'],default='mock')
    run.add_argument('--execute',action='store_true')
    run.add_argument('--allow-paid',action='store_true')
    run.add_argument('--max-paid-sessions',type=int,default=2)
    run.add_argument('--claude',default='claude')
    show=sub.add_parser('report');show.add_argument('experiment',type=Path)
    probe=sub.add_parser('preflight');probe.add_argument('experiment',type=Path)
    probe.add_argument('--claude',default='claude')
    attach=sub.add_parser('attach-usage');attach.add_argument('experiment',type=Path)
    attach.add_argument('--trial',required=True);attach.add_argument('--source',required=True,type=Path)
    attach.add_argument('--format',choices=['deepseek','anthropic'],required=True)
    attach.add_argument('--evidence',choices=['synthetic','provider_export','gateway_export','manual'],required=True)
    attach.add_argument('--coverage',choices=['complete_task','partial'],required=True)
    attach.add_argument('--request-count',type=int);attach.add_argument('--billing',type=Path)
    args=parser.parse_args()
    try:
        if args.command=='prepare':
            print(prepare(args.project,args.task,args.output,args.rounds))
        elif args.command=='run':
            result=execute(args.experiment,args.driver,args.execute,args.allow_paid,args.max_paid_sessions,args.claude)
            if args.execute:
                assessed=report(args.experiment)
                result.update(successful_trials=assessed['successful_trials'])
                print(json.dumps(result,indent=2))
                return 0 if assessed['successful_trials']==assessed['prepared_trials'] else 1
            print(json.dumps(result,indent=2))
        elif args.command=='report':
            value=report(args.experiment)
            print(json.dumps({k:v for k,v in value.items() if k!='runs'},indent=2))
        elif args.command=='preflight':
            value=preflight(args.experiment,args.claude)
            print(json.dumps(value,indent=2))
            return 0 if value['local_checks_passed'] else 1
        else:
            print(attach_usage(args.experiment,args.trial,args.source,args.format,args.evidence,
                               args.coverage,args.request_count,args.billing))
    except (OSError,ValueError,KeyError,TypeError) as exc:
        # Do not print raw export text or exception messages containing project data.
        print('Task experiment rejected: '+type(exc).__name__+'. Check task, state, paths and flags.',file=sys.stderr)
        return 2
    return 0


if __name__=='__main__':
    raise SystemExit(main())
