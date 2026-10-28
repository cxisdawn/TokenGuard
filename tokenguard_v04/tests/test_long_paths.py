"""Real Windows long paths must preserve artifact identity and link checks."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from tokenguard.artifact_integrity import integrity_path
from tokenguard.audit import events
from tokenguard.hook import handle
from tokenguard.line_index import index_path
from tokenguard.storage import artifact_path, grep_lines, read_lines, save_original


def extended(path):
    """Independent test fixture spelling; no production conversion involved."""
    value = str(path.absolute())
    return Path(value if value.startswith('\\\\?\\') else '\\\\?\\' + value)


@pytest.fixture
def deep_data(tmp_path, monkeypatch):
    if os.name != 'nt':
        pytest.skip('Actual Windows filesystem long-path contract')
    owned = tmp_path / 'owned-long-paths'

    def make(length=270, unicode=False):
        path = owned / ('unicode' if unicode else 'ascii') / str(length)
        # Keep each component below NTFS's separate 255 UTF-16-unit limit.
        units = lambda value: len(str(value).encode('utf-16-le')) // 2
        target = max(length, units(path) + 10)
        while units(path) < target:
            width = min(40, target - units(path) - 1)
            if width < 1:
                break
            name = ('\U0001f600' * (width // 2) + 'x' * (width % 2)) if unicode else 'x' * width
            path /= name
        extended(path).mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv('TOKENGUARD_DATA_DIR', str(path))
        monkeypatch.setenv('TOKENGUARD_INDEX_ENABLED', '1')
        monkeypatch.setenv('TOKENGUARD_ENABLED', '1')
        monkeypatch.setenv('TOKENGUARD_SOURCE', 'manual')
        return path

    yield make
    # Remove only this newly created test tree, with a checked owned root.
    assert owned.absolute().parent == tmp_path.absolute()
    if extended(owned).exists():
        shutil.rmtree(extended(owned))


@pytest.mark.parametrize('length', [183, 270, 350])
def test_actual_long_path_save_and_duplicate_publish(deep_data, length):
    deep_data(length)
    raw = 'original \u4e2d\u6587\r\n' * 100
    identifier = save_original(raw)
    assert extended(artifact_path(identifier)).read_bytes() == raw.encode('utf-8')
    assert save_original(raw) == identifier
    assert not list(extended(artifact_path(identifier)).parent.glob('.tg-*'))


@pytest.mark.parametrize('length', [183, 270, 350])
def test_existing_long_artifact_is_verified_before_reuse(deep_data, length):
    root = deep_data(length)
    raw = 'existing original\r\n'
    identifier = hashlib.sha256(raw.encode()).hexdigest()
    source = extended(root / 'artifacts' / (identifier + '.txt'))
    source.parent.mkdir(exist_ok=True)
    source.write_bytes(raw.encode())
    assert save_original(raw) == identifier
    source.write_bytes(b'conflicting content\r\n')
    with pytest.raises(ValueError):
        save_original(raw)
    assert source.read_bytes() == b'conflicting content\r\n'


@pytest.mark.parametrize('unicode', [False, True])
def test_long_recovery_builds_and_reuses_both_sidecars(deep_data, unicode):
    root = deep_data(300, unicode=unicode)
    raw = 'normal\r\n' * 400 + 'TARGET \u4e2d\u6587\r\n'
    identifier = hashlib.sha256(raw.encode('utf-8')).hexdigest()
    source = extended(root / 'artifacts' / (identifier + '.txt'))
    source.parent.mkdir(exist_ok=True)
    source.write_bytes(raw.encode('utf-8'))
    assert read_lines(identifier, 401, 1) == 'TARGET \u4e2d\u6587\r\n'
    assert extended(index_path(identifier)).exists()
    assert extended(integrity_path(identifier)).exists()
    assert grep_lines(identifier, 'TARGET', start=401, context=0) == '401: TARGET \u4e2d\u6587\r\n'
    rows, malformed = events()
    assert malformed == 0
    assert rows[-2]['integrity_status'] == 'built'
    assert rows[-1]['integrity_status'] == 'reused'
    assert rows[-1]['index_status'] == 'reused'
    source.write_bytes(raw.replace('TARGET', 'BROKEN').encode('utf-8'))
    with pytest.raises(ValueError):
        read_lines(identifier, 401, 1)
    assert events()[0] == rows


def test_actual_long_path_concurrent_publish(deep_data):
    deep_data(300)
    raw = 'same original\n' * 5000
    with ThreadPoolExecutor(max_workers=6) as pool:
        identifiers = list(pool.map(lambda _: save_original(raw), range(18)))
    assert len(set(identifiers)) == 1
    path = extended(artifact_path(identifiers[0]))
    assert path.read_bytes() == raw.encode()
    assert list(path.parent.glob('.tg-*')) == []


def test_long_path_publish_failure_still_falls_back(deep_data, monkeypatch):
    deep_data(300)
    raw = 'heartbeat\r\n' * 500
    event = {'tool_name': 'Bash', 'tool_input': {'command': 'cmake --build out'},
             'tool_response': {'stdout': raw, 'stderr': 'unchanged'}}
    def denied(*args, **kwargs):
        raise PermissionError('Synthetic atomic publish denial')
    monkeypatch.setattr('tokenguard.storage.os.link', denied)
    assert handle(event) is None
    assert event['tool_response']['stdout'] == raw
    assert not events()[0]
    assert not list(extended(artifact_path('a' * 64)).parent.glob('.tg-*'))


def test_long_path_access_denial_is_not_treated_as_missing(deep_data, monkeypatch):
    root = deep_data(300)
    original = Path.lstat
    def denied(path, *args, **kwargs):
        if path.name == 'artifacts':
            raise PermissionError('Synthetic directory inspection denial')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', denied)
    with pytest.raises(PermissionError):
        artifact_path('a' * 64)
    assert not extended(root / 'artifacts').exists()


@pytest.mark.parametrize('length', [240, 247])
def test_audit_file_and_lock_cross_threshold_independently(deep_data, length):
    deep_data(length)
    raw = 'heartbeat\r\n' * 500
    event = {'tool_name': 'Bash', 'tool_input': {'command': 'cmake --build out'},
             'session_id': 'manual-audit-threshold', 'tool_response': {'stdout': raw, 'stderr': ''}}
    assert handle(event) is not None
    rows, malformed = events()
    assert malformed == 0 and len(rows) == 1
    assert rows[0]['session_id'] == 'manual-audit-threshold'
    assert read_lines(rows[0]['artifact_id'], 1, 1) == 'heartbeat\r\n'
    assert events()[0][-1]['event'] == 'recovery'


@pytest.mark.parametrize('kind', ['artifacts', 'indexes-v04', 'integrity-v04'])
def test_real_long_junction_is_rejected(deep_data, tmp_path, kind):
    import _winapi
    root = deep_data(300)
    outside = tmp_path / 'outside'
    outside.mkdir()
    link = extended(root / kind)
    _winapi.CreateJunction(str(outside), str(link))
    try:
        get_path = {'artifacts': artifact_path, 'indexes-v04': index_path, 'integrity-v04': integrity_path}[kind]
        with pytest.raises(ValueError, match='links|reparse'):
            get_path('a' * 64)
        assert list(outside.iterdir()) == []
    finally:
        os.rmdir(link)  # Only remove this test's junction, never its target.


def test_hook_and_recovery_cli_in_long_data_directory(deep_data):
    root = deep_data(300)
    version = Path(__file__).resolve().parents[1]
    detail = 'fatal: missing_symbol \u4e2d\u6587\r\n'
    raw = 'compile heartbeat\r\n' * 500 + detail
    event = {'hook_event_name': 'PostToolUse', 'tool_name': 'Bash', 'session_id': 'manual-long-path',
             'tool_input': {'command': 'cmake --build out'},
             'tool_response': {'stdout': raw, 'stderr': 'compiler stderr\r\n', 'exit_code': 1}}
    process = subprocess.run([sys.executable, '-X', 'utf8', str(version / 'tokenguard' / 'hook.py'),
                              '--source', 'manual'], input=json.dumps(event).encode(), capture_output=True, timeout=30)
    assert process.returncode == 0, process.stderr
    assert process.stdout, process.stderr
    changed = json.loads(process.stdout)['hookSpecificOutput']['updatedToolOutput']
    assert detail in changed['stdout'] and len(changed['stdout']) < len(raw) * 0.85
    assert changed['stderr'] == event['tool_response']['stderr']
    assert changed['exit_code'] == 1
    row = next(item for item in events()[0] if item['event'] == 'compression')
    assert extended(root / 'artifacts' / (row['artifact_id'] + '.txt')).read_bytes() == raw.encode('utf-8')
    process = subprocess.run([sys.executable, '-X', 'utf8', str(version / 'tg.py'), 'show', row['artifact_id'],
                              '--start', '501', '--count', '1', '--compression-id', row['compression_id']],
                             capture_output=True, timeout=30)
    assert process.returncode == 0, process.stderr
    assert process.stdout == detail.encode('utf-8')
    assert events()[0][-1]['compression_id'] == row['compression_id']
