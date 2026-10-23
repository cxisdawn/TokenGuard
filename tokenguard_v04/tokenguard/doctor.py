"""Offline checks for the project-installed Hook, with no model/host invocation."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import uuid
from typing import Any

from .audit import diagnostics, timestamp


def _installed_argv(project: Path) -> tuple[list[str] | None, str]:
    settings = project / '.claude' / 'settings.local.json'
    if not settings.is_file():
        return None, 'missing_project_settings'
    data = json.loads(settings.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict) or not isinstance(data.get('hooks', {}), dict):
        return None, 'invalid_settings'
    entries = data.get('hooks', {}).get('PostToolUse', [])
    if not isinstance(entries, list):
        return None, 'invalid_hook_entries'
    candidates = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get('matcher') != 'Bash':
            continue
        hooks = entry.get('hooks', [])
        if not isinstance(hooks, list):
            continue
        for hook in hooks:
            if isinstance(hook, dict) and isinstance(hook.get('command'), str) and '--tokenguard-v04' in hook['command']:
                candidates.append(hook)
    if len(candidates) != 1:
        return None, 'missing_v04_hook' if not candidates else 'multiple_v04_hooks'
    hook = candidates[0]
    if hook.get('type') != 'command' or hook.get('async'):
        return None, 'not_synchronous_command'
    argv = shlex.split(hook['command'], posix=True)
    if (len(argv) not in (5, 7) or argv[2:5] != ['--tokenguard-v04', '--source', 'claude_hook'] or
            (len(argv) == 7 and (argv[5] != '--pytest-mode' or argv[6] not in ('compact', 'indexed', 'off')))):
        return None, 'unsupported_command_shape'
    expected_hook = Path(__file__).resolve().parent / 'hook.py'
    # Do not execute arbitrary configuration commands, interpreters or shell expressions.
    if not Path(argv[0]).is_absolute() or Path(argv[0]).resolve() != Path(sys.executable).resolve():
        return None, 'interpreter_differs_from_running_python'
    if not Path(argv[1]).is_absolute() or Path(argv[1]).resolve() != expected_hook:
        return None, 'hook_path_mismatch'
    return argv, 'passed'


def check(project: Path) -> dict[str, Any]:
    project = project.resolve()
    report: dict[str, Any] = {
        'timestamp': timestamp(), 'mode': 'offline_configured_hook_probe',
        'project': str(project), 'configuration': 'unchecked', 'configured_process': 'not_run',
        'compression': 'not_run', 'artifact_integrity': 'not_run',
        'host_acceptance': 'unverified', 'model_api_calls': 0,
        'probe_storage': 'temporary; isolated from live audit',
    }
    try:
        report['recent_diagnostics'] = diagnostics(limit=3)
        argv, status = _installed_argv(project)
        report['configuration'] = status
        if argv is None:
            return report
        with tempfile.TemporaryDirectory(prefix='tg-doctor-') as folder:
            env = dict(os.environ)
            env.update({'TOKENGUARD_DATA_DIR': folder, 'TOKENGUARD_ENABLED': '1',
                        'TOKENGUARD_DIAGNOSTICS': '1', 'TOKENGUARD_EXPERIMENT_ID': 'manual-doctor'})
            fixture = Path(__file__).resolve().parent.parent / 'live_check.py'
            generated = subprocess.run([argv[0], str(fixture)], cwd=project, env=env,
                                       capture_output=True, text=True, encoding='utf-8', timeout=15, check=True)
            raw = generated.stdout
            event = {'hook_event_name': 'PostToolUse', 'session_id': 'manual-doctor-' + uuid.uuid4().hex,
                     'tool_name': 'Bash', 'tool_input': {'command': 'python live_check.py'},
                     'tool_response': {'stdout': raw, 'stderr': 'doctor stderr retained',
                                       'interrupted': False, 'isImage': False, 'extra': {'probe': True}}}
            # Keep the configured interpreter/path/flags, explicitly tag this simulation as manual.
            result = subprocess.run([*argv, '--source', 'manual'], input=json.dumps(event), cwd=project,
                                    env=env, capture_output=True, text=True, encoding='utf-8', timeout=15, check=True)
            report['configured_process'] = 'passed'
            if not result.stdout.strip():
                report['compression'] = 'no_replacement'
                return report
            output = json.loads(result.stdout)['hookSpecificOutput']
            changed = output['updatedToolOutput']
            out = changed['stdout']
            same_fields = {k: v for k, v in changed.items() if k != 'stdout'} == {
                k: v for k, v in event['tool_response'].items() if k != 'stdout'}
            valid = (output['hookEventName'] == 'PostToolUse' and same_fields and
                     len(out) < len(raw)*0.85 and 'TG_LIVE_CHECK_DONE lines=400' in out)
            report['compression'] = 'passed' if valid else 'failed'
            report['original_chars'] = len(raw)
            report['optimized_chars'] = len(out)
            audit = [json.loads(line) for line in (Path(folder)/'audit-v04.jsonl').read_text(encoding='utf-8').splitlines()]
            row = next(r for r in audit if r.get('event') == 'compression')
            artifact = Path(folder)/'artifacts'/(row['artifact_id']+'.txt')
            report['artifact_integrity'] = 'passed' if artifact.read_bytes() == raw.encode('utf-8') else 'failed'
            report['probe_source'] = row['source']
    except (ValueError, OSError, TypeError, KeyError, StopIteration, subprocess.SubprocessError) as exc:
        report['error_type'] = type(exc).__name__
        # No command, stderr or raw settings in error reports; these may contain private data.
    return report
