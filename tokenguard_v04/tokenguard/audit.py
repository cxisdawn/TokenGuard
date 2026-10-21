"""Local audit events; recovery joins use compression IDs, never artifact alone."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def root_dir() -> Path:
    return Path(os.environ.get('TOKENGUARD_DATA_DIR', str(Path.home() / '.tokenguard')))


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def append(row: dict[str, Any], filename: str = 'audit-v04.jsonl') -> None:
    folder = root_dir()
    folder.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps({'timestamp': timestamp(), **row}, ensure_ascii=True) + '\n').encode('utf-8')
    fd = os.open(folder / filename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        if os.write(fd, encoded) != len(encoded):
            raise OSError('incomplete audit write')
    finally:
        os.close(fd)


def events(filename: str = 'audit-v04.jsonl') -> tuple[list[dict], int]:
    path = root_dir() / filename
    rows, malformed = [], 0
    if path.exists():
        with path.open(encoding='utf-8') as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError('not object')
                    rows.append(row)
                except (ValueError, TypeError):
                    malformed += 1
    return rows, malformed


def summary(session_id: str | None = None, experiment_id: str | None = None) -> dict:
    rows, malformed = events()
    selected = [r for r in rows if (session_id is None or r.get('session_id') == session_id)
                and (experiment_id is None or r.get('experiment_id') == experiment_id)]
    compressed = [dict(r) for r in selected if r.get('event') == 'compression']
    recovery = [r for r in selected if r.get('event') == 'recovery']
    for row in compressed:
        own = [r for r in recovery if r.get('compression_id') == row.get('compression_id')]
        row['recovery_count'] = len(own)
        row['recovered_chars'] = sum(r.get('returned_chars', 0) for r in own)
    original = sum(r['original_chars'] for r in compressed)
    optimized = sum(r['optimized_chars'] for r in compressed)
    estimated_original = sum(r.get('original_token_estimate', {}).get('estimated_tokens', 0) for r in compressed)
    estimated_optimized = sum(r.get('optimized_token_estimate', {}).get('estimated_tokens', 0) for r in compressed)
    return {'index_build_bytes': sum(r.get('index_build_bytes', 0) for r in recovery),
            'indexed_recoveries': sum(r.get('index_status') in ('built', 'reused') for r in recovery),
            'recovery_latency_ms': sum(r.get('recovery_latency_ms', 0) for r in recovery),
            'estimated_original_tokens': estimated_original, 'estimated_optimized_tokens': estimated_optimized,
            'estimated_token_reduction_ratio': 1-estimated_optimized/estimated_original if estimated_original else 0,
            'token_estimate_scope': 'compressed tool stdout including metadata only; excludes recovery and task context',
            'net_character_saving_after_recovery': original-optimized-sum(r.get('returned_chars', 0) for r in recovery),
            'scope': 'filtered' if session_id is not None or experiment_id is not None else 'cumulative_v04',
            'session_id': session_id, 'experiment_id': experiment_id, 'compressions': len(compressed),
            'original_chars': original, 'optimized_chars': optimized, 'saved_chars': original-optimized,
            'character_reduction_ratio': (original-optimized)/original if original else 0,
            'recovery_count': len(recovery), 'returned_chars': sum(r.get('returned_chars', 0) for r in recovery),
            'scanned_chars': sum(r.get('scanned_chars', 0) for r in recovery),
            'malformed_rows': malformed, 'records': compressed,
            'actual_api_input_tokens': 'unavailable', 'cache_read_tokens': 'unavailable',
            'cache_write_tokens': 'unavailable', 'actual_cost': 'unavailable'}


def diagnostics(session_id: str | None = None, experiment_id: str | None = None,
                limit: int = 20) -> dict:
    """Read metadata only, retaining at most limit matching invocations in memory."""
    from collections import deque
    if not 1 <= limit <= 100:
        raise ValueError('limit must be 1..100')
    recent: deque[dict] = deque(maxlen=limit)
    malformed = matched = 0
    path = root_dir() / 'diagnostics-v04.jsonl'
    if path.exists():
        with path.open(encoding='utf-8-sig') as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError('not object')
                except (ValueError, TypeError):
                    malformed += 1
                    continue
                if row.get('event') != 'hook_invocation':
                    continue
                if session_id is not None and row.get('session_id') != session_id:
                    continue
                if experiment_id is not None and row.get('experiment_id') != experiment_id:
                    continue
                matched += 1
                recent.append(row)
    return {'invocations': list(recent), 'matching_invocations': matched, 'limit': limit,
            'omitted_invocations': max(0, matched-limit), 'malformed_rows': malformed,
            'session_id': session_id, 'experiment_id': experiment_id}
