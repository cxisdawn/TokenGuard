"""Exercise the actual subprocess boundary without launching an agent/model."""
import io
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

from tokenguard import hook
from tokenguard.audit import events


HOOK = Path(hook.__file__).resolve()


def payload():
    return {'hook_event_name': 'PostToolUse', 'tool_name': 'Bash',
            'session_id': 'delivery-session', 'tool_use_id': 'tool-1',
            'tool_input': {'command': 'pytest -v'},
            'tool_response': {'stdout': ''.join(
                f'test_x.py::test_value[{i}] PASSED [100%]\n' for i in range(400))
                + '===== 400 passed in 0.1s =====\n', 'stderr': 'retain me', 'extra': 42}}


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv('TOKENGUARD_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('TOKENGUARD_ENABLED', '1')
    monkeypatch.setenv('TOKENGUARD_SOURCE', 'manual')
    monkeypatch.setenv('TOKENGUARD_DIAGNOSTICS', '1')
    monkeypatch.delenv('TOKENGUARD_EXPERIMENT_ID', raising=False)
    return tmp_path


def run_hook(value):
    return subprocess.run([sys.executable, '-X', 'utf8', str(HOOK), '--source', 'manual'],
                          input=json.dumps(value), text=True, encoding='utf-8',
                          capture_output=True, timeout=15)


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -float('inf')])
def test_non_json_numbers_fail_open_before_artifact(isolated, value):
    event = payload()
    event['tool_response']['extra'] = value
    proc = run_hook(event)
    assert proc.returncode == 0 and proc.stdout == ''
    assert events()[0] == []
    assert not list(isolated.glob('artifacts/*'))
    assert hook.handle(event) is None


@pytest.mark.parametrize('key,value', [('session_id', {}), ('tool_use_id', []),
                                      ('session_id', 'x\nprivate'), ('tool_use_id', 'x' * 257)])
def test_identity_rejected_and_not_copied_to_diagnostics(isolated, key, value):
    event = payload()
    event[key] = value
    proc = run_hook(event)
    assert proc.returncode == 0 and proc.stdout == ''
    assert events()[0] == []
    rows, _ = events('diagnostics-v04.jsonl')
    assert rows[-1]['status'] == 'invalid_metadata'
    assert rows[-1][key] is None


def test_success_receipt_follows_candidate_and_preserves_shape(isolated):
    event = payload()
    proc = run_hook(event)
    assert proc.returncode == 0
    result = json.loads(proc.stdout)['hookSpecificOutput']['updatedToolOutput']
    assert {k: v for k, v in result.items() if k != 'stdout'} == {'stderr': 'retain me', 'extra': 42}
    rows, malformed = events()
    assert malformed == 0
    assert [r['event'] for r in rows] == ['compression', 'replacement_delivery']
    assert rows[0]['replacement_status'] == 'prepared_candidate_not_host_confirmed'
    assert rows[1]['status'] == 'stdout_write_succeeded'
    assert rows[0]['compression_id'] == rows[1]['compression_id']
    assert rows[1]['session_id'] == 'delivery-session'


def test_direct_handle_only_prepares_candidate(isolated):
    assert hook.handle(payload()) is not None
    rows, _ = events()
    assert len(rows) == 1
    assert rows[0]['replacement_status'] == 'prepared_candidate_not_host_confirmed'


def test_duplicate_json_identity_is_rejected(isolated):
    raw = json.dumps(payload()).replace('"session_id": "delivery-session"',
                                       '"session_id": "other", "session_id": "delivery-session"')
    proc = subprocess.run([sys.executable, '-X', 'utf8', str(HOOK)], input=raw,
                          text=True, encoding='utf-8', capture_output=True, timeout=15)
    assert proc.returncode == 0 and proc.stdout == ''
    assert events()[0] == []


def test_closed_pipe_is_fail_open_and_records_write_failure(isolated):
    proc = subprocess.Popen([sys.executable, '-X', 'utf8', str(HOOK), '--source', 'manual'],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None and proc.stdin is not None and proc.stderr is not None
    proc.stdout.close()
    proc.stdin.write(json.dumps(payload()).encode('utf-8'))
    proc.stdin.close()
    assert proc.wait(timeout=15) == 0
    error = proc.stderr.read().decode('utf-8')
    proc.stderr.close()
    assert 'Traceback' not in error and 'Exception ignored' not in error
    rows, _ = events()
    assert rows[-1]['event'] == 'replacement_delivery'
    assert rows[-1]['status'] == 'stdout_write_failed'
    diagnostics, _ = events('diagnostics-v04.jsonl')
    assert diagnostics[-1]['session_id'] == 'delivery-session'
    assert diagnostics[-1]['status'].startswith('output_')


def test_receipt_failure_cannot_retract_already_written_candidate(isolated):
    real_append = hook.append
    def fail_receipt(row, *args, **kwargs):
        if row['event'] == 'replacement_delivery':
            raise OSError('private message')
        return real_append(row, *args, **kwargs)
    output = io.StringIO()
    errors = io.StringIO()
    with patch.object(sys, 'argv', ['hook.py', '--source', 'manual']), \
         patch.object(sys, 'stdin', io.StringIO(json.dumps(payload()))), \
         patch.object(sys, 'stdout', output), patch.object(sys, 'stderr', errors), \
         patch.object(hook, 'append', side_effect=fail_receipt):
        hook.main()
    assert json.loads(output.getvalue())['hookSpecificOutput']['updatedToolOutput']
    rows, _ = events()
    assert len(rows) == 1
    assert 'private message' not in errors.getvalue()
    diagnostics, _ = events('diagnostics-v04.jsonl')
    assert diagnostics[-1]['status'] == 'delivery_receipt_OSError'


@pytest.mark.parametrize('failure', ['short_write', 'partial_exception', 'flush'])
@pytest.mark.parametrize('devnull_failure', [False, True])
def test_output_fault_receipts_and_secondary_sink_failure(isolated, failure, devnull_failure):
    class FailedStream:
        written = ''

        def write(self, text):
            self.written += text if failure == 'flush' else text[:20]
            if failure == 'partial_exception':
                raise BrokenPipeError('private message')
            return len(text) if failure == 'flush' else 20

        def flush(self):
            raise OSError('private flush')

        def fileno(self):
            raise io.UnsupportedOperation('no fd')

        def close(self):
            raise OSError('private close')

    stream = FailedStream()
    errors = io.StringIO()
    # Patch the module name only: audit persistence still uses normal file IO.
    real_open = open
    def sink_open(*args, **kwargs):
        if devnull_failure:
            raise OSError('private exhausted descriptors')
        return real_open(*args, **kwargs)
    with patch.object(sys, 'argv', ['hook.py']), \
         patch.object(sys, 'stdin', io.StringIO(json.dumps(payload()))), \
         patch.object(sys, 'stdout', stream), patch.object(sys, 'stderr', errors), \
         patch.object(hook, 'open', side_effect=sink_open, create=True):
        hook.main()
    rows, _ = events()
    assert rows[-1]['event'] == 'replacement_delivery'
    assert rows[-1]['status'] == 'stdout_write_failed'
    assert rows[-1]['session_id'] == 'delivery-session'
    diagnostics, _ = events('diagnostics-v04.jsonl')
    assert diagnostics[-1]['status'].startswith('output_')
    assert 'private' not in errors.getvalue()
    # Bytes already accepted cannot be withdrawn, even though delivery failed.
    assert len(stream.written) == 20 if failure != 'flush' else len(stream.written) > 20


def test_unsupported_artifact_filesystem_leaves_hook_response_untouched(isolated):
    import copy
    event = payload()
    before = copy.deepcopy(event)
    with patch('tokenguard.storage.os.link', side_effect=OSError('unsupported filesystem')):
        assert hook.handle(event) is None
    assert event == before
    assert events()[0] == []
    assert list((isolated/'artifacts').iterdir()) == []
