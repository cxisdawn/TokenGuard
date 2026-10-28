"""Recovery must never certify altered artifact bytes as the saved output."""
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

from tokenguard.audit import events
from tokenguard.line_index import ensure_index, index_path
from tokenguard.storage import artifact_path, grep_lines, read_lines, save_original


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('TOKENGUARD_DATA_DIR', str(tmp_path/'data'))
    monkeypatch.setenv('TOKENGUARD_INDEX_ENABLED', '1')


@pytest.mark.parametrize('indexed', [False, True])
@pytest.mark.parametrize('mode', ['show_head', 'show_tail', 'grep', 'disabled'])
def test_stable_corrupt_artifact_is_rejected_without_success_audit(indexed, mode):
    identifier = save_original('correct\n'*400+'TARGET original\n')
    source = artifact_path(identifier)
    if indexed:
        read_lines(identifier, 401, 1)
    previous = events()[0]
    source.write_bytes(b'ALTERED\n'*400+b'TARGET tampered\n')
    bad = source.read_bytes()
    with pytest.raises(ValueError):
        if mode == 'grep':
            grep_lines(identifier, 'TARGET', start=400)
        elif mode == 'disabled':
            with patch.dict(os.environ, {'TOKENGUARD_INDEX_ENABLED':'0'}):
                read_lines(identifier, 1, 1)
        else:
            read_lines(identifier, 401 if mode == 'show_tail' else 1, 1)
    assert events()[0] == previous
    assert source.read_bytes() == bad


def test_first_recovery_records_hash_cost_and_reuse_is_bounded():
    raw = 'normal\n'*50000+'TARGET\n'
    identifier = save_original(raw)
    assert read_lines(identifier, 50001, 1) == 'TARGET\n'
    first = events()[0][-1]
    assert first['integrity_status'] == 'built'
    assert first['integrity_build_bytes'] == len(raw.encode('utf-8'))
    assert 0 < first['integrity_checked_bytes'] <= 65536
    assert read_lines(identifier, 50001, 1) == 'TARGET\n'
    again = events()[0][-1]
    assert again['integrity_status'] == 'reused'
    assert again['integrity_build_bytes'] == 0
    assert 0 < again['integrity_checked_bytes'] <= 65536


@pytest.mark.parametrize('field', ['start','count','max_count','max_chars','char_offset'])
@pytest.mark.parametrize('value', [True, 1.5, '1', 2**63])
def test_show_rejects_noninteger_or_overflow_parameters(field, value):
    identifier = save_original('one\ntwo\n')
    options = {'start':1,'count':1,field:value}
    with pytest.raises(ValueError):
        read_lines(identifier, **options)
    assert events()[0] == []


@pytest.mark.parametrize('field', ['start','end','context','limit','max_chars','char_offset'])
@pytest.mark.parametrize('value', [True, 1.5, '1', 2**63])
def test_grep_rejects_noninteger_or_overflow_parameters(field, value):
    identifier = save_original('needle\nneedle\n')
    with pytest.raises(ValueError):
        grep_lines(identifier, 'needle', **{field:value})
    assert events()[0] == []


@pytest.mark.parametrize('field', ['target','char_offset'])
@pytest.mark.parametrize('value', [True, 1.5, '1', 2**63])
def test_index_rejects_noninteger_or_overflow_cursor(field, value):
    identifier = save_original('one\ntwo\n')
    with pytest.raises(ValueError):
        ensure_index(identifier, artifact_path(identifier), **{field:value})
    assert not index_path(identifier).exists()


def test_failed_cli_recovery_emits_no_changed_content():
    identifier = save_original('original\n')
    artifact_path(identifier).write_bytes(b'PRIVATE ALTERED CONTENT\n')
    cli = Path(__file__).resolve().parents[1]/'tg.py'
    process = subprocess.run([sys.executable,'-X','utf8',str(cli),'show',identifier],
                             capture_output=True,timeout=30)
    assert process.returncode == 2
    assert process.stdout == b''
    assert b'PRIVATE ALTERED CONTENT' not in process.stderr
    assert events()[0] == []


def test_duplicate_save_accepts_competing_verified_publisher():
    raw = 'same original\n'
    def racing(temporary, target):
        # Another cooperating writer has just published identical content.
        Path(target).write_bytes(Path(temporary).read_bytes())
        raise FileExistsError('Another writer published first')
    with patch('tokenguard.storage.os.link', side_effect=racing):
        identifier = save_original(raw)
    assert artifact_path(identifier).read_bytes() == raw.encode('utf-8')
    assert not list(artifact_path(identifier).parent.glob('.tg-*'))


def test_failed_publish_does_not_accept_conflicting_existing_target():
    def conflicting(temporary, target):
        Path(target).write_bytes(b'different bytes')
        raise FileExistsError('Another writer published first')
    with patch('tokenguard.storage.os.link', side_effect=conflicting):
        with pytest.raises(ValueError):
            save_original('original\n')


@pytest.mark.skipif(os.name != 'nt', reason='Actual Windows junction contract')
@pytest.mark.parametrize('kind', ['artifacts','indexes-v04'])
def test_actual_windows_junction_cannot_redirect_artifact_or_index(tmp_path, kind):
    import _winapi
    data=tmp_path/'data'; data.mkdir(exist_ok=True)
    outside=tmp_path/'outside'; outside.mkdir()
    link=data/kind
    _winapi.CreateJunction(str(outside), str(link))
    try:
        with pytest.raises(ValueError):
            (artifact_path if kind=='artifacts' else index_path)('a'*64)
        assert list(outside.iterdir()) == []
    finally:
        os.rmdir(link)  # Remove only the junction itself, never its target.


@pytest.mark.parametrize('attempt', range(3))
def test_concurrent_same_content_saves_are_atomic(attempt):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    barrier = Barrier(8)
    raw = ('compile diagnostic\n'*50000) + str(attempt)
    def writer(_):
        barrier.wait(timeout=30)
        return save_original(raw)
    with ThreadPoolExecutor(max_workers=8) as pool:
        identifiers = list(pool.map(writer, range(24)))
    assert len(set(identifiers)) == 1
    path = artifact_path(identifiers[0])
    assert path.read_bytes() == raw.encode('utf-8')
    assert list(path.parent.glob('.tg-*')) == []


def test_unsupported_atomic_publication_preserves_original_output_contract():
    with patch('tokenguard.storage.os.link', side_effect=OSError('hardlinks unavailable')):
        with pytest.raises(OSError, match='hardlinks unavailable'):
            save_original('original\n')
    folder = artifact_path('a'*64).parent
    assert list(folder.iterdir()) == []


def test_independent_processes_publish_same_artifact_without_overwrite():
    script = ("from tokenguard.storage import save_original; "
              "[print(save_original('diagnostic\\n'*50000)) for _ in range(12)]")
    children = [subprocess.Popen([sys.executable, '-X', 'utf8', '-c', script],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(6)]
    try:
        results = [child.communicate(timeout=60) for child in children]
        for child, (out, err) in zip(children, results):
            assert child.returncode == 0, err.decode('utf-8', errors='replace')
            assert len(out.splitlines()) == 12
        identifiers = {line.decode() for out, _ in results for line in out.splitlines()}
        assert len(identifiers) == 1
        path = artifact_path(identifiers.pop())
        assert path.read_bytes() == ('diagnostic\n'*50000).encode()
        assert list(path.parent.glob('.tg-*')) == []
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=30)


@pytest.mark.skipif(os.name != 'nt', reason='Windows extended-length path spelling')
@pytest.mark.parametrize('unc', [False, True])
def test_canonical_path_normalizes_windows_extended_prefix(tmp_path, unc):
    from tokenguard.visible_tests import canonical_path
    if unc:
        resolved = Path('\\\\?\\UNC\\server\\share\\artifact.txt')
        expected = Path('\\\\server\\share\\artifact.txt')
    else:
        resolved = Path('\\\\?\\' + str(tmp_path))
        expected = tmp_path
    with patch.object(Path, 'resolve', return_value=resolved):
        assert canonical_path(tmp_path) == expected


@pytest.mark.parametrize('keep_changing', [False, True])
def test_duplicate_save_rechecks_content_after_metadata_only_change(monkeypatch, keep_changing):
    raw = 'saved output\n'*10000
    identifier = save_original(raw)
    source = artifact_path(identifier)
    original_open = Path.open
    changed = 0
    class Reader:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def fileno(self):
            return self.stream.fileno()
        def read(self, size):
            nonlocal changed
            data = self.stream.read(size)
            if data and (keep_changing or not changed):
                stamp = source.stat()
                os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns+1000000000))
                changed += 1
            return data
    def opening(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        return Reader(stream) if path == source and args == ('rb',) else stream
    with monkeypatch.context() as scoped:
        scoped.setattr(Path, 'open', opening)
        if keep_changing:
            with pytest.raises(ValueError):
                save_original(raw)
        else:
            assert save_original(raw) == identifier
    assert changed > 0
    assert source.read_bytes() == raw.encode()
