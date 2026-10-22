"""Opt-in single-task SWE-bench quality pilot. Preparation/preflight make no model calls."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tokenguard.swebench_quality import prepare_quality, preflight, execute_quality, report_quality, grade_log, fetch_instance


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    fetch = sub.add_parser('fetch')
    fetch.add_argument('--output', type=Path, required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--instance', type=Path, required=True)
    prep.add_argument('--harness', type=Path, required=True)
    prep.add_argument('--output', type=Path, required=True)
    prep.add_argument('--rounds', type=int, default=3)
    prep.add_argument('--model', default='deepseek-flash')
    prep.add_argument('--compat-wheel', type=Path)
    prep.add_argument('--max-turns', type=int, default=20)
    for mode in ('preflight', 'run', 'report'):
        command = sub.add_parser(mode)
        command.add_argument('experiment', type=Path)
        if mode == 'run':
            command.add_argument('--execute', action='store_true')
            command.add_argument('--allow-paid', action='store_true')
            command.add_argument('--max-paid-sessions', type=int, default=2)
            command.add_argument('--claude', default='claude')
    grade = sub.add_parser('grade')
    for name in ('instance', 'harness', 'log'):
        grade.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'fetch':
        print(fetch_instance(args.output))
        return 0
    if args.action == 'prepare':
        print(prepare_quality(args.instance, args.harness, args.output, args.rounds, args.model,
                              args.compat_wheel, args.max_turns))
        return 0
    if args.action == 'grade':
        result = grade_log(args.instance, args.harness, args.log)
    elif args.action == 'preflight':
        result = preflight(args.experiment.resolve())
    elif args.action == 'run':
        result = execute_quality(args.experiment.resolve(), execute=args.execute, allow_paid=args.allow_paid,
                                 max_paid_sessions=args.max_paid_sessions, claude=args.claude)
    else:
        result = report_quality(args.experiment.resolve())
    # Reports contain only allowlisted metadata. Full traces remain in ignored reports/.
    print(json.dumps(result, indent=2, ensure_ascii=True))
    if args.action == 'preflight':
        return 0 if result['passed'] else 1
    if args.action == 'run' and args.execute:
        return 0 if all(arm['successful'] == arm['planned'] for arm in result['arms'].values()) else 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
