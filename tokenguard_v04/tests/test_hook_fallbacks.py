"""Real Hook processes: disabling and filesystem faults must emit no replacement."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def fixtures():
    return [
        ('pytest', 'pytest -v', ''.join(f'test_x.py::test_x[{i}] PASSED\n' for i in range(400))
         + '=== 400 passed in 0.1s ===\n'),
        ('unittest', 'python -m unittest -v', ''.join(
            f'test_{i} (test_mod.C.test_{i}) ... ok\n' for i in range(400))
         + '\nRan 400 tests in 0.1s\n\nOK\n'),
        ('search', 'rg --files', ''.join(f'project/module_{i}.py\n' for i in range(400))),
        ('pip', 'python -m pip install fixture', '  ━━━━━━━━━━ 1/2 MB 5 MB/s\n' * 400
         + 'Successfully installed fixture-1.0\n'),
        ('build', 'cmake --build out', 'compile step complete\n' * 400),
        ('warnings', 'python diagnostics.py', 'WARNING: synthetic advisory\n' * 400),
        ('generic', 'python diagnostics.py', 'phase complete\n' * 400),
    ]


@pytest.mark.parametrize('name,command,raw', fixtures(), ids=[item[0] for item in fixtures()])
@pytest.mark.parametrize('boundary', ['enabled', 'disabled', 'data_file', 'artifact_file', 'audit_directory'])
def test_actual_process_shutdown_and_storage_failures(tmp_path, name, command, raw, boundary):
    data = tmp_path / 'data'
    data.mkdir()
    original = b'existing user artifact must remain\r\n'
    existing = data / 'preserved.bin'
    existing.write_bytes(original)
    if boundary == 'data_file':
        data = tmp_path / 'blocked-data'
        data.write_bytes(original)
    elif boundary == 'artifact_file':
        (data / 'artifacts').write_bytes(original)
    elif boundary == 'audit_directory':
        (data / 'audit-v04.jsonl').mkdir()
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('TOKENGUARD_') and key != 'PYTHONPATH'}
    env.update(TOKENGUARD_DATA_DIR=str(data), TOKENGUARD_DIAGNOSTICS='1',
               TOKENGUARD_ENABLED='0' if boundary == 'disabled' else '1', PYTHONUTF8='1')
    response = {'stdout': raw, 'stderr': 'ERROR: private fixture detail\r\n',
                'exit_code': 1, 'extra': {'retain': ['项目', None, False]}}
    event = {'hook_event_name': 'PostToolUse', 'session_id': 'manual-fallback',
             'tool_name': 'Bash', 'tool_input': {'command': command}, 'tool_response': response}
    proc = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(ROOT / 'tokenguard/hook.py'),
                           '--source', 'manual'], input=json.dumps(event).encode('utf-8'),
                          env=env, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert existing.read_bytes() == original
    if boundary == 'enabled':
        assert proc.stderr == b''
        changed = json.loads(proc.stdout)['hookSpecificOutput']['updatedToolOutput']
        assert len(changed['stdout']) < len(raw) * .85
        assert {key: value for key, value in changed.items() if key != 'stdout'} == {
            key: value for key, value in response.items() if key != 'stdout'}
        identifier = hashlib.sha256(raw.encode('utf-8')).hexdigest()
        assert (data / 'artifacts' / (identifier + '.txt')).read_bytes() == raw.encode('utf-8')
    else:
        # Empty Hook stdout asks the host to keep its original response. These
        # subprocess tests do not independently observe a live host consuming it.
        assert proc.stdout == b''
        assert b'private' not in proc.stderr
        assert b'Traceback' not in proc.stderr
        if boundary == 'disabled':
            assert proc.stderr == b''
            assert not (data / 'artifacts').exists()
            assert not (data / 'audit-v04.jsonl').exists()
            diagnostics = [json.loads(line) for line in (data / 'diagnostics-v04.jsonl').read_text(
                encoding='utf-8').splitlines()]
            assert len(diagnostics) == 1 and diagnostics[0]['status'] == 'disabled'
        else:
            assert proc.stderr.startswith(b'TokenGuard fail-open: ')
            if boundary == 'audit_directory':
                assert list((data / 'audit-v04.jsonl').iterdir()) == []
                identifier = hashlib.sha256(raw.encode('utf-8')).hexdigest()
                assert (data / 'artifacts' / (identifier + '.txt')).read_bytes() == raw.encode('utf-8')
            elif boundary == 'artifact_file':
                assert (data / 'artifacts').read_bytes() == original
                assert not (data / 'audit-v04.jsonl').exists()
            else:
                assert data.read_bytes() == original
            if boundary != 'data_file':
                diagnostics = [json.loads(line) for line in (data / 'diagnostics-v04.jsonl').read_text(
                    encoding='utf-8').splitlines()]
                assert diagnostics[-1]['status'].startswith('exception_')
                assert 'private' not in json.dumps(diagnostics)
