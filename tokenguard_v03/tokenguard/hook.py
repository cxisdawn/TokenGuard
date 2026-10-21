"""Claude Code PostToolUse adapter. Fail-open: no stdout on failure."""
import json
import os
import sys
import time
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tokenguard.core import compress
from tokenguard.storage import root_dir, save_original

def handle(event: dict) -> dict | None:
    if event.get('tool_name') != 'Bash':
        return None
    response = event.get('tool_response')
    if not isinstance(response, dict) or response.get('interrupted') or response.get('isImage'):
        return None
    original = response.get('stdout')
    if not isinstance(original, str):
        return None
    optimized, reason = compress(original, command=str(event.get('tool_input', {}).get('command', '')))
    if optimized == original:
        return None
    identifier = save_original(original)
    changed = dict(response)
    # A bounded retrieval guide: avoids encouraging a full artifact read.
    changed['stdout'] = (
        optimized + '\n[TokenGuard artifact=' + identifier + ']\n'
        + 'If you need omitted details, use selective retrieval:\n'
        + 'python "' + str(Path(__file__).resolve().parent.parent / 'tg.py')
        + '" grep ' + identifier + ' "PATTERN"\n'
        + 'python "' + str(Path(__file__).resolve().parent.parent / 'tg.py')
        + '" show ' + identifier + ' --start 1 --count 30\n'
        + 'Do not read the full artifact unless necessary.\n'
    )
    # Audit is best effort; never break an otherwise successful replacement.
    try:
        path = root_dir() / 'audit.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            'ts': round(time.time(), 3), 'session_id': event.get('session_id'),
            'tool': 'Bash', 'action': reason, 'artifact_id': identifier,
            'original_chars': len(original), 'optimized_chars': len(changed['stdout']),
            'raw_compressed_chars': len(optimized),
            'characters_saved_after_metadata': len(original) - len(changed['stdout'])
        }
        with path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except Exception as err:
        print(f'TokenGuard audit warning: {err}', file=sys.stderr)
    return {'hookSpecificOutput': {'hookEventName':'PostToolUse', 'updatedToolOutput':changed}}

def main():
    try:
        data = json.load(sys.stdin)
        result = handle(data)
        if result is not None:
            print(json.dumps(result, ensure_ascii=False))
    except Exception as err:
        print(f'TokenGuard fail-open: {err}', file=sys.stderr)

if __name__ == '__main__':
    main()
