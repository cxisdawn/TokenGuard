"""Local sparse byte offsets for universal-newline UTF-8 artifacts.

Sidecars contain no log text. Validation checks source stat metadata, ordered
bounds and a checksum. Corrupt/stale sidecars are rebuilt without editing source.
"""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import struct
import tempfile

from .audit import root_dir

MAGIC = b'TGIDX001'
HEADER = struct.Struct('<8s6Q')
POINT = struct.Struct('<2Q')
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


def _read(path: Path, source: Path, target: int) -> Position:
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
        previous_line, previous_offset = 0, -1
        selected_line, selected_offset = 1, 0
        remaining = count
        while remaining:
            points = min(remaining, CHUNK//POINT.size)
            chunk = stream.read(points*POINT.size)
            if len(chunk) != points*POINT.size:
                raise ValueError('Incomplete index records')
            digest.update(chunk)
            for line, offset in POINT.iter_unpack(chunk):
                if not (previous_line < line <= max(1,total) and previous_offset < offset <= size):
                    raise ValueError('Invalid checkpoint bounds/order')
                if previous_line == 0 and (line,offset) != (1,0):
                    raise ValueError('Index must start at line 1')
                if line <= target:
                    selected_line, selected_offset = line, offset
                previous_line, previous_offset = line, offset
            remaining -= points
        if stream.read(32) != digest.digest():
            raise ValueError('Index checksum mismatch')
        if _fingerprint(os.fstat(original.fileno())) != _fingerprint(stat) or not _same_file_state(source.stat(), stat):
            raise ValueError('Artifact changed during index read')
    return Position(selected_line, selected_offset, total, count, 'reused')


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


def ensure_index(identifier: str, source: Path, target: int = 1, force: bool = False) -> Position:
    if target < 1:
        raise ValueError('target must be >= 1')
    path = index_path(identifier)
    if not force:
        try:
            return _read(path, source, target)
        except (OSError, ValueError):
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.tg-index-')
    try:
        with os.fdopen(fd, 'w+b') as output, source.open('rb') as original:
            stat = os.fstat(original.fileno())
            size = stat.st_size
            output.write(b'\0'*HEADER.size)
            output.write(POINT.pack(1,0))
            line = previous_line = 1
            previous_offset = last_end = 0
            count = 1
            for end in _ends(original):
                last_end = end
                line += 1
                if end < size and (line-previous_line >= LINE_STRIDE or end-previous_offset >= BYTE_STRIDE):
                    output.write(POINT.pack(line,end))
                    count += 1
                    previous_line, previous_offset = line,end
                    if HEADER.size + count*POINT.size + 32 > MAX_INDEX_BYTES:
                        raise ValueError('Index exceeds size budget')
            total = (line-1 if last_end == size else line) if size else 0
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
    result = _read(path, source, target)
    return Position(result.line,result.offset,result.total_lines,result.checkpoints,'built',size)
