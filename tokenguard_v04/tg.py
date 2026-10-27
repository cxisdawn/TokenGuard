"""TokenGuard v0.4 selective artifact recovery and scoped reports."""
import argparse
import json
import sys
from tokenguard.storage import read_lines, grep_lines, RecoveryLimitError
from tokenguard.audit import summary, diagnostics


def main() -> None:
    # Recovery is a UTF-8 data boundary. Windows newline translation would turn
    # stored CRLF into CRCRLF and make emitted character counts differ from audit.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', newline='')
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='cmd', required=True)
    for kind in ('show', 'grep'):
        p = sub.add_parser(kind)
        p.add_argument('id')
        p.add_argument('--compression-id')
        p.add_argument('--session', dest='session_id')
        p.add_argument('--max-chars', type=int, default=12000)
        p.add_argument('--start-char', type=int, default=0,
                       help='zero-based Unicode character offset within --start (continuation cursor)')
        if kind == 'show':
            p.add_argument('--start', type=int, default=1)
            p.add_argument('--count', type=int, default=30)
            p.add_argument('--end', type=int)
        else:
            p.add_argument('pattern')
            p.add_argument('--context', type=int, default=1)
            p.add_argument('--limit', type=int, default=20)
            p.add_argument('--start', type=int, default=1)
            p.add_argument('--end', type=int)
    p = sub.add_parser('index', help='prepare or rebuild a local artifact line index')
    p.add_argument('id')
    p.add_argument('--rebuild', action='store_true')
    p = sub.add_parser('stats')
    p.add_argument('--session')
    p.add_argument('--experiment')
    p.add_argument('--json', action='store_true')
    p = sub.add_parser('diagnostics')
    p.add_argument('--session')
    p.add_argument('--experiment')
    p.add_argument('--limit', type=int, default=20)
    p = sub.add_parser('doctor', help='offline checks of the installed project hook; no model calls')
    p.add_argument('--project', required=True)

    p = sub.add_parser('usage', help='offline provider usage import and paired comparison')
    usage_sub = p.add_subparsers(dest='usage_cmd', required=True)
    imp = usage_sub.add_parser('import')
    imp.add_argument('--source', required=True)
    imp.add_argument('--manifest', required=True)
    imp.add_argument('--format', choices=['deepseek','anthropic'], required=True)
    imp.add_argument('--billing')
    imp.add_argument('--output', required=True)
    pair = usage_sub.add_parser('compare')
    pair.add_argument('runs', nargs='+')
    pair.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        if args.cmd in ('show', 'grep'):
            options = {'compression_id': args.compression_id, 'session_id': args.session_id,
                       'max_chars': args.max_chars, 'char_offset': args.start_char}
            if args.cmd == 'show':
                count = args.end-args.start+1 if args.end is not None else args.count
                result = read_lines(args.id, args.start, count, **options)
            else:
                result = grep_lines(args.id, args.pattern, args.context, args.limit, start=args.start, end=args.end, **options)
            print(result, end='')
        elif args.cmd == 'index':
            from tokenguard.storage import artifact_path
            from tokenguard.line_index import ensure_index
            from dataclasses import asdict
            print(json.dumps(asdict(ensure_index(args.id, artifact_path(args.id), force=args.rebuild)), indent=2))
        elif args.cmd == 'usage':
            from pathlib import Path
            from tokenguard.usage import import_usage, write_bundle, load
            if args.usage_cmd == 'import':
                bundle = import_usage(Path(args.source), Path(args.manifest), args.format,
                                      Path(args.billing) if args.billing else None)
                print(write_bundle(bundle, Path(args.output)))
            else:
                from tokenguard.usage_compare import compare, export
                report = compare([load(Path(path)) for path in args.runs])
                print(export(report, Path(args.output)))
                if report['eligible_pairs'] != report['total_pairs']:
                    parser.exit(1)
        elif args.cmd == 'diagnostics':
            print(json.dumps(diagnostics(args.session, args.experiment, args.limit), indent=2))
        elif args.cmd == 'doctor':
            from pathlib import Path
            from tokenguard.doctor import check
            report = check(Path(args.project))
            print(json.dumps(report, indent=2, ensure_ascii=True))
            if not all(report.get(k) == 'passed' for k in ('configuration', 'configured_process', 'compression', 'artifact_integrity')):
                parser.exit(1)

        else:
            result = summary(args.session, args.experiment)
            if args.json:
                print(json.dumps(result, indent=2, ensure_ascii=True))
            else:
                print(f"Scope: {result['scope']}; session={args.session}; experiment={args.experiment}")
                print(f"Compressed outputs: {result['compressions']}; characters saved: {result['saved_chars']} ({result['character_reduction_ratio']:.1%})")
                print(f"Recoveries: {result['recovery_count']}; returned characters: {result['returned_chars']}; scanned characters: {result['scanned_chars']}")
                print(f"Indexed recoveries: {result['indexed_recoveries']}; index construction bytes: {result['index_build_bytes']}; recovery latency ms: {result['recovery_latency_ms']:.3f}")
                print(f"Uncalibrated local estimate, compressed outputs only: {result['estimated_original_tokens']} -> {result['estimated_optimized_tokens']} tokens ({result['estimated_token_reduction_ratio']:.1%})")
                print('Character counts and local heuristic token estimates are NOT verified task token/cost savings.')
                print('Actual API input, cache read/write tokens and billed cost: unavailable')
    except RecoveryLimitError as exc:
        parser.exit(2, f'TokenGuard retrieval limit: {exc}\n')
    except (ValueError, OSError) as exc:
        parser.exit(2, f'TokenGuard retrieval/report failed: {type(exc).__name__}\n')

if __name__ == '__main__':
    main()
