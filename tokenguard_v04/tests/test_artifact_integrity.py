"""Byte-level corruption checks; fixtures never touch historical artifacts."""
import hashlib
import os
from pathlib import Path

import pytest

from tokenguard import artifact_integrity as integrity


@pytest.fixture
def artifact(tmp_path, monkeypatch):
    monkeypatch.setenv('TOKENGUARD_DATA_DIR', str(tmp_path))

    def create(data: bytes):
        identifier = hashlib.sha256(data).hexdigest()
        source = tmp_path / 'artifacts' / (identifier + '.txt')
        source.parent.mkdir(exist_ok=True)
        source.write_bytes(data)
        return identifier, source
    return create


def test_first_read_checks_whole_artifact_then_reuses_only_selected_blocks(artifact):
    raw = b'a' * (integrity.BLOCK_SIZE * 4 + 37)
    identifier, source = artifact(raw)
    first = integrity.verify_range(identifier, source, integrity.BLOCK_SIZE + 9,
                                   integrity.BLOCK_SIZE + 15)
    assert first == {'integrity_status': 'built', 'integrity_build_bytes': len(raw),
                     'integrity_checked_bytes': integrity.BLOCK_SIZE}
    reused = integrity.verify_range(identifier, source, len(raw) - 1, len(raw))
    assert reused == {'integrity_status': 'reused', 'integrity_build_bytes': 0,
                      'integrity_checked_bytes': 37}


@pytest.mark.parametrize('changed', [b'b' * 200, b'longer' * 40])
def test_stable_rewrite_is_rejected_without_repairing_artifact(artifact, changed):
    identifier, source = artifact(b'a' * 200)
    integrity.verify_range(identifier, source, 0, 1)
    original = source.stat()
    source.write_bytes(changed)
    os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
    with pytest.raises(integrity.IntegrityError, match='mismatch'):
        integrity.verify_range(identifier, source, 0, 1)
    assert source.read_bytes() == changed


def test_missing_cache_cannot_bless_legacy_content_address_mismatch(artifact):
    identifier, source = artifact(b'original')
    source.write_bytes(b'modified')
    with pytest.raises(integrity.IntegrityError, match='mismatch'):
        integrity.verify_range(identifier, source, 0, 1)
    assert not integrity.integrity_path(identifier).exists()


def test_same_fingerprint_still_checks_selected_block(artifact, monkeypatch):
    identifier, source = artifact(b'a' * integrity.BLOCK_SIZE + b'b' * 12)
    integrity.verify_range(identifier, source, 0, 1)
    # Explicitly model hidden/restored metadata, including ctime; the selected
    # block check must not rely on the stale-cache metadata branch.
    fingerprint = integrity._fingerprint(source.stat())
    source.write_bytes(b'z' + b'a' * (integrity.BLOCK_SIZE - 1) + b'b' * 12)
    monkeypatch.setattr(integrity, '_fingerprint', lambda stat: fingerprint)
    with pytest.raises(integrity.IntegrityError, match='block'):
        integrity.verify_range(identifier, source, 0, 1)


@pytest.mark.parametrize('corrupt', ['checksum', 'truncated', 'magic', 'layout', 'oversized'])
def test_bad_cache_rebuilt_after_full_content_check(artifact, corrupt):
    raw = '中文\r\n😀\n'.encode() * 100
    identifier, source = artifact(raw)
    integrity.verify_range(identifier, source, 0, 5)
    sidecar = integrity.integrity_path(identifier)
    value = bytearray(sidecar.read_bytes())
    if corrupt == 'checksum':
        value[-1] ^= 1
    elif corrupt == 'truncated':
        value = value[:-1]
    elif corrupt == 'magic':
        value[0] ^= 1
        value[-32:] = hashlib.sha256(value[:-32]).digest()
    elif corrupt == 'layout':
        value = value[:-32] + b'extra' + hashlib.sha256(value[:-32] + b'extra').digest()
    else:
        value = b'!' * (integrity.MAX_SIDECAR_BYTES + 1)
    sidecar.write_bytes(value)
    result = integrity.verify_range(identifier, source, 0, 5)
    assert result['integrity_status'] == 'built'
    assert result['integrity_build_bytes'] == len(raw)
    assert source.read_bytes() == raw


def test_unwritable_cache_does_not_disable_integrity(artifact, monkeypatch):
    identifier, source = artifact(b'hello')
    monkeypatch.setattr(integrity.tempfile, 'mkstemp', lambda **kwargs: (_ for _ in ()).throw(PermissionError()))
    result = integrity.verify_range(identifier, source, 0, 5)
    assert result['integrity_status'] == 'uncached'
    assert result['integrity_build_bytes'] == 5
    source.write_bytes(b'other')
    with pytest.raises(integrity.IntegrityError):
        integrity.verify_range(identifier, source, 0, 5)


def test_cache_directory_replaced_by_regular_file_uses_uncached_checks(artifact, tmp_path):
    identifier, source = artifact(b'hello')
    (tmp_path / 'integrity-v04').write_bytes(b'occupied')
    result = integrity.verify_range(identifier, source, 0, 5)
    assert result['integrity_status'] == 'uncached'
    assert result['integrity_build_bytes'] == 5
    source.write_bytes(b'other')
    with pytest.raises(integrity.IntegrityError):
        integrity.verify_range(identifier, source, 0, 5)


def test_cache_location_permission_failure_still_checks_source(artifact, monkeypatch):
    identifier, source = artifact(b'hello')
    monkeypatch.setattr(integrity, 'integrity_path', lambda identifier: (_ for _ in ()).throw(PermissionError()))
    assert integrity.verify_range(identifier, source, 0, 5)['integrity_status'] == 'uncached'
    source.write_bytes(b'other')
    with pytest.raises(integrity.IntegrityError):
        integrity.verify_range(identifier, source, 0, 5)


def test_sidecar_size_limit_uses_streamed_uncached_verification(artifact, monkeypatch):
    raw = b'z' * (integrity.BLOCK_SIZE * 4)
    identifier, source = artifact(raw)
    monkeypatch.setattr(integrity, 'MAX_SIDECAR_BYTES', integrity.HEADER.size + 32 * 2)
    result = integrity.verify_range(identifier, source, 1, 2)
    assert result['integrity_status'] == 'uncached'
    assert result['integrity_build_bytes'] == len(raw)
    assert not integrity.integrity_path(identifier).exists()


@pytest.mark.parametrize('raw', [b'', b'last', '中文😀\r\n'.encode()])
def test_empty_and_utf8_artifacts(artifact, raw):
    identifier, source = artifact(raw)
    result = integrity.verify_range(identifier, source, 0, len(raw))
    assert result['integrity_build_bytes'] == len(raw)
    assert result['integrity_checked_bytes'] == len(raw)
    assert integrity.verify_range(identifier, source, 0, 0)['integrity_status'] == 'reused'


@pytest.mark.parametrize('start,end', [(-1, 2), (0, 4), (2, 1), (True, 1), (0, 1.0)])
def test_invalid_ranges_rejected(artifact, start, end):
    identifier, source = artifact(b'abc')
    with pytest.raises(ValueError):
        integrity.verify_range(identifier, source, start, end)


def test_wrong_source_path_rejected(artifact, tmp_path):
    identifier, source = artifact(b'abc')
    wrong = tmp_path / 'other.txt'
    wrong.write_bytes(source.read_bytes())
    with pytest.raises(integrity.IntegrityError):
        integrity.verify_range(identifier, wrong, 0, 1)


class _ObservedFile:
    def __init__(self, stream, read):
        self.stream = stream
        self.on_read = read

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self.stream.__exit__(*args)

    def read(self, size=-1):
        data = self.stream.read(size)
        self.on_read(size, data)
        return data


def test_source_read_calls_are_bounded_and_reuse_does_not_rescan(artifact, monkeypatch):
    raw = b'x' * (integrity.BLOCK_SIZE * 12 + 17)
    identifier, source = artifact(raw)
    original_open = Path.open
    observed = []

    def tracking_open(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        if path == source and args == ('rb',):
            return _ObservedFile(stream, lambda size, data: observed.append((size, len(data))))
        return stream

    monkeypatch.setattr(Path, 'open', tracking_open)
    first = integrity.verify_range(identifier, source, len(raw)-1, len(raw))
    assert all(0 < size <= integrity.BLOCK_SIZE for size, _ in observed)
    assert sum(length for _, length in observed) == first['integrity_build_bytes'] + first['integrity_checked_bytes']
    observed.clear()
    result = integrity.verify_range(identifier, source, len(raw)-1, len(raw))
    assert result['integrity_build_bytes'] == 0
    assert observed == [(17, 17)]


@pytest.mark.parametrize('cached', [False, True])
def test_source_modified_during_read_is_rejected(artifact, monkeypatch, cached):
    raw = b'x' * (integrity.BLOCK_SIZE + 19)
    identifier, source = artifact(raw)
    if cached:
        integrity.verify_range(identifier, source, 0, 1)
    original_open = Path.open
    changed = False

    def mutate(size, data):
        nonlocal changed
        if not changed and data:
            changed = True
            stamp = source.stat()
            with original_open(source, 'r+b') as writer:
                writer.write(b'z')
                writer.flush()
            # Do not rely on filesystem timestamp resolution for this race test.
            os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000_000))

    def changing_open(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        return _ObservedFile(stream, mutate) if path == source and args == ('rb',) else stream

    monkeypatch.setattr(Path, 'open', changing_open)
    with pytest.raises(integrity.IntegrityError, match='changed'):
        integrity.verify_range(identifier, source, 0, 1)
    assert changed
    assert source.read_bytes().startswith(b'z')


@pytest.mark.parametrize('position', ['source', 'cache', 'cache_parent', 'root_parent'])
def test_reparse_points_rejected_even_when_target_looks_valid(artifact, tmp_path, monkeypatch, position):
    from types import SimpleNamespace

    identifier, source = artifact(b'abc')
    sidecar = integrity.integrity_path(identifier)
    targets = {'source': source, 'cache': sidecar, 'cache_parent': sidecar.parent,
               'root_parent': tmp_path.parent}
    target = targets[position]
    original = Path.lstat

    def reparse_stat(path, *args, **kwargs):
        if path == target:
            return SimpleNamespace(st_file_attributes=0x400, st_mode=0o100644)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'lstat', reparse_stat)
    with pytest.raises(integrity.IntegrityError, match='links'):
        integrity.verify_range(identifier, source, 0, 1)


def test_path_handle_ctime_disagreement_does_not_invalidate_cache(artifact, monkeypatch):
    from types import SimpleNamespace

    identifier, source = artifact(b'abc')
    integrity.verify_range(identifier, source, 0, 1)
    original = Path.stat

    def path_ctime(path, *args, **kwargs):
        current = original(path, *args, **kwargs)
        if path != source:
            return current
        fields = {name: getattr(current, name) for name in dir(current) if name.startswith('st_')}
        fields['st_ctime_ns'] += 100_000
        return SimpleNamespace(**fields)

    monkeypatch.setattr(Path, 'stat', path_ctime)
    assert integrity.verify_range(identifier, source, 0, 1)['integrity_status'] == 'reused'


def test_range_crosses_block_boundary_and_checks_both_complete_blocks(artifact):
    raw = b'a' * (integrity.BLOCK_SIZE * 3)
    identifier, source = artifact(raw)
    integrity.verify_range(identifier, source, 0, 0)
    result = integrity.verify_range(identifier, source, integrity.BLOCK_SIZE - 1, integrity.BLOCK_SIZE + 1)
    assert result['integrity_build_bytes'] == 0
    assert result['integrity_checked_bytes'] == 2 * integrity.BLOCK_SIZE


@pytest.mark.skipif(os.name != 'nt', reason='Windows 8.3 filesystem aliases')
def test_real_windows_short_alias_resolves_to_same_artifact(artifact):
    import ctypes

    identifier, source = artifact(b'valid artifact')
    buffer = ctypes.create_unicode_buffer(32768)
    function = ctypes.windll.kernel32.GetShortPathNameW
    function.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
    function.restype = ctypes.c_uint
    length = function(str(source), buffer, len(buffer))
    if not length or length >= len(buffer) or buffer.value == str(source):
        pytest.skip('Filesystem does not expose an 8.3 alias')
    assert integrity.verify_range(identifier, Path(buffer.value), 0, 1)['integrity_status'] == 'built'
