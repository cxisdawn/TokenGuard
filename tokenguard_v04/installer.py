"""Project-local hook installer; preserves unrelated hooks and permissions."""
import argparse
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import uuid


def install(project: Path, python: Path, uninstall: bool = False, *, pytest_mode: str = 'compact') -> Path:
    if pytest_mode not in ('compact', 'indexed', 'off'):
        raise ValueError('Unknown pytest mode')
    project = project.resolve()
    if project == Path.home().resolve() or project == (Path.home()/'.claude').resolve():
        raise ValueError('Global/user Claude configuration cannot be targeted')
    target = project / '.claude' / 'settings.local.json'
    if (project/'.claude').is_symlink() or target.is_symlink() or (project/'.claude').resolve().parent != project:
        raise ValueError('Configuration links forbidden')
    script = Path(__file__).resolve().parent / 'tokenguard' / 'hook.py'
    settings = json.loads(target.read_text(encoding='utf-8-sig')) if target.exists() else {}
    if not isinstance(settings, dict):
        raise ValueError('Settings must be an object')
    hooks = settings.setdefault('hooks', {})
    entries = hooks.setdefault('PostToolUse', [])
    if not isinstance(entries, list):
        raise ValueError('PostToolUse must be an array')
    marker = '--tokenguard-v04'
    kept = []
    for entry in entries:
        def owned(h: dict) -> bool:
            command = h.get('command', '')
            return marker in command and script.as_posix() in command
        if not any(owned(h) for h in entry.get('hooks', [])):
            kept.append(entry)
            continue
        remaining = [h for h in entry.get('hooks', []) if not owned(h)]
        if remaining:
            kept.append({**entry, 'hooks': remaining})
    if not uninstall:
        if not python.is_file():
            raise ValueError('Python executable does not exist')
        paths = [python.resolve().as_posix(), script.as_posix()]
        if os.name == 'nt':
            if any(any(c in p for c in '\n\r"$`%') for p in paths):
                raise ValueError('Unsupported shell metacharacters in executable path')
            command = ' '.join('"'+p+'"' for p in paths)
        else:
            command = ' '.join(shlex.quote(p) for p in paths)
        mode = '' if pytest_mode == 'compact' else ' --pytest-mode ' + pytest_mode
        kept.append({'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': command+' '+marker+' --source claude_hook'+mode, 'timeout': 10}]})
    hooks['PostToolUse'] = kept
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup = target.with_name(target.name+'.backup-'+uuid.uuid4().hex)
        backup.write_bytes(target.read_bytes())
    fd, temp = tempfile.mkstemp(dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(settings, stream, indent=2, ensure_ascii=True)
            stream.write('\n')
        os.replace(temp, target)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--python', type=Path, default=Path(sys.executable))
    parser.add_argument('--uninstall', action='store_true')
    parser.add_argument('--pytest-mode', choices=['compact', 'indexed', 'off'], default='compact')
    args = parser.parse_args()
    print(install(args.project, args.python, args.uninstall, pytest_mode=args.pytest_mode))

if __name__ == '__main__':
    main()
