"""Fail-open Claude Code PostToolUse adapter. Stdout contains only valid hook JSON."""
import hashlib
import io
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


def _identity(value: Any) -> str | None:
    """Only bounded plain strings may become audit identifiers."""
    if (isinstance(value, str) and 0 < len(value) <= 256
            and not any(ord(char) < 32 or ord(char) == 127 for char in value)):
        return value
    return None


def _error(exc: Exception) -> None:
    # Never echo exception messages: paths or hostile output may contain secrets.
    try:
        print('TokenGuard fail-open: ' + type(exc).__name__, file=sys.stderr, flush=True)
    except Exception:
        pass


def _diagnostic(event: Any, status: str, pytest_mode: str = 'compact') -> None:
    if os.environ.get('TOKENGUARD_DIAGNOSTICS') == '1':
        event = event if isinstance(event, dict) else {}
        try:
            response = event.get('tool_response')
            tool_input = event.get('tool_input')
            command = tool_input.get('command') if isinstance(tool_input, dict) else None
            stdout = response.get('stdout') if isinstance(response, dict) else None
            stderr = response.get('stderr') if isinstance(response, dict) else None
            append({'event': 'hook_invocation', 'status': status, 'session_id': _identity(event.get('session_id')),
                    'pytest_mode': pytest_mode,
                    'experiment_id': _identity(os.environ.get('TOKENGUARD_EXPERIMENT_ID')),
                    'tool_use_id': _identity(event.get('tool_use_id')),
                    'response_type': type(response).__name__,
                    'stdout_chars': len(stdout) if isinstance(stdout, str) else None,
                    'stderr_chars': len(stderr) if isinstance(stderr, str) else None,
                    'command_type': command_type(command) if isinstance(command, str) else None,
                    'source': _identity(os.environ.get('TOKENGUARD_SOURCE', 'unverified_hook'))}, 'diagnostics-v04.jsonl')
        except Exception:
            pass


def _handle(event: Any, *, pytest_mode: str = 'compact',
            _delivery_context: dict | None = None) -> tuple[dict | None, str]:
    started = time.perf_counter()
    if os.environ.get('TOKENGUARD_ENABLED', '1') == '0':
        return None, 'disabled'
    if not isinstance(event, dict) or event.get('tool_name') != 'Bash' or event.get('hook_event_name', 'PostToolUse') != 'PostToolUse':
        return None, 'unmatched_event'
    identities = (event.get('session_id'), event.get('tool_use_id'),
                  os.environ.get('TOKENGUARD_EXPERIMENT_ID'),
                  os.environ.get('TOKENGUARD_SOURCE', 'unverified_hook'))
    if any(value is not None and _identity(value) is None for value in identities):
        return None, 'invalid_metadata'
    response, tool_input = event.get('tool_response'), event.get('tool_input', {})
    if not isinstance(response, dict) or not isinstance(tool_input, dict):
        return None, 'invalid_shape'
    if response.get('interrupted') or response.get('isImage'):
        return None, 'interrupted_or_image'
    original, command = response.get('stdout'), tool_input.get('command', '')
    if not isinstance(original, str) or not isinstance(command, str):
        return None, 'invalid_text'
    optimized, strategy = compress(original, command=command, pytest_mode=pytest_mode)
    if optimized == original:
        return None, strategy
    identifier = hashlib.sha256(original.encode('utf-8')).hexdigest()
    compression_id = uuid.uuid4().hex
    cli = str(Path(__file__).resolve().parent.parent / 'tg.py')
    # Starting with LF after an original CR would silently turn its terminator
    # into CRLF. Reuse an existing line boundary before adding the recovery guide.
    separator = '' if optimized.endswith(('\r', '\n')) else '\n'
    guide = (f'{separator}[TokenGuard artifact={identifier}; compression_id={compression_id}]\n'
             f'Selective recovery: python "{cli}" grep {identifier} "PATTERN" --compression-id {compression_id}\n'
             'For a line range, use show ID --start N --count 30 with the same --compression-id.\n')
    candidate = optimized + guide
    if len(candidate) >= len(original)*0.85:
        return None, 'insufficient_saving_after_metadata'
    changed = dict(response)
    changed['stdout'] = candidate
    result = {'hookSpecificOutput': {'hookEventName': 'PostToolUse', 'updatedToolOutput': changed}}
    # Validate the entire preserved response before storing a candidate. Python's
    # default encoder accepts NaN/Infinity, which are not valid JSON for the host.
    serialized = json.dumps(result, ensure_ascii=True, allow_nan=False) + '\n'
    # Persist both original and audit before emitting any replacement.
    save_original(original)
    source = os.environ.get('TOKENGUARD_SOURCE', 'unverified_hook')
    append({'event': 'compression', 'session_id': event.get('session_id'), 'source': source,
            'tool_use_id': event.get('tool_use_id'),
            'experiment_id': os.environ.get('TOKENGUARD_EXPERIMENT_ID'), 'tool_name': 'Bash',
            'origin_verification': 'self_reported; no live transcript validation',
            'command_type': command_type(command), 'strategy': strategy, 'artifact_id': identifier,
            'pytest_mode': pytest_mode,
            'compression_id': compression_id, 'original_chars': len(original), 'optimized_chars': len(candidate),
            'saved_chars': len(original)-len(candidate), 'compression_ratio': len(candidate)/len(original),
            'compression_latency_ms': round((time.perf_counter()-started)*1000, 3), 'recovery_count': 0,
            'original_token_estimate': estimate(original), 'optimized_token_estimate': estimate(candidate),
            'actual_api_input_tokens': 'unavailable', 'cache_read_tokens': 'unavailable',
            'cache_write_tokens': 'unavailable', 'actual_cost': 'unavailable',
            'replacement_status': 'prepared_candidate_not_host_confirmed'})
    if _delivery_context is not None:
        _delivery_context.update({'serialized': serialized, 'receipt': {
            'event': 'replacement_delivery', 'compression_id': compression_id,
            'session_id': event.get('session_id'), 'tool_use_id': event.get('tool_use_id'),
            'experiment_id': os.environ.get('TOKENGUARD_EXPERIMENT_ID'), 'source': source,
            'artifact_id': identifier}})
    return result, 'compressed'


def handle(event: Any, *, pytest_mode: str = 'compact',
           _delivery_context: dict | None = None) -> dict | None:
    try:
        result, status = _handle(event, pytest_mode=pytest_mode, _delivery_context=_delivery_context)
    except Exception as exc:
        result, status = None, 'exception_' + type(exc).__name__
        _error(exc)
    _diagnostic(event, status, pytest_mode)
    return result


def _reject_constant(value: str) -> None:
    raise ValueError('non-JSON numeric constant')


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _silence_failed_stdout() -> None:
    """Avoid a second failed buffered flush during interpreter shutdown.

    This cannot retract bytes already accepted by a pipe. No host acknowledgement
    is available in this protocol; a flushed write is the strongest local fact.
    """
    try:
        with open(os.devnull, 'w', encoding='utf-8') as sink:
            os.dup2(sink.fileno(), sys.stdout.fileno())
    except Exception:
        # Descriptor exhaustion can prevent even opening devnull. Closing a
        # failed buffered stream may itself fail, but must not skip the receipt.
        try:
            sys.stdout.close()
        except Exception:
            pass
        sys.stdout = io.StringIO()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--tokenguard-v04', action='store_true')
    parser.add_argument('--source', choices=['manual', 'mock', 'claude_hook'])
    parser.add_argument('--pytest-mode', choices=['compact', 'indexed', 'off'], default='compact')
    args = parser.parse_args()
    if args.source:
        os.environ['TOKENGUARD_SOURCE'] = args.source
    event = None
    delivery: dict = {}
    try:
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            if hasattr(stream, 'reconfigure'):
                stream.reconfigure(encoding='utf-8')
        event = json.load(sys.stdin, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
        result = handle(event, pytest_mode=args.pytest_mode, _delivery_context=delivery)
    except Exception as exc:
        _diagnostic(event, 'input_' + type(exc).__name__, args.pytest_mode)
        _error(exc)
        return
    if result is None:
        return
    receipt = delivery['receipt']
    try:
        serialized = delivery['serialized']
        if sys.stdout.write(serialized) != len(serialized):
            raise OSError('short stdout write')
        sys.stdout.flush()
    except Exception as exc:
        receipt['status'] = 'stdout_write_failed'
        _silence_failed_stdout()
        _diagnostic(event, 'output_' + type(exc).__name__, args.pytest_mode)
        _error(exc)
    else:
        receipt['status'] = 'stdout_write_succeeded'
    # Receipt persistence is a separate failure boundary: once flushed, the
    # candidate cannot be withdrawn if the audit append subsequently fails.
    try:
        append(receipt)
    except Exception as exc:
        _diagnostic(event, 'delivery_receipt_' + type(exc).__name__, args.pytest_mode)
        _error(exc)

if __name__ == '__main__':
    main()
