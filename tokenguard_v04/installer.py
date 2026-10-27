"""Project-local hook installer; preserves unrelated hooks and permissions."""
import argparse
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shlex
import sys
import tempfile
import uuid


def _managed_v04(hook: object) -> bool:
    """Recognize our exact command contract across checkout moves, without running it."""
    if (not isinstance(hook, dict) or hook.get('type') != 'command' or
            hook.get('async', False) is not False or not isinstance(hook.get('command'), str)):
        return False
    try:
        argv = shlex.split(hook['command'], posix=True)
    except ValueError:
        return False
    if (len(argv) not in (5, 7) or argv[2:5] != ['--tokenguard-v04', '--source', 'claude_hook'] or
            (len(argv) == 7 and (argv[5] != '--pytest-mode' or argv[6] not in ('compact', 'indexed', 'off')))):
        return False
    # Paths need not still exist: cleaning a moved/deleted installation is the
    # purpose of migration. Require our actual serialization as well as argv:
    # shlex alone loses shell operators and single/double-quote semantics.
    paths = argv[:2]
    if all(PureWindowsPath(path).is_absolute() for path in paths):
        if any(path != PureWindowsPath(path).as_posix() or
               any(c in path for c in '\n\r"$`%') for path in paths):
            return False
        encoded = ' '.join('"' + path + '"' for path in paths)
        tail = tuple(part.casefold() for part in PureWindowsPath(paths[1]).parts[-2:])
    elif all(PurePosixPath(path).is_absolute() for path in paths):
        encoded = ' '.join(map(shlex.quote, paths))
        tail = PurePosixPath(paths[1]).parts[-2:]
    else:
        return False
    return (tail == ('tokenguard', 'hook.py') and
            hook['command'] == encoded + ' ' + ' '.join(argv[2:]))


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
    if not isinstance(hooks, dict):
        raise ValueError('Hooks must be an object')
    entries = hooks.setdefault('PostToolUse', [])
    if not isinstance(entries, list):
        raise ValueError('PostToolUse must be an array')
    marker = '--tokenguard-v04'
    kept = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get('hooks', []), list):
            raise ValueError('Hook entries must be objects with hook arrays')
        if entry.get('matcher') != 'Bash' or not any(_managed_v04(h) for h in entry.get('hooks', [])):
            kept.append(entry)
            continue
        remaining = [h for h in entry.get('hooks', []) if not _managed_v04(h)]
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
