"""Local append-only observations; damaged evidence is reported, never repaired away."""
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import time
from typing import Any, Iterator
import uuid

from .audit_schema import AuditIndex, count, identifier, validated
from .visible_tests import filesystem_path

MAX_LINE_BYTES = 256 * 1024
LOCK_TIMEOUT_SECONDS = 2.0


def root_dir() -> Path:
    return filesystem_path(Path(os.environ.get('TOKENGUARD_DATA_DIR', str(Path.home() / '.tokenguard'))))


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _append_lock(path: Path) -> Iterator[None]:
    """Serialize TokenGuard writers with a bounded OS lock, released on process exit."""
    fd = os.open(filesystem_path(path), os.O_RDWR | os.O_CREAT | getattr(os, 'O_BINARY', 0), 0o600)
    locked = False
    try:
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while not locked:
            try:
                if os.name == 'nt':
                    import msvcrt
                    # Windows permits locking a byte beyond EOF. Do not race
                    # to initialize that byte before acquiring the lock.
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError('audit append lock timed out') from exc
                time.sleep(0.01)
        yield
    finally:
        if locked:
            if os.name == 'nt':
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    pending = memoryview(data)
    while pending:
        written = os.write(fd, pending)
        if written <= 0:
            raise OSError('incomplete audit write')
        pending = pending[written:]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate audit JSON key')
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError('nonfinite audit JSON constant')


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('nonfinite audit JSON number')
    return result


def append(row: dict[str, Any], filename: str = 'audit-v04.jsonl') -> None:
    folder = root_dir()
    folder.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps({'timestamp': timestamp(), 'event_id': uuid.uuid4().hex, **row},
                          ensure_ascii=True, allow_nan=False) + '\n').encode('utf-8')
    if len(encoded) > MAX_LINE_BYTES:
        raise ValueError('audit record exceeds the bounded reader limit')
    path = filesystem_path(folder / filename)
    with _append_lock(path.with_name(path.name + '.lock')):
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_BINARY', 0), 0o600)
        try:
            size = os.lseek(fd, 0, os.SEEK_END)
            if size:
                os.lseek(fd, -1, os.SEEK_END)
                last = os.read(fd, 1)
                os.lseek(fd, 0, os.SEEK_END)
                if last != b'\n':
                    _write_all(fd, b'\n')
            _write_all(fd, encoded)
        finally:
            os.close(fd)


def _rows(path: Path) -> Iterator[dict | None]:
    """Bound each read; a bad UTF-8/JSON/oversized physical line yields one None."""
    path = filesystem_path(path)
    if not path.exists():
        return
    with path.open('rb') as stream:
        first = True
        while True:
            line = stream.readline(MAX_LINE_BYTES+1)
            if not line:
                break
            if len(line) > MAX_LINE_BYTES:
                while not line.endswith(b'\n'):
                    line = stream.readline(MAX_LINE_BYTES+1)
                    if not line:
                        break
                yield None
                first = False
                continue
            try:
                text = line.decode('utf-8-sig' if first else 'utf-8')
                row = json.loads(text, parse_constant=_reject_constant, parse_float=_finite_float,
                                 object_pairs_hook=_unique_object)
                yield row if isinstance(row, dict) else None
            except (UnicodeError, ValueError, TypeError, RecursionError):
                yield None
            first = False


def events(filename: str = 'audit-v04.jsonl') -> tuple[list[dict], int]:
    """Parsing compatibility API; schema validation is deliberately separate."""
    rows, malformed = [], 0
    for row in _rows(root_dir() / filename):
        if row is None:
            malformed += 1
        else:
            rows.append(row)
    return rows, malformed


def compression_index(rows: list[dict] | None = None) -> AuditIndex:
    return validated(events()[0] if rows is None else rows)


def recovery_owner(artifact_id: str, compression_id: str | None = None,
                   session_id: str | None = None, experiment_id: str | None = None) -> dict:
    rows, _ = events()
    index = compression_index(rows)
    candidates = [row for row in index.compressions.values() if row.get('artifact_id') == artifact_id
                  and (compression_id is None or row['compression_id'] == compression_id)
                  and (session_id is None or row.get('session_id') == session_id)
                  and (experiment_id is None or row.get('experiment_id') == experiment_id)]
    explicit = compression_id is not None or session_id is not None or experiment_id is not None
    # A damaged candidate cannot silently disappear from a session-only choice.
    # A caller-supplied valid compression ID resolves that ambiguity explicitly.
    quarantined_match = any(row.get('event') == 'compression' and row.get('artifact_id') == artifact_id
        and (session_id is None or row.get('session_id') == session_id)
        and (experiment_id is None or row.get('experiment_id') == experiment_id)
        and (not identifier(row.get('compression_id')) or row['compression_id'] not in index.compressions)
        for row in rows)
    if explicit and (len(candidates) != 1 or (compression_id is None and quarantined_match)):
        raise ValueError('Recovery requires one valid unambiguous matching compression; pass --compression-id')
    return candidates[0] if explicit and len(candidates) == 1 else {}


def summary(session_id: str | None = None, experiment_id: str | None = None,
            *, source: str | None = None, record_limit: int | None = None) -> dict:
    if record_limit is not None and (isinstance(record_limit, bool) or not isinstance(record_limit, int) or record_limit < 0):
        raise ValueError('record_limit must be a nonnegative integer or None')
    rows, malformed = events()
    index = validated(rows)
    def selected(row: dict) -> bool:
        return ((session_id is None or row.get('session_id') == session_id) and
                (experiment_id is None or row.get('experiment_id') == experiment_id))
    compressed = [dict(row) for row in index.compressions.values()
                  if selected(row) and (source is None or row.get('source') == source)]
    ids = {row['compression_id'] for row in compressed}
    recovery = [row for row in index.recoveries if selected(row) and
                (source is None or row.get('compression_id') in ids)]
    deliveries = [index.deliveries[cid] for cid in ids if cid in index.deliveries]
    recovery_totals: dict[str, tuple[int, int]] = {}
    for row in recovery:
        if identifier(row.get('compression_id')):
            reads, chars = recovery_totals.get(row['compression_id'], (0, 0))
            recovery_totals[row['compression_id']] = (reads+1, chars+row['returned_chars'])
    for row in compressed:
        row['recovery_count'], row['recovered_chars'] = recovery_totals.get(row['compression_id'], (0, 0))
        row['delivery_status'] = index.deliveries.get(row['compression_id'], {}).get('status', 'unknown')
    original = sum(row['original_chars'] for row in compressed)
    optimized = sum(row['optimized_chars'] for row in compressed)
    complete = [row for row in compressed if 'original_token_estimate' in row and 'optimized_token_estimate' in row]
    coverage = {'complete_records': len(complete), 'total_records': len(compressed),
                'missing_records': len(compressed)-len(complete)}
    estimated_original = (sum(row['original_token_estimate']['estimated_tokens'] for row in complete)
                          if len(complete) == len(compressed) else None)
    estimated_optimized = (sum(row['optimized_token_estimate']['estimated_tokens'] for row in complete)
                           if len(complete) == len(compressed) else None)
    returned = sum(row['returned_chars'] for row in recovery)
    failures = sum(row['status'] == 'stdout_write_failed' for row in deliveries)
    successes = sum(row['status'] == 'stdout_write_succeeded' for row in deliveries)
    unknown = len(compressed)-len(deliveries)
    integrity = not (malformed or index.invalid_rows or index.duplicate_rows or index.conflicting_rows)
    measurement_coverage = {key: {'observed_records': sum(key in row for row in recovery),
                                 'total_records': len(recovery)}
                            for key in ('scanned_chars', 'index_build_bytes', 'recovery_latency_ms', 'index_status',
                                        'integrity_status', 'integrity_build_bytes', 'integrity_checked_bytes')}
    def measured_sum(key: str) -> int | float | None:
        return sum(row[key] for row in recovery) if all(key in row for row in recovery) else None
    return {'index_build_bytes': measured_sum('index_build_bytes'),
            'integrity_build_bytes': measured_sum('integrity_build_bytes'),
            'integrity_checked_bytes': measured_sum('integrity_checked_bytes'),
            'indexed_recoveries': (sum(r['index_status'] in ('built', 'reused') for r in recovery)
                                   if all('index_status' in r for r in recovery) else None),
            'recovery_latency_ms': measured_sum('recovery_latency_ms'),
            'measurement_coverage': measurement_coverage,
            'estimated_original_tokens': estimated_original, 'estimated_optimized_tokens': estimated_optimized,
            'estimated_token_reduction_ratio': (1-estimated_optimized/estimated_original
                if estimated_original else (0 if estimated_original == 0 else None)),
            'token_estimate_coverage': coverage,
            'token_estimate_scope': 'prepared candidate stdout including metadata only; excludes recovery and task context',
            'net_character_saving_after_recovery': original-optimized-returned,
            'scope': 'filtered' if session_id is not None or experiment_id is not None or source is not None else 'cumulative_v04',
            'session_id': session_id, 'experiment_id': experiment_id, 'source_filter': source,
            'compressions': len(compressed), 'prepared_candidates': len(compressed),
            'confirmed_stdout_deliveries': successes, 'delivery_failures': failures, 'delivery_unknown': unknown,
            'delivery_data_available': integrity and unknown == 0,
            'delivery_scope': 'stdout write and flush only; host acceptance unverified',
            'original_chars': original, 'optimized_chars': optimized, 'saved_chars': original-optimized,
            'character_reduction_ratio': (original-optimized)/original if original else 0,
            'recovery_count': len(recovery), 'returned_chars': returned,
            'scanned_chars': measured_sum('scanned_chars'),
            'malformed_rows': malformed, 'invalid_rows': index.invalid_rows,
            'duplicate_rows': index.duplicate_rows, 'conflicting_rows': index.conflicting_rows,
            'data_available': integrity, 'aggregate_scope': 'valid observed records; not necessarily complete totals',
            'records_total': len(compressed),
            'records_omitted': max(0, len(compressed)-record_limit) if record_limit is not None else 0,
            'records': compressed if record_limit is None else compressed[-record_limit:] if record_limit else [],
            'actual_api_input_tokens': 'unavailable', 'cache_read_tokens': 'unavailable',
            'cache_write_tokens': 'unavailable', 'actual_cost': 'unavailable'}


def diagnostics(session_id: str | None = None, experiment_id: str | None = None,
                limit: int = 20) -> dict:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError('limit must be 1..100')
    recent: deque[dict] = deque(maxlen=limit)
    malformed = matched = invalid = 0
    for row in _rows(root_dir() / 'diagnostics-v04.jsonl'):
        if row is None:
            malformed += 1
            continue
        if row.get('event') != 'hook_invocation':
            continue
        if (not identifier(row.get('status')) or any(key in row and row[key] is not None and
                not identifier(row[key]) for key in ('session_id', 'experiment_id', 'source', 'tool_use_id',
                                                     'timestamp', 'pytest_mode', 'command_type', 'response_type')) or
                any(key in row and row[key] is not None and not count(row[key])
                    for key in ('stdout_chars', 'stderr_chars'))):
            invalid += 1
            continue
        if session_id is not None and row.get('session_id') != session_id:
            continue
        if experiment_id is not None and row.get('experiment_id') != experiment_id:
            continue
        matched += 1
        recent.append(row)
    return {'invocations': list(recent), 'matching_invocations': matched, 'limit': limit,
            'omitted_invocations': max(0, matched-limit), 'malformed_rows': malformed,
            'invalid_rows': invalid, 'data_available': not (malformed or invalid),
            'session_id': session_id, 'experiment_id': experiment_id}
