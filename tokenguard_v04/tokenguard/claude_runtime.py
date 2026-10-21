"""Resolve a native Claude binary without executing Windows shell wrappers."""
import os
from pathlib import Path
import shutil


class ClaudeExecutableError(ValueError):
    def __init__(self, code: str):
        self.code=code
        super().__init__(code)


def resolve_claude(executable: str = 'claude', windows: bool | None = None) -> dict[str,str]:
    found=shutil.which(executable)
    if not found:
        raise ClaudeExecutableError('executable_not_found')
    selected=Path(found).absolute()
    is_windows=os.name=='nt' if windows is None else windows
    if not is_windows or selected.suffix.lower()=='.exe':
        return {'executable':str(selected.resolve()),'resolution':'direct_executable'}
    # Known npm installation layout only. Never parse or invoke a .cmd/.ps1 file.
    if selected.name.lower() not in ('claude','claude.cmd','claude.ps1'):
        raise ClaudeExecutableError('unsupported_shell_launcher')
    package=selected.parent/'node_modules'/'@anthropic-ai'/'claude-code'
    native=package/'bin'/'claude.exe'
    if not native.is_file() or native.is_symlink() or not native.resolve().is_relative_to(package.resolve()):
        raise ClaudeExecutableError('npm_native_binary_missing_or_linked')
    return {'executable':str(native.resolve()),'resolution':'npm_native_binary'}
