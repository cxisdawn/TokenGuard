"""Verify content-addressed artifacts, then recheck only requested byte blocks.

The disposable sidecar contains hashes and file metadata, never artifact text.
Its checksum detects accidental corruption, not malicious replacement by the
same OS user who can rewrite both source and sidecar. First use, stale metadata,
or a damaged sidecar requires streaming the complete source and matching its ID.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat as stat_module
import struct
import tempfile
from typing import BinaryIO

from .audit import root_dir
from .visible_tests import canonical_path

BLOCK_SIZE = 64 * 1024
MAX_SIDECAR_BYTES = 16 * 1024 * 1024
MAGIC = b'TGSHA001'
# magic, block size, source size, mtime, ctime, device, inode, artifact SHA, blocks
HEADER = struct.Struct('<8sQQqqQQ32sQ')
_UINT64 = (1 << 64) - 1


class IntegrityError(ValueError):
    """The artifact identity, bytes, or safe path could not be established."""


class _CacheError(ValueError):
    """A disposable cache must be rebuilt from the content-addressed source."""


def _fingerprint(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_size, value.st_mtime_ns, value.st_ctime_ns,
            value.st_dev & _UINT64, value.st_ino & _UINT64)


def _same_file_state(left: os.stat_result, right: os.stat_result) -> bool:
    # fstat and stat may expose different Windows ctime semantics. Compare
    # ctime only within the same API, while preserving full handle/path identity.
    return (left.st_dev, left.st_ino, left.st_size, left.st_mtime_ns) == (
        right.st_dev, right.st_ino, right.st_size, right.st_mtime_ns)


def _canonical(path: Path) -> Path:
    try:
        return canonical_path(path)
    except ValueError as exc:
        raise IntegrityError('Artifact/cache links or escaped paths are forbidden') from exc


def _identifier(identifier: str) -> None:
    if not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f]{64}', identifier):
        raise IntegrityError('Artifact ID must be exactly 64 lowercase SHA256 hex characters')


def integrity_path(identifier: str) -> Path:
    """Return a validated cache location; reject linked ancestors, even at root."""
    _identifier(identifier)
    folder = _canonical(root_dir()) / 'integrity-v04'
    return _canonical(folder / (identifier + '.sha256'))


def _source_path(identifier: str, source: Path) -> Path:
    expected = _canonical(_canonical(root_dir()) / 'artifacts' / (identifier + '.txt'))
    source = _canonical(source)
    if source != expected:
        raise IntegrityError('Source path does not match the content-addressed artifact')
    return source


def _stable(source: Path, stream: BinaryIO, opened: os.stat_result,
            initial_path: os.stat_result) -> None:
    _canonical(source)
    current_path = source.stat()
    if (_fingerprint(os.fstat(stream.fileno())) != _fingerprint(opened)
            or _fingerprint(current_path) != _fingerprint(initial_path)
            or not _same_file_state(current_path, opened)):
        raise IntegrityError('Artifact changed during integrity verification')


def _header(identifier: str, opened: os.stat_result) -> bytes:
    size, mtime, ctime, device, inode = _fingerprint(opened)
    return HEADER.pack(MAGIC, BLOCK_SIZE, size, mtime, ctime, device, inode,
                       bytes.fromhex(identifier), (size + BLOCK_SIZE - 1) // BLOCK_SIZE)


def _read_cache(path: Path, expected: bytes) -> bytes:
    """Read a bounded, exact-layout sidecar and validate its complete checksum."""
    _canonical(path)
    initial = path.stat()
    if not stat_module.S_ISREG(initial.st_mode):
        raise _CacheError('Cache is not a regular file')
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if not _same_file_state(initial, opened):
            raise _CacheError('Cache changed before read')
        count = HEADER.unpack(expected)[-1]
        expected_size = HEADER.size + 32 * count + 32
        if opened.st_size != expected_size or expected_size > MAX_SIDECAR_BYTES:
            raise _CacheError('Cache length exceeds bounds or disagrees with layout')
        data = stream.read(expected_size + 1)
        if (len(data) != expected_size or data[:HEADER.size] != expected
                or hashlib.sha256(data[:-32]).digest() != data[-32:]):
            raise _CacheError('Stale or damaged integrity cache')
        if (_fingerprint(os.fstat(stream.fileno())) != _fingerprint(opened)
                or _fingerprint(path.stat()) != _fingerprint(initial)
                or not _same_file_state(path.stat(), opened)):
            raise _CacheError('Cache changed during read')
    _canonical(path)
    return data[HEADER.size:-32]


def _build(identifier: str, source: Path, stream: BinaryIO,
           opened: os.stat_result, initial_path: os.stat_result) -> bytes | None:
    """Stream the source even when the optional hash inventory exceeds budget."""
    size = opened.st_size
    count = (size + BLOCK_SIZE - 1) // BLOCK_SIZE
    blocks = bytearray() if HEADER.size + 32 * count + 32 <= MAX_SIDECAR_BYTES else None
    digest = hashlib.sha256()
    total = 0
    stream.seek(0)
    while True:
        data = stream.read(BLOCK_SIZE)
        if not data:
            break
        digest.update(data)
        total += len(data)
        if total > size:
            raise IntegrityError('Artifact changed during integrity build')
        if blocks is not None:
            blocks.extend(hashlib.sha256(data).digest())
    _stable(source, stream, opened, initial_path)
    if total != size or digest.hexdigest() != identifier:
        raise IntegrityError('Artifact SHA256 content-address mismatch')
    return bytes(blocks) if blocks is not None else None


def _write_cache(path: Path, header: bytes, blocks: bytes) -> None:
    _canonical(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _canonical(path)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.tg-integrity-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(header)
            stream.write(blocks)
            stream.write(hashlib.sha256(header + blocks).digest())
            stream.flush()
            os.fsync(stream.fileno())
        _canonical(path)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _check_blocks(stream: BinaryIO, blocks: bytes, size: int,
                  start_byte: int, end_byte: int) -> int:
    checked = 0
    if start_byte == end_byte:
        return checked
    first, last = start_byte // BLOCK_SIZE, (end_byte - 1) // BLOCK_SIZE
    stream.seek(first * BLOCK_SIZE)
    for number in range(first, last + 1):
        length = min(BLOCK_SIZE, size - number * BLOCK_SIZE)
        data = stream.read(length)
        if len(data) != length or hashlib.sha256(data).digest() != blocks[number*32:(number+1)*32]:
            raise IntegrityError('Artifact block hash mismatch')
        checked += len(data)
    return checked


def verify_range(identifier: str, source: Path, start_byte: int, end_byte: int) -> dict:
    """Verify half-open byte range ``[start_byte, end_byte)`` without editing it.

    Reused caches re-read every complete 64 KiB block touched by the range.
    Missing/stale caches first verify the entire source SHA256. A read-only or
    over-budget cache degrades to full streaming verification, never acceptance
    of unverified bytes. Counters separate full builds from extra block reads.
    """
    _identifier(identifier)
    if type(start_byte) is not int or type(end_byte) is not int or not 0 <= start_byte <= end_byte:
        raise ValueError('Byte range must contain nonnegative ordered integers')
    source = _source_path(identifier, Path(source))
    try:
        path = integrity_path(identifier)
    except OSError:
        # An inaccessible cache location is optional. Unsafe link paths raise
        # IntegrityError instead and must never be downgraded to this fallback.
        path = None
    initial = source.stat()
    if not stat_module.S_ISREG(initial.st_mode):
        raise IntegrityError('Artifact must be a regular file')
    with source.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if not _same_file_state(initial, opened):
            raise IntegrityError('Artifact changed before integrity read')
        if end_byte > opened.st_size:
            raise ValueError('Byte range exceeds artifact size')
        expected = _header(identifier, opened)
        build_bytes = 0
        status = 'reused'
        try:
            if path is None:
                raise _CacheError('Integrity cache location is unavailable')
            blocks = _read_cache(path, expected)
        except (OSError, _CacheError):
            blocks = _build(identifier, source, stream, opened, initial)
            build_bytes = opened.st_size
            status = 'uncached'
            if blocks is not None and path is not None:
                try:
                    _write_cache(path, expected, blocks)
                    status = 'built'
                except OSError:
                    # Storage failure cannot suppress content-address failures.
                    pass
        checked_bytes = 0 if blocks is None else _check_blocks(
            stream, blocks, opened.st_size, start_byte, end_byte)
        _stable(source, stream, opened, initial)
    return {'integrity_status': status, 'integrity_build_bytes': build_bytes,
            'integrity_checked_bytes': checked_bytes}
