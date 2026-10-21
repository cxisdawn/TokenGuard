"""Content-addressed artifacts and bounded streaming selective recovery."""
import hashlib
import os
import re
import tempfile
import time
from collections import deque
from pathlib import Path
from .audit import append, events, root_dir
from .line_index import ensure_index, Position

ID_RE = re.compile(r'^[0-9a-f]{64}$')
MAX_CHARS = 12000
MAX_SCAN = 8 * 1024 * 1024


def artifact_path(identifier: str) -> Path:
    if not isinstance(identifier, str) or not ID_RE.fullmatch(identifier):
        raise ValueError('Artifact ID must be exactly 64 lowercase SHA256 hex characters')
    folder = root_dir() / 'artifacts'
    path = folder / (identifier + '.txt')
    if folder.is_symlink() or path.is_symlink() or path.resolve().parent != folder.resolve():
        raise ValueError('Artifact links or escaped paths are forbidden')
    return path


def save_original(text: str) -> str:
    data = text.encode('utf-8')
    identifier = hashlib.sha256(data).hexdigest()
    path = artifact_path(identifier)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest() != identifier:
            raise ValueError('Existing artifact integrity mismatch')
        return identifier
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.tg-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return identifier


def _recover(identifier: str, mode: str, *, start: int = 1, count: int = 30,
             pattern: str = '', context: int = 1, limit: int = 20,
             max_chars: int = MAX_CHARS, compression_id: str | None = None,
             session_id: str | None = None, end: int | None = None) -> str:
    if not 1 <= max_chars <= MAX_CHARS:
        raise ValueError(f'max_chars must be 1..{MAX_CHARS}')
    if start < 1 or (end is not None and end < start):
        raise ValueError('start >= 1; end >= start required')
    started = time.perf_counter()
    path = artifact_path(identifier)
    records, _ = events()
    candidates = [r for r in records if r.get('event') == 'compression' and r.get('artifact_id') == identifier
                  and (compression_id is None or r.get('compression_id') == compression_id)
                  and (session_id is None or r.get('session_id') == session_id)]
    if (compression_id or session_id) and len(candidates) != 1:
        raise ValueError('Recovery requires one matching compression; pass --compression-id')
    owner = candidates[0] if len(candidates) == 1 else {}
    # Ambiguous shared artifacts remain readable, but are never attributed to a guessed session.
    scanned = emitted = matches = tail = 0
    output: list[str] = []
    output_lines: list[int] = []
    before: deque[tuple[int, str]] = deque(maxlen=context)
    last_emitted = 0
    truncated = False
    position = Position(1,0,0,0,'not_needed')
    if os.environ.get('TOKENGUARD_INDEX_ENABLED', '1') == '0':
        position = Position(1,0,0,0,'disabled')
    elif start > 1:
        try:
            position = ensure_index(identifier, path, target=start)
        except (OSError, ValueError):
            position = Position(1,0,0,0,'unavailable_sequential_fallback')
    window_end = start+count-1 if mode == 'show' else end
    with path.open('r', encoding='utf-8', newline='') as stream:
        stream.seek(position.offset)
        number = position.line-1
        while scanned < MAX_SCAN:
            line = stream.readline(MAX_SCAN-scanned+1)
            if not line:
                break
            scanned += len(line)
            number += 1
            if scanned > MAX_SCAN:
                truncated = True
                break
            if number < start:
                continue
            selected: list[tuple[int, str]] = []
            if mode == 'show':
                if number >= start:
                    selected = [(number, line)]
            else:
                hit = matches < limit and pattern.casefold() in line.casefold()
                if hit:
                    matches += 1
                    selected = list(before) + [(number, line)]
                    tail = context
                elif tail:
                    selected = [(number, line)]
                    tail -= 1
                before.append((number, line))
            for n, value in selected:
                if n <= last_emitted:
                    continue
                value = f'{n}: {value}' if mode == 'grep' else value
                remaining = max_chars-emitted
                output.append(value[:remaining])
                output_lines.append(n)
                emitted += min(remaining, len(value))
                last_emitted = n
                if emitted == max_chars:
                    truncated = True
                    break
            if truncated:
                break
            if window_end is not None and number >= window_end:
                break
            if mode == 'grep' and matches >= limit and tail == 0:
                break
        else:
            truncated = True
    result = ''.join(output)
    if not result:
        result = '(scan limit reached; narrow line range)\n' if truncated else '(no matches)\n'
    # Hard cap includes the marker. Bounded output is auditable even for huge single lines.
    resume_line = None
    if truncated:
        resume_line = max(start, number)
        marker = f'\n[TokenGuard: retrieval limit reached; next_start_line={resume_line}]\n'
        retained = max(0, max_chars-len(marker))
        offset = 0
        for line_number, piece in zip(output_lines, output):
            if offset+len(piece) > retained:
                resume_line = line_number
                break
            offset += len(piece)
        marker = f'\n[TokenGuard: retrieval limit reached; next_start_line={resume_line}]\n'
        # Reserve marker room before identifying the first affected line, so a
        # continuation never skips content displaced by the marker itself.
        result = result[:retained] + marker[:max_chars]
    result = result[:max_chars]
    append({'event': 'recovery', 'artifact_id': identifier, 'mode': mode,
            'compression_id': owner.get('compression_id'), 'session_id': owner.get('session_id'),
            'experiment_id': owner.get('experiment_id'), 'returned_chars': len(result),
            'scanned_chars': scanned, 'truncated': truncated, 'attribution': 'matched' if owner else 'unattributed',
            'index_status': position.status, 'index_build_bytes': position.build_bytes,
            'seek_offset_bytes': position.offset, 'requested_start_line': start, 'requested_end_line': window_end, 'next_start_line': resume_line,
            'recovery_latency_ms': round((time.perf_counter()-started)*1000,3)})
    return result


def read_lines(identifier: str, start: int, count: int, max_count: int = 100, **kwargs) -> str:
    if start < 1 or not 1 <= count <= min(100, max_count):
        raise ValueError('start >= 1 and count 1..100 required')
    return _recover(identifier, 'show', start=start, count=count, **kwargs)


def grep_lines(identifier: str, pattern: str, context: int = 1, limit: int = 20, **kwargs) -> str:
    if not isinstance(pattern, str) or not 1 <= len(pattern) <= 200:
        raise ValueError('pattern must have 1..200 characters')
    if not 0 <= context <= 5 or not 1 <= limit <= 50:
        raise ValueError('context 0..5; limit 1..50')
    return _recover(identifier, 'grep', pattern=pattern, context=context, limit=limit, **kwargs)
