"""Artifact CLI. Example: python tg.py grep <sha256> ERROR"""
import argparse
import json
from pathlib import Path
from tokenguard.storage import artifact_path, read_lines, grep_lines, root_dir

def main():
    p = argparse.ArgumentParser(description='TokenGuard v0.3 local artifact tools')
    sub = p.add_subparsers(dest='cmd', required=True)
    show = sub.add_parser('show', help='show a bounded line range')
    show.add_argument('id'); show.add_argument('--start', type=int, default=1)
    show.add_argument('--count', type=int, default=30)
    grep = sub.add_parser('grep', help='literal case-insensitive search')
    grep.add_argument('id'); grep.add_argument('pattern')
    grep.add_argument('--context', type=int, default=1)
    grep.add_argument('--limit', type=int, default=20)
    stat = sub.add_parser('stats', help='show compression audit summary')
    args = p.parse_args()
    if args.cmd == 'show':
        print(read_lines(args.id, args.start, args.count), end='')
    elif args.cmd == 'grep':
        print(grep_lines(args.id, args.pattern, args.context, args.limit), end='')
    elif args.cmd == 'stats':
        log = root_dir() / 'audit.jsonl'
        rows = [json.loads(s) for s in log.read_text(encoding='utf-8').splitlines() if s.strip()] if log.exists() else []
        total = sum(r.get('original_chars',0) for r in rows)
        saved = sum(r.get('characters_saved_after_metadata',0) for r in rows)
        print(f'Compressed tool outputs: {len(rows)}')
        print(f'Original chars: {total}; saved after metadata: {saved}')
        print('These are character counts, NOT billable tokens or verified API savings.')

if __name__ == '__main__':
    main()
