"""Local sparse byte offsets for universal-newline UTF-8 artifacts.

Sidecars contain no log text. Validation checks source stat metadata, ordered
bounds and a checksum. Corrupt/stale sidecars are rebuilt without editing source.
"""
from dataclasses import dataclass
import codecs
import hashlib
import os
from pathlib import Path
import re
import struct
import tempfile

from .audit import root_dir

MAGIC = b'TGIDX002'
HEADER = struct.Struct('<8s6Q')
POINT = struct.Struct('<3Q')
CHUNK = 64 * 1024
LINE_STRIDE = 128
BYTE_STRIDE = 64 * 1024
MAX_INDEX_BYTES = 128 * 1024 * 1024
NEWLINES = re.compile(rb'\r\n|\r|\n')


@dataclass(frozen=True)
class Position:
    line: int
    offset: int
    total_lines: int
    checkpoints: int
    status: str
    build_bytes: int = 0
    column: int = 0


def _fingerprint(stat: os.stat_result) -> tuple[int, int, int, int]:
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino & ((1 << 64)-1)


def _same_file_state(left: os.stat_result, right: os.stat_result) -> bool:
    # Windows stat(path) and fstat(fd) can expose different ctime semantics.
    # Compare identity/size/mtime across APIs; compare full fingerprints only
    # between results from the same API. See CPython issue #157671.
    return (left.st_dev, left.st_ino, left.st_size, left.st_mtime_ns) == (
        right.st_dev, right.st_ino, right.st_size, right.st_mtime_ns)


def index_path(identifier: str) -> Path:
    # Keep the same identifier validation as the source artifact, without circular imports.
    if not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f]{64}', identifier):
        raise ValueError('Invalid artifact ID')
    folder = root_dir() / 'indexes-v04'
    path = folder / (identifier + '.idx')
    if folder.is_symlink() or path.is_symlink() or path.resolve().parent != folder.resolve():
        raise ValueError('Index links or escaped paths are forbidden')
    return path


def _read(path: Path, source: Path, target: int, column: int = 0) -> Position:
    with source.open('rb') as original, path.open('rb') as stream:
        stat = os.fstat(original.fileno())
        if not _same_file_state(source.stat(), stat):
            raise ValueError('Artifact changed before index read')
        length = os.fstat(stream.fileno()).st_size
        if length > MAX_INDEX_BYTES:
            raise ValueError('Index exceeds size budget')
        header = stream.read(HEADER.size)
        if len(header) != HEADER.size:
            raise ValueError('Incomplete index header')
        magic, size, mtime, ctime, inode, total, count = HEADER.unpack(header)
        if magic != MAGIC or (size, mtime, ctime, inode) != _fingerprint(stat):
            raise ValueError('Stale index')
        if count < 1 or length != HEADER.size + count*POINT.size + 32:
            raise ValueError('Invalid index length')
        digest = hashlib.sha256(header)
        previous_line, previous_column, previous_offset = 0, 0, -1
        selected_line, selected_column, selected_offset = 1, 0, 0
        remaining = count
        while remaining:
            points = min(remaining, CHUNK//POINT.size)
            chunk = stream.read(points*POINT.size)
            if len(chunk) != points*POINT.size:
                raise ValueError('Incomplete index records')
            digest.update(chunk)
            for line, char_column, offset in POINT.iter_unpack(chunk):
                if not ((previous_line, previous_column) < (line, char_column)
                        and 1 <= line <= max(1,total)
                        and char_column <= offset and previous_offset < offset <= size):
                    raise ValueError('Invalid checkpoint bounds/order')
                if previous_line == 0 and (line,char_column,offset) != (1,0,0):
                    raise ValueError('Index must start at line 1')
                if (line,char_column) <= (target,column):
                    selected_line, selected_column, selected_offset = line, char_column, offset
                previous_line, previous_column, previous_offset = line, char_column, offset
            remaining -= points
        if stream.read(32) != digest.digest():
            raise ValueError('Index checksum mismatch')
        if _fingerprint(os.fstat(original.fileno())) != _fingerprint(stat) or not _same_file_state(source.stat(), stat):
            raise ValueError('Artifact changed during index read')
    return Position(selected_line, selected_offset, total, count, 'reused', column=selected_column)


def _ends(stream):
    """Universal newline ends in bytes; handle CRLF split across chunk boundaries."""
    offset = 0
    pending = b''
    while True:
        chunk = stream.read(CHUNK)
        if not chunk:
            break
        base = offset-len(pending)
        offset += len(chunk)
        data = pending+chunk
        pending = b'\r' if data.endswith(b'\r') else b''
        if pending:
            data = data[:-1]
        for match in NEWLINES.finditer(data):
            yield base+match.end()
    if pending:
        yield offset


def _positions(stream):
    """Yield UTF-8 character boundaries, never a boundary within CRLF.

    Newline positions preserve the existing sparse line index. Chunk boundaries
    also index long lines, so resuming a character cursor does not rescan their
    entire prefix. Only UTF-8 bytes and CR/LF delimit lines; Unicode separators
    and other control characters are ordinary artifact content.
    """
    decoder = codecs.getincrementaldecoder('utf-8')()
    line, column, offset = 1, 0, 0
    pending = ''
    while True:
        chunk = stream.read(CHUNK)
        decoded = pending + decoder.decode(chunk, final=not chunk)
        pending = '\r' if chunk and decoded.endswith('\r') else ''
        if pending:
            decoded = decoded[:-1]
        begin = 0
        for match in re.finditer(r'\r\n|\r|\n', decoded):
            offset += len(decoded[begin:match.end()].encode('utf-8'))
            line += 1
            column = 0
            begin = match.end()
            yield line, column, offset
        if begin < len(decoded):
            remainder = decoded[begin:]
            column += len(remainder)
            offset += len(remainder.encode('utf-8'))
            yield line, column, offset
        if not chunk:
            break


def ensure_index(identifier: str, source: Path, target: int = 1, force: bool = False,
                 *, char_offset: int = 0) -> Position:
    if target < 1 or char_offset < 0:
        raise ValueError('target must be >= 1; char_offset must be >= 0')
    path = index_path(identifier)
    if not force:
        try:
            return _read(path, source, target, char_offset)
        except (OSError, ValueError):
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.tg-index-')
    try:
        with os.fdopen(fd, 'w+b') as output, source.open('rb') as original:
            stat = os.fstat(original.fileno())
            size = stat.st_size
            output.write(b'\0'*HEADER.size)
            output.write(POINT.pack(1,0,0))
            line = previous_line = 1
            column = previous_offset = 0
            count = 1
            for line, column, offset in _positions(original):
                if offset < size and (line-previous_line >= LINE_STRIDE or offset-previous_offset >= BYTE_STRIDE):
                    output.write(POINT.pack(line,column,offset))
                    count += 1
                    previous_line, previous_offset = line,offset
                    if HEADER.size + count*POINT.size + 32 > MAX_INDEX_BYTES:
                        raise ValueError('Index exceeds size budget')
            total = (line-1 if column == 0 else line) if size else 0
            if _fingerprint(os.fstat(original.fileno())) != _fingerprint(stat) or not _same_file_state(source.stat(), stat):
                raise ValueError('Artifact changed during index build')
            output.seek(0)
            output.write(HEADER.pack(MAGIC, *_fingerprint(stat), total, count))
            output.flush()
            output.seek(0)
            digest = hashlib.sha256()
            for chunk in iter(lambda: output.read(CHUNK), b''):
                digest.update(chunk)
            output.seek(0, os.SEEK_END)
            output.write(digest.digest())
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    result = _read(path, source, target, char_offset)
    return Position(result.line,result.offset,result.total_lines,result.checkpoints,'built',size,result.column)
