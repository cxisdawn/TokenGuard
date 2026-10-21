"""Conservative v0.4 strategies, evolved from v0.3's run-length compressor."""
import re

PROTECTED = re.compile(r'error|fail|exception|traceback|assert|fatal|denied|critical|^\s*at\s+|^\s*File\s+"', re.I)
WARNING = re.compile(r'\bwarning\b', re.I)
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
PYTEST_PASS = re.compile(r'^\s*.+?::.+?\s+PASSED(?:\s+\[\s*\d+%\])?\s*$')
PYTEST_SUMMARY = re.compile(r'(?m)^\s*(?:=+\s*)?\d+ (?:passed|failed|errors?|skipped|warnings?|deselected|xfailed|xpassed).*\bin \d+(?:\.\d+)?s\b')
TRACEBACK = re.compile(r'Traceback \(most recent call last\)|ExceptionGroup Traceback', re.I)
DIFF_HEADER = re.compile(r'(?m)^diff (?:--git|--cc|--combined)|^@@|^--- |^\+\+\+ |^GIT binary patch|^Binary files .* differ')
UNIT_PASS = re.compile(r'^\S+ \([^\n]+\)(?:\s+\.\.\.)? \.\.\. ok\s*$')
DETAIL = re.compile(r'^\s*(?:=+\s*(?:FAILURES|ERRORS|short test summary)|_{3,}|FAIL:|ERROR:)', re.I)


def command_type(command: str) -> str:
    if re.search(r'\bgit\s+(?:diff|show)\b', command):
        return 'git_diff'
    for kind, pattern in [('pytest', r'\bpytest\b'), ('unittest', r'\bunittest\b'),
                          ('pip', r'\bpip(?:3)?\s+install\b'),
                          ('search', r'\brg\s+--files\b|\bfind\s+.+\s-type\s+f\b'),
                          ('build', r'\b(?:build|compile|make|cmake|ninja|msbuild|webpack)\b')]:
        if re.search(pattern, command, re.I):
            return kind
    return 'generic'


def _rle(text: str, allow_warning: bool = False) -> str:
    result: list[str] = []
    previous = None
    count = 0
    def flush() -> None:
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


def _aggregate(text: str, matcher: re.Pattern, label: str) -> str:
    result: list[str] = []
    count = 0
    details = False
    def flush() -> None:
        nonlocal count
        if count:
            result.append(f'[TokenGuard: {count} {label} lines hidden; original retrievable]\n')
            count = 0
    for line in text.splitlines(keepends=True):
        clean = ANSI.sub('', line).rstrip('\r\n')
        details = details or bool(DETAIL.search(clean))
        if not details and matcher.fullmatch(clean):
            count += 1
        else:
            flush()
            result.append(line)
    flush()
    return ''.join(result)


def compress(text: str, min_chars: int = 3000, max_ratio: float = 0.85,
             command: str = '') -> tuple[str, str]:
    if len(text) < min_chars:
        return text, 'below_threshold'
    clean = ANSI.sub('', text)
    kind = command_type(command)
    if kind == 'git_diff' or DIFF_HEADER.search(clean) or TRACEBACK.search(clean):
        return text, 'protected_diff_or_traceback'
    if kind == 'pytest':
        if not PYTEST_SUMMARY.search(clean):
            return text, 'incomplete_pytest'
        candidate = _aggregate(text, PYTEST_PASS, 'pytest PASSED')
        strategy = 'pytest_pass_summary'
    elif kind == 'unittest':
        if not re.search(r'(?m)^Ran \d+ tests? in [\d.]+s\s*$', clean) or not re.search(r'(?m)^(?:OK(?: \(.*\))?|FAILED \(.*\))\s*$', clean):
            return text, 'incomplete_unittest'
        candidate = _aggregate(text, UNIT_PASS, 'unittest ok')
        strategy = 'unittest_pass_summary'
    elif kind == 'pip':
        # Only discard progress bars, never dependency versions, resolver or error text.
        progress = re.compile(r'^\s*[━─█=]+.*(?:kB|MB|GB|bytes)/s.*$')
        candidate = _aggregate(text, progress, 'pip progress')
        strategy = 'pip_progress'
    elif kind == 'search':
        lines = text.splitlines(keepends=True)
        # Filename-only results, with no diagnostic/unknown rows. Never truncate grep content.
        if any(not re.fullmatch(r'[\w./\\:@+ -]+\.[\w-]+', x.rstrip('\r\n')) or PROTECTED.search(x) for x in lines):
            return text, 'ambiguous_search'
        candidate = ''.join(lines[:20]) + f'[TokenGuard: {len(lines)-20} file paths hidden; use artifact grep/show]\n'
        strategy = 'file_search'
    else:
        candidate = _rle(text, allow_warning=True)
        strategy = 'adjacent_duplicates' if kind == 'build' else ('repeated_warnings' if WARNING.search(text) else 'adjacent_duplicates')
    if len(candidate) >= len(text) * max_ratio:
        return text, 'insufficient_saving'
    return candidate, strategy
