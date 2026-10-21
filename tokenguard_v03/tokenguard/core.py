"""TokenGuard v0.3: conservative, local, recoverable tool output compression."""
import re

PROTECTED = re.compile(r'error|fail|exception|traceback|assert|fatal|denied|critical|^\s*at\s+|^\s*File\s+"', re.I)
# pytest -v canonical progress lines. Do not touch a row with warnings, failures or unknown text.
PYTEST_PASS = re.compile(r'^\s*\S+::\S+\s+PASSED(?:\s+\[\s*\d+%\])?\s*$', re.I)
PYTEST_SUMMARY = re.compile(r'=+\s*(?:\d+\s+passed|\d+\s+failed|\d+\s+error|\d+\s+skipped)', re.I)
TRACEBACK = re.compile(r'Traceback \(most recent call last\)|ExceptionGroup Traceback', re.I)
DIFF_HEADER = re.compile(r'(?m)^diff --git |^@@ -\d+(?:,\d+)? \+\d+')
WARNING = re.compile(r'\bwarning\b', re.I)


def _rle(text: str, allow_warning: bool = False) -> str:
    result = []
    previous = None
    count = 0
    def flush():
        nonlocal count
        if count:
            result.append(f'[TokenGuard: {count} identical adjacent non-critical lines hidden]\n')
            count = 0
    for line in text.splitlines(keepends=True):
        protected = PROTECTED.search(line) or (WARNING.search(line) and not allow_warning)
        if line == previous and not protected:
            count += 1
            continue
        flush()
        result.append(line)
        previous = line
    flush()
    return ''.join(result)


def _pytest(text: str) -> str:
    # Summaries needed for safe checking; absent/incomplete command output stays unchanged.
    if not PYTEST_SUMMARY.search(text):
        return text
    lines = text.splitlines(keepends=True)
    result = []
    run = 0
    def flush():
        nonlocal run
        if run:
            result.append(f'[TokenGuard: {run} pytest PASSED lines hidden; original retrievable]\n')
            run = 0
    for line in lines:
        if PYTEST_PASS.fullmatch(line.rstrip('\r\n')):
            run += 1
        else:
            flush()
            result.append(line)
    flush()
    return ''.join(result)


def compress(text: str, min_chars: int = 3000, max_ratio: float = 0.85,
             command: str = '') -> tuple[str, str]:
    if len(text) < min_chars:
        return text, 'below_threshold'
    # Never manipulate patch syntax or exception traces: exact bytes can matter.
    if DIFF_HEADER.search(text) or TRACEBACK.search(text):
        return text, 'protected_diff_or_traceback'
    # Infer pytest from explicit command AND output header for additional protection.
    is_pytest = ('pytest' in command.lower() and PYTEST_SUMMARY.search(text))
    if is_pytest:
        candidate = _pytest(text)
        reason = 'pytest_pass_summary'
    else:
        candidate = _rle(text, allow_warning=('build' in command.lower() or 'compile' in command.lower()))
        reason = 'adjacent_duplicates'
    if len(candidate) >= len(text) * max_ratio:
        return text, 'insufficient_saving'
    return candidate, reason
