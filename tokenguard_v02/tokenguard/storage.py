"""Local artifact recovery, with bounded reading and path-safe IDs."""
import hashlib
import os
import re
from pathlib import Path

ID_RE = re.compile(r'^[0-9a-f]{64}$')

def root_dir() -> Path:
    return Path(os.environ.get('TOKENGUARD_DATA_DIR', str(Path.home() / '.tokenguard')))

def save_original(text: str) -> str:
    identifier = hashlib.sha256(text.encode('utf-8')).hexdigest()
    folder = root_dir() / 'artifacts'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (identifier + '.txt')
    if not path.exists():
        path.write_text(text, encoding='utf-8')
        try:
            if os.name != 'nt':
                path.chmod(0o600)
        except OSError:
            pass
    return identifier

def artifact_path(identifier: str) -> Path:
    if not ID_RE.fullmatch(identifier):
        raise ValueError('Artifact ID must be exactly 64 lowercase SHA256 hex characters')
    return root_dir() / 'artifacts' / (identifier + '.txt')

def read_lines(identifier: str, start: int, count: int, max_count: int = 100) -> str:
    if start < 1 or count < 1 or count > max_count:
        raise ValueError(f'start >= 1 and count 1..{max_count} required')
    with artifact_path(identifier).open('r', encoding='utf-8') as f:
        return ''.join(line for n, line in enumerate(f, 1) if start <= n < start + count)

def grep_lines(identifier: str, pattern: str, context: int = 1, limit: int = 20) -> str:
    if len(pattern) > 200 or not pattern:
        raise ValueError('pattern must have 1..200 characters')
    if context < 0 or context > 5 or limit < 1 or limit > 50:
        raise ValueError('context 0..5; limit 1..50')
    # Literal matching (not regex) to avoid expensive regex patterns.
    with artifact_path(identifier).open('r', encoding='utf-8') as f:
        lines = f.readlines()
    found = [i for i, line in enumerate(lines) if pattern.casefold() in line.casefold()][:limit]
    indices = set()
    for i in found:
        indices.update(range(max(0, i - context), min(len(lines), i + context + 1)))
    return ''.join(f'{i+1}: {lines[i]}' for i in sorted(indices)) or '(no matches)\n'
