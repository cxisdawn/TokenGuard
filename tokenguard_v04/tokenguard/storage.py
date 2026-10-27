"""Content-addressed artifacts and bounded streaming selective recovery."""
import hashlib
import os
import re
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from .audit import append, events, root_dir
from .line_index import ensure_index, Position, _fingerprint, _same_file_state

ID_RE = re.compile(r'^[0-9a-f]{64}$')
MAX_CHARS = 12000
MAX_SCAN = 8 * 1024 * 1024


class RecoveryLimitError(ValueError):
    """A bounded request cannot deliver complete evidence or advance its cursor."""


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


@dataclass(frozen=True)
class _Piece:
    line: int
    column: int
    prefix: str
    body: str
    required_chars: int = 0

    @property
    def text(self) -> str:
        return self.prefix + self.body

    def cursor(self, retained: int) -> tuple[int, int]:
        consumed = min(len(self.body), max(0, retained-len(self.prefix)))
        if consumed == len(self.body) and self.body.endswith(('\r', '\n')):
            return self.line+1, 0
        return self.line, self.column+consumed


def _match_span(value: str, pattern: str) -> tuple[int, int] | None:
    """Map a casefold match back to Unicode character offsets, including ß."""
    folded = value.casefold()
    needle = pattern.casefold()
    start = folded.find(needle)
    if start < 0:
        return None
    if value.isascii():
        return start, start+len(needle)

    def boundary(target: int) -> int:
        low, high = 0, len(value)
        while low < high:
            middle = (low+high)//2
            if len(value[:middle].casefold()) < target:
                low = middle+1
            else:
                high = middle
        return low

    return boundary(start+1)-1, boundary(start+len(needle))


def _limit_marker(line: int, column: int) -> str:
    return (f'\n[TokenGuard: retrieval limit reached; next_start_line={line}; '
            f'next_start_char={column}]\n')


def _bounded_result(pieces: list[_Piece], maximum: int,
                    fallback: tuple[int, int]) -> tuple[str, tuple[int, int]]:
    """Reserve the cursor marker before identifying the first displaced char."""
    text = ''.join(piece.text for piece in pieces)

    def cursor_at(retained: int) -> tuple[int, int]:
        for piece in pieces:
            if retained < len(piece.text):
                return piece.cursor(retained)
            retained -= len(piece.text)
        return fallback

    # Reserving the largest encountered marker also handles digit boundaries
    # without oscillating between e.g. column 999 and 1000.
    reserved = len(_limit_marker(*fallback))
    while True:
        retained = max(0, maximum-reserved)
        offset = 0
        for piece in pieces:
            # A continuation marker must not displace half a keyword. Defer
            # that whole record so a subsequent grep can still find its match.
            if piece.required_chars and 0 < retained-offset < piece.required_chars:
                retained = offset
                break
            offset += len(piece.text)
        cursor = cursor_at(retained)
        marker = _limit_marker(*cursor)
        if len(marker) <= reserved:
            if len(marker) > maximum or (pieces and retained <= len(pieces[0].prefix)):
                raise RecoveryLimitError('max_chars is too small for complete match/content and continuation metadata')
            return text[:retained]+marker, cursor
        reserved = len(marker)


def _recover(identifier: str, mode: str, *, start: int = 1, count: int = 30,
             pattern: str = '', context: int = 1, limit: int = 20,
             max_chars: int = MAX_CHARS, compression_id: str | None = None,
             session_id: str | None = None, end: int | None = None,
             char_offset: int = 0) -> str:
    if not 1 <= max_chars <= MAX_CHARS:
        raise ValueError(f'max_chars must be 1..{MAX_CHARS}')
    if start < 1 or char_offset < 0 or (end is not None and end < start):
        raise ValueError('start >= 1; char_offset >= 0; end >= start required')
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
    pieces: list[_Piece] = []
    before: deque[tuple[int, int, str, tuple[int, int] | None]] = deque(maxlen=context)
    last_emitted = 0
    truncated = False
    position = Position(1,0,0,0,'not_needed')
    expected_source = path.stat()
    if os.environ.get('TOKENGUARD_INDEX_ENABLED', '1') == '0':
        position = Position(1,0,0,0,'disabled')
    elif start > 1 or char_offset > 0:
        try:
            position = ensure_index(identifier, path, target=start, char_offset=char_offset)
        except (OSError, ValueError):
            position = Position(1,0,0,0,'unavailable_sequential_fallback')
    window_end = start+count-1 if mode == 'show' else end
    resume = (start, char_offset)
    windowed_matches = omitted_context_lines = 0
    with path.open('r', encoding='utf-8', newline='') as stream:
        opened_source = os.fstat(stream.fileno())
        if (not _same_file_state(expected_source, opened_source) or
                _fingerprint(path.stat()) != _fingerprint(expected_source)):
            raise ValueError('Artifact changed before recovery read')
        stream.seek(position.offset)
        number = position.line-1
        initial_column = position.column
        while scanned < MAX_SCAN:
            line = stream.readline(MAX_SCAN-scanned+1)
            if not line:
                break
            available = MAX_SCAN-scanned
            scanned += len(line)
            number += 1
            column = initial_column if number == position.line else 0
            scan_limited = len(line) > available
            if scan_limited:
                line = line[:available]
            # The next scan overlaps a possible boundary-spanning keyword.
            resume = ((number, column+max(0, len(line)-len(pattern.casefold())))
                      if scan_limited and mode == 'grep' else (number, column+len(line)))
            if not scan_limited and line.endswith(('\r', '\n')):
                resume = (number+1, 0)
            if number < start:
                if scan_limited:
                    truncated = True
                    break
                continue
            if number == start:
                skip = max(0, char_offset-column)
                if skip > len(line) and not scan_limited:
                    raise ValueError('char_offset exceeds the requested line length')
                column += min(skip, len(line))
                line = line[skip:]
            selected: list[tuple[int, int, str, tuple[int, int] | None]] = []
            context_notice = ''
            if mode == 'show':
                if number >= start:
                    selected = [(number, column, line, None)]
            else:
                span = _match_span(line, pattern) if matches < limit else None
                hit = span is not None
                if hit:
                    matches += 1
                    selected = list(before) + [(number, column, line, span)]
                    # A large context line must not consume the entire response
                    # before the actual keyword is shown. Short-line output is
                    # unchanged; over-budget context is explicitly audited.
                    projected = sum(len(value)+len(str(n))+2 for n, _, value, _ in selected)
                    if matches == 1 and projected > max_chars-emitted:
                        omitted_context_lines += len(before)
                        if before:
                            context_notice = (f'[TokenGuard: omitted {len(before)} context lines; '
                                              'use show for original]\n')
                        selected = [(number, column, line, span)]
                    tail = context
                elif tail:
                    selected = [(number, column, line, None)]
                    tail -= 1
                before.append((number, column, line, None))
            for n, col, value, span in selected:
                if n <= last_emitted:
                    continue
                notice = context_notice if span is not None else ''
                prefix = notice + f'{n}: ' if mode == 'grep' else ''
                remaining = max_chars-emitted
                match_end_in_value = span[1] if span is not None else 0
                if mode == 'grep' and span is not None and len(prefix)+len(value) > remaining:
                    # Surface the matching text, even when it follows megabytes
                    # of a single-line JSON record. Offsets are Unicode chars.
                    reserve = len(_limit_marker(n, col+len(value)))
                    prefix_budget = len(notice + f'{n}: [char {col+len(value)}] ')
                    width = max(1, remaining-reserve-prefix_budget)
                    match_start, match_end = span
                    begin = max(0, match_start-max(0, width-(match_end-match_start))//2)
                    begin = min(begin, max(0, len(value)-width))
                    value = value[begin:]
                    col += begin
                    prefix = notice + f'{n}: [char {col}] '
                    match_end_in_value -= begin
                    windowed_matches += 1
                used = (prefix+value)[:remaining]
                used_prefix = used[:min(len(prefix), len(used))]
                body = used[len(used_prefix):]
                required_chars = len(prefix)+match_end_in_value if span is not None else 0
                pieces.append(_Piece(n, col, used_prefix, body, required_chars))
                emitted += len(used)
                last_emitted = n
                if emitted == max_chars:
                    truncated = True
                    break
            if truncated:
                break
            if scan_limited:
                truncated = True
                break
            if window_end is not None and number >= window_end:
                break
            if mode == 'grep' and matches >= limit and tail == 0:
                # One character distinguishes EOF from an unexamined tail;
                # reaching the match limit must never claim a complete search.
                lookahead = stream.read(1)
                scanned += len(lookahead)
                truncated = bool(lookahead)
                break
        else:
            truncated = True
        if (_fingerprint(os.fstat(stream.fileno())) != _fingerprint(opened_source) or
                _fingerprint(path.stat()) != _fingerprint(expected_source)):
            raise ValueError('Artifact changed during recovery read')
    result = ''.join(piece.text for piece in pieces)
    if not result:
        result = '(scan limit reached; narrow line range)\n' if truncated else '(no matches)\n'
    # Hard cap includes the marker. Bounded output is auditable even for huge single lines.
    resume_line = resume_char = None
    if truncated:
        result, (resume_line, resume_char) = _bounded_result(pieces, max_chars, resume)
        if (resume_line, resume_char) <= (start, char_offset):
            raise RecoveryLimitError('scan limit prevents reaching or advancing the requested cursor; '
                                     'enable or repair the artifact index')
    result = result[:max_chars]
    append({'event': 'recovery', 'artifact_id': identifier, 'mode': mode,
            'compression_id': owner.get('compression_id'), 'session_id': owner.get('session_id'),
            'experiment_id': owner.get('experiment_id'), 'returned_chars': len(result),
            'scanned_chars': scanned, 'truncated': truncated, 'attribution': 'matched' if owner else 'unattributed',
            'index_status': position.status, 'index_build_bytes': position.build_bytes,
            'seek_offset_bytes': position.offset, 'requested_start_line': start, 'requested_end_line': window_end, 'next_start_line': resume_line,
            'requested_start_char': char_offset, 'next_start_char': resume_char,
            'windowed_matches': windowed_matches, 'omitted_context_lines': omitted_context_lines,
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
