"""Fail-open Claude Code PostToolUse adapter. Stdout contains only valid hook JSON."""
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tokenguard.core import compress, command_type
from tokenguard.storage import save_original
from tokenguard.audit import append
from tokenguard.tokens import estimate


def _diagnostic(event: Any, status: str) -> None:
    if os.environ.get('TOKENGUARD_DIAGNOSTICS') == '1':
        event = event if isinstance(event, dict) else {}
        try:
            response = event.get('tool_response')
            tool_input = event.get('tool_input')
            command = tool_input.get('command') if isinstance(tool_input, dict) else None
            stdout = response.get('stdout') if isinstance(response, dict) else None
            stderr = response.get('stderr') if isinstance(response, dict) else None
            append({'event': 'hook_invocation', 'status': status, 'session_id': event.get('session_id'),
                    'experiment_id': os.environ.get('TOKENGUARD_EXPERIMENT_ID'),
                    'tool_use_id': event.get('tool_use_id'),
                    'response_type': type(response).__name__,
                    'stdout_chars': len(stdout) if isinstance(stdout, str) else None,
                    'stderr_chars': len(stderr) if isinstance(stderr, str) else None,
                    'command_type': command_type(command) if isinstance(command, str) else None,
                    'source': os.environ.get('TOKENGUARD_SOURCE', 'unverified_hook')}, 'diagnostics-v04.jsonl')
        except Exception:
            pass


def _handle(event: Any) -> tuple[dict | None, str]:
    started = time.perf_counter()
    if os.environ.get('TOKENGUARD_ENABLED', '1') == '0':
        return None, 'disabled'
    if not isinstance(event, dict) or event.get('tool_name') != 'Bash' or event.get('hook_event_name', 'PostToolUse') != 'PostToolUse':
        return None, 'unmatched_event'
    response, tool_input = event.get('tool_response'), event.get('tool_input', {})
    if not isinstance(response, dict) or not isinstance(tool_input, dict):
        return None, 'invalid_shape'
    if response.get('interrupted') or response.get('isImage'):
        return None, 'interrupted_or_image'
    original, command = response.get('stdout'), tool_input.get('command', '')
    if not isinstance(original, str) or not isinstance(command, str):
        return None, 'invalid_text'
    optimized, strategy = compress(original, command=command)
    if optimized == original:
        return None, strategy
    identifier = hashlib.sha256(original.encode('utf-8')).hexdigest()
    compression_id = uuid.uuid4().hex
    cli = str(Path(__file__).resolve().parent.parent / 'tg.py')
    guide = (f'\n[TokenGuard artifact={identifier}; compression_id={compression_id}]\n'
             f'Selective recovery: python "{cli}" grep {identifier} "PATTERN" --compression-id {compression_id}\n'
             'Use show ID --start N --count 30 for a line range.\n')
    candidate = optimized + guide
    if len(candidate) >= len(original)*0.85:
        return None, 'insufficient_saving_after_metadata'
    # Persist both original and audit before emitting any replacement.
    save_original(original)
    source = os.environ.get('TOKENGUARD_SOURCE', 'unverified_hook')
    append({'event': 'compression', 'session_id': event.get('session_id'), 'source': source,
            'tool_use_id': event.get('tool_use_id'),
            'experiment_id': os.environ.get('TOKENGUARD_EXPERIMENT_ID'), 'tool_name': 'Bash',
            'origin_verification': 'self_reported; no live transcript validation',
            'command_type': command_type(command), 'strategy': strategy, 'artifact_id': identifier,
            'compression_id': compression_id, 'original_chars': len(original), 'optimized_chars': len(candidate),
            'saved_chars': len(original)-len(candidate), 'compression_ratio': len(candidate)/len(original),
            'compression_latency_ms': round((time.perf_counter()-started)*1000, 3), 'recovery_count': 0,
            'original_token_estimate': estimate(original), 'optimized_token_estimate': estimate(candidate),
            'actual_api_input_tokens': 'unavailable', 'cache_read_tokens': 'unavailable',
            'cache_write_tokens': 'unavailable', 'actual_cost': 'unavailable',
            'replacement_status': 'emitted_candidate_not_host_confirmed'})
    changed = dict(response)
    changed['stdout'] = candidate
    return {'hookSpecificOutput': {'hookEventName': 'PostToolUse', 'updatedToolOutput': changed}}, 'compressed'


def handle(event: Any) -> dict | None:
    try:
        result, status = _handle(event)
    except Exception as exc:
        result, status = None, 'exception_' + type(exc).__name__
        # Never echo exception messages: paths or hostile output may contain secrets.
        print('TokenGuard fail-open: ' + type(exc).__name__, file=sys.stderr)
    _diagnostic(event, status)
    return result


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--tokenguard-v04', action='store_true')
    parser.add_argument('--source', choices=['manual', 'mock', 'claude_hook'])
    args = parser.parse_args()
    if args.source:
        os.environ['TOKENGUARD_SOURCE'] = args.source
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    try:
        event = json.load(sys.stdin)
        result = handle(event)
        if result is not None:
            print(json.dumps(result, ensure_ascii=True))
    except Exception as exc:
        _diagnostic(None, 'input_or_output_' + type(exc).__name__)
        print('TokenGuard fail-open: ' + type(exc).__name__, file=sys.stderr)

if __name__ == '__main__':
    main()
