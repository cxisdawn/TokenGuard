"""Offline CLI protocol stand-in for runner tests. No model/network/Docker calls."""
import json
import os
from pathlib import Path
import sys

base = Path(os.environ['TG_FIXTURE_BASE'])
sys.path.insert(0, str(base))
from tokenguard.experiment_validation import run_process

session = sys.argv[sys.argv.index('--session-id') + 1]
mode = os.environ.get('TG_FIXTURE_MODE', 'quick')
root = Path.cwd()
prompt = sys.stdin.read()
(root.parent / 'received-prompt.txt').write_bytes(prompt.encode('utf-8'))


def emit(row):
    print(json.dumps({'session_id': session, **row}, ensure_ascii=True), flush=True)


if mode in ('native', 'controlled'):
    steps = ('before', 'after') if mode == 'native' else ('after',)
    for step in steps:
        if step == 'after':
            source = root / 'pylint/arithmetic.py'
            source.write_bytes(source.read_bytes().replace(b'a // b', b'a / b'))
        command = '"' + sys.executable.replace('\\', '/') + '" tests/pytest.py'
        emit({'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'id': step, 'name': 'Bash', 'input': {'command': command}}]}})
        run = run_process([sys.executable, str(root / 'tests/pytest.py')], root,
                          {**os.environ, 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}, 60)
        response = {'stdout': run['stdout'], 'stderr': run['stderr'], 'exit_code': run['exit_code']}
        if os.environ['TOKENGUARD_ENABLED'] == '1':
            event = {'hook_event_name': 'PostToolUse', 'tool_name': 'Bash', 'session_id': session,
                     'tool_use_id': step, 'tool_input': {'command': command}, 'tool_response': response}
            hook = run_process([sys.executable, str(base / 'tokenguard/hook.py'), '--source', 'claude_hook'],
                               root, dict(os.environ), 30, json.dumps(event))
            if hook['stdout'].strip():
                response = json.loads(hook['stdout'])['hookSpecificOutput']['updatedToolOutput']
        emit({'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': step, 'content': response['stdout'],
             'is_error': run['exit_code'] != 0}]}})

result = {'type': 'result', 'is_error': False, 'subtype': 'success', 'num_turns': 2, 'usage': {}}
if mode == 'bad_json':
    source = root / 'pylint/arithmetic.py'
    source.write_bytes(source.read_bytes().replace(b'a // b', b'a / b'))
    print('{malformed stream row', flush=True)
if mode == 'bad_usage':
    result.update(usage=None, modelUsage=['invalid shape'], permission_denials=None)
emit(result)
