"""Installing from another checkout must replace only managed v0.4 hooks."""
import json
import os
from pathlib import Path
import shlex
import sys

import pytest

from installer import install


SCRIPT = Path(__file__).resolve().parents[1] / 'tokenguard/hook.py'


def command(script: Path = SCRIPT, extra: str = '') -> str:
    paths = [Path(sys.executable).as_posix(), script.as_posix()]
    encoded = ' '.join('"' + path + '"' for path in paths) if os.name == 'nt' else ' '.join(map(shlex.quote, paths))
    return encoded + ' --tokenguard-v04 --source claude_hook' + extra


def settings(tmp_path, commands, matcher='Bash'):
    project = tmp_path / 'project'
    target = project / '.claude/settings.local.json'
    target.parent.mkdir(parents=True)
    data = {'permissions': {'allow': ['Read']}, 'custom': {'keep': True},
            'hooks': {'PreToolUse': [], 'PostToolUse': [
                {'matcher': matcher, 'custom_entry': 'keep', 'hooks': commands}]}}
    original = (json.dumps(data, ensure_ascii=True, indent=2) + '\n').encode()
    target.write_bytes(original)
    return project, target, data, original


def hook(text):
    return {'type': 'command', 'command': text, 'timeout': 10}


def test_moved_checkout_replaces_old_hook_and_preserves_neighbors_and_backup(tmp_path):
    old = tmp_path / 'old checkout/tokenguard/hook.py'
    unrelated = hook('echo unrelated')
    project, target, data, original = settings(tmp_path, [hook(command(old)), unrelated])
    install(project, Path(sys.executable), pytest_mode='indexed')
    updated = json.loads(target.read_text())
    entries = updated['hooks']['PostToolUse']
    assert len(entries) == 2
    assert entries[0] == {**data['hooks']['PostToolUse'][0], 'hooks': [unrelated]}
    assert shlex.split(entries[1]['hooks'][0]['command']) == shlex.split(command(extra=' --pytest-mode indexed'))
    assert updated['permissions'] == data['permissions']
    assert updated['custom'] == data['custom']
    assert updated['hooks']['PreToolUse'] == []
    assert [p.read_bytes() for p in target.parent.glob('*.backup-*')] == [original]


def test_duplicate_paths_collapse_and_reinstallation_stays_idempotent(tmp_path):
    old = tmp_path / 'missing old checkout/tokenguard/hook.py'
    project, target, _, _ = settings(tmp_path, [hook(command(old)), hook(command())])
    install(project, Path(sys.executable))
    once = json.loads(target.read_text())
    install(project, Path(sys.executable))
    assert json.loads(target.read_text()) == once
    assert len(once['hooks']['PostToolUse']) == 1
    assert len(once['hooks']['PostToolUse'][0]['hooks']) == 1


def test_uninstall_removes_managed_v04_from_old_paths_only(tmp_path):
    old = tmp_path / 'old/tokenguard/hook.py'
    legacy = hook(command(old).replace('--tokenguard-v04', '--tokenguard-v03'))
    project, target, _, _ = settings(tmp_path, [hook(command(old)), hook(command()), legacy])
    install(project, Path(sys.executable), uninstall=True)
    assert json.loads(target.read_text())['hooks']['PostToolUse'][0]['hooks'] == [legacy]


@pytest.mark.parametrize('text', [
    'echo ' + command(),
    command() + ' && echo retained',
    command() + ' --other',
    command() + ' --pytest-mode indexed --other',
    command() + ' --pytest-mode unknown',
    command().replace('--source claude_hook', '--source manual'),
    command().replace('--tokenguard-v04', '--tokenguard-v040'),
    command().replace('hook.py', 'hook.py.bak'),
    command() + '"',
    '"python" ' + command().split(' ', 1)[1],
    'C:/Python/python.exe;echo D:/old/tokenguard/hook.py --tokenguard-v04 --source claude_hook',
    '"/usr/bin/python" "/old/$(whoami)/tokenguard/hook.py" --tokenguard-v04 --source claude_hook',
])
def test_uninstall_preserves_commands_outside_managed_shape(tmp_path, text):
    item = hook(text)
    project, target, data, _ = settings(tmp_path, [item])
    install(project, Path(sys.executable), uninstall=True)
    assert json.loads(target.read_text()) == data


def test_uninstall_preserves_other_matchers_and_async_hooks(tmp_path):
    project, target, data, _ = settings(tmp_path, [hook(command())], matcher='Read')
    install(project, Path(sys.executable), uninstall=True)
    assert json.loads(target.read_text()) == data
    data['hooks']['PostToolUse'][0]['matcher'] = 'Bash'
    data['hooks']['PostToolUse'][0]['hooks'][0]['async'] = True
    target.write_text(json.dumps(data))
    install(project, Path(sys.executable), uninstall=True)
    assert json.loads(target.read_text()) == data


@pytest.mark.parametrize('text', [
    '"C:/Python/python.exe" "C:/old checkout/tokenguard/hook.py" --tokenguard-v04 --source claude_hook',
    '"//server/share/python.exe" "//server/share/old checkout/tokenguard/hook.py" --tokenguard-v04 --source claude_hook',
    "/usr/bin/python '/old checkout/tokenguard/hook.py' --tokenguard-v04 --source claude_hook",
])
def test_managed_paths_can_migrate_between_platform_spellings(tmp_path, text):
    project, target, _, _ = settings(tmp_path, [hook(text)])
    install(project, Path(sys.executable), uninstall=True)
    assert json.loads(target.read_text())['hooks']['PostToolUse'] == []
