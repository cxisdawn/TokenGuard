"""Conservative reversible line compression; no external dependencies."""
import re

PROTECTED = re.compile(r'error|fail|exception|traceback|warning|assert|fatal|denied|critical|^\s*at\s+|^\s*File\s+"', re.I)

def compress(text: str, min_chars: int = 3000, max_ratio: float = 0.85) -> tuple[str, str]:
    if len(text) < min_chars:
        return text, 'below_threshold'
    lines = text.splitlines(keepends=True)
    result = []
    last = None
    repeats = 0
    for line in lines:
        if line == last and not PROTECTED.search(line):
            repeats += 1
            continue
        if repeats:
            result.append(f'[TokenGuard: {repeats} identical adjacent non-critical lines hidden]\n')
            repeats = 0
        result.append(line)
        last = line
    if repeats:
        result.append(f'[TokenGuard: {repeats} identical adjacent non-critical lines hidden]\n')
    output = ''.join(result)
    if len(output) >= len(text) * max_ratio:
        return text, 'insufficient_saving'
    return output, 'adjacent_duplicates'
