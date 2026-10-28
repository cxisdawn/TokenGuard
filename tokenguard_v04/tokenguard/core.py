"""Conservative v0.4 strategies, evolved from v0.3's run-length compressor."""
import io
import re

from .file_search import filename_command, search_hint, summarize as summarize_files
from .pytest_index import PassedIndex, base_node
from .unittest_progress import summarize as summarize_unittest

PROTECTED = re.compile(r'error|fail|exception|traceback|assert|fatal|denied|critical|^\s*at\s+|^\s*File\s+"', re.I)
WARNING = re.compile(r'\bwarning\b', re.I)
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
PYTEST_PASS = re.compile(r'^(?P<node>\S.*?)' r'[ \t]+PASSED(?:[ \t]+\[[ \t]*\d+%\])?[ \t]*$')
PYTEST_SUMMARY = re.compile(r'(?m)^\s*(?:=+\s*)?\d+ (?:passed|failed|errors?|skipped|warnings?|deselected|xfailed|xpassed).*\bin \d+(?:\.\d+)?s\b')
PYTEST_SECTION = re.compile(r'^[ \t]*(?:={3,}|_{3,}|-{3,})')
PYTEST_START = re.compile(r'^=+[ \t]*test session starts[ \t]*=+$', re.I)
PYTEST_CAPTURED = re.compile(r'^[ \t]*Captured[ \t]+(?:stdout|stderr|log)\b', re.I)
PYTEST_DETACHED_STATUS = re.compile(r'^[ \t]*(?:PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:[ \t]|$)')
PYTEST_RESULT = re.compile(r'^(?P<node>\S.*?)[ \t]+(?:PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:[ \t]|$)')
# These are conservative hints, not shell parsing: even a quoted option value
# may cause passthrough. The command does not expose pytest.ini/PYTEST_ADDOPTS.
PYTEST_LIVE_CAPTURE = re.compile(
    r'''(?:^|[\s"'=])(?:-[A-Za-z]*s[A-Za-z]*(?=$|[\s"'])|--capture(?:=|[ \t]+)["']?(?:no|tee-sys)(?=$|[\s"']))''')
TRACEBACK = re.compile(r'Traceback \(most recent call last\)|ExceptionGroup Traceback', re.I)
DIFF_HEADER = re.compile(r'(?m)^diff (?:--git|--cc|--combined)|^@@|^--- |^\+\+\+ |^GIT binary patch|^Binary files .* differ')
DETAIL = re.compile(r'^\s*(?:=+\s*(?:FAILURES|ERRORS|short test summary)|_{3,}|FAIL:|ERROR:)', re.I)
PIP_PROGRESS = re.compile(
    r'[ \t]*[━─█=\-]+[ \t]+'
    r'[0-9]+(?:\.[0-9]+)?(?:/[0-9]+(?:\.[0-9]+)?)?[ \t]+'
    r'(?:bytes|[kMGTPEZY]B)[ \t]+'
    r'(?:[0-9]+(?:\.[0-9]+)?[ \t]+(?:bytes|[kMGTPEZY]B)/s|\?)'
    r'(?:[ \t]+(?:eta[ \t]+)?(?:[0-9]+:[0-9]{2}:[0-9]{2}|-:--:--))?[ \t]*')


def command_type(command: str) -> str:
    if re.search(r'\bgit\s+(?:diff|show)\b', command):
        return 'git_diff'
    if filename_command(command) or search_hint(command):
        return 'search'
    for kind, pattern in [('pytest', r'\bpytest\b'), ('unittest', r'\bunittest\b'),
                          ('pip', r'\bpip(?:3)?\s+install\b'),
                          ('build', r'\b(?:build|compile|compileall|py_compile|make|cmake|ninja|msbuild|webpack)\b')]:
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
    # Match artifact recovery's CR/LF records. Unicode separators and C0
    # controls can occur inside one diagnostic/JSON record: splitting them
    # would hide inline fragments and bypass protection on the whole record.
    for line in io.StringIO(text, newline=''):
        # Styling may split a severity word (ER<SGR>ROR). Recognize the rendered
        # text while retaining the entire original record, including its bytes.
        visible = ANSI.sub('', line)
        protected = PROTECTED.search(visible) or (WARNING.search(visible) and not allow_warning)
        if line == previous and not protected:
            count += 1
            continue
        flush()
        result.append(line)
        previous = line
    flush()
    return ''.join(result)


def _pytest_report_boundary(line: str, seen_progress: bool) -> bool:
    session_start = PYTEST_START.fullmatch(line) and not seen_progress
    return bool(DETAIL.search(line) or PYTEST_SUMMARY.match(line) or PYTEST_CAPTURED.match(line)
                or (PYTEST_SECTION.match(line) and not session_start))


def _pytest_ambiguous_progress(clean: str) -> bool:
    """Reject interleaved live output even when capture flags are hidden.

    Test stdout can replace an interrupted progress node with a fake PASSED
    line, leaving the apparent pass count correct. A detached status or pending
    node is evidence that the progress stream cannot be interpreted safely.
    Report sections are already preserved verbatim by the aggregator.
    """
    seen_progress = False
    for raw_line in io.StringIO(clean, newline=''):
        line = raw_line.rstrip('\r\n')
        if _pytest_report_boundary(line, seen_progress):
            break
        if PYTEST_DETACHED_STATUS.match(line):
            return True
        result = PYTEST_RESULT.match(line)
        if result and base_node(result.group('node')) is not None:
            seen_progress = True
        elif base_node(line.strip()) is not None:
            return True
    return False


def _aggregate(text: str, matcher: re.Pattern, label: str,
               inventory: PassedIndex | None = None, *, pytest_progress: bool = False) -> str:
    result: list[str] = []
    count = 0
    details = False
    seen_progress = False
    def flush() -> None:
        nonlocal count
        if count:
            result.append(f'[TokenGuard: {count} {label} lines hidden; original retrievable]\n')
            count = 0
    # Use the same physical line boundaries as artifact recovery in every mode.
    # splitlines() also splits controls such as form-feed, which could turn an
    # unknown diagnostic fragment into an apparently valid progress record.
    lines = io.StringIO(text, newline='')
    for number, line in enumerate(lines, 1):
        clean = ANSI.sub('', line).rstrip('\r\n')
        details = details or bool(DETAIL.search(clean))
        if pytest_progress:
            # Warnings, captured output, durations, plugin sections and footer
            # text are not execution progress. Once a section/footer starts,
            # preserve the entire remaining output, even if it resembles nodes.
            details = details or _pytest_report_boundary(clean, seen_progress)
            status = PYTEST_RESULT.match(clean)
            if status and base_node(status.group('node')) is not None:
                seen_progress = True
        match = matcher.fullmatch(clean)
        if pytest_progress and match and base_node(match.group('node')) is None:
            match = None
        if not details and match:
            seen_progress = True
            count += 1
            if inventory is not None:
                inventory.add(match.group('node'), number)
        else:
            flush()
            result.append(line)
    flush()
    return ''.join(result)


def compress(text: str, min_chars: int = 3000, max_ratio: float = 0.85,
             command: str = '', *, pytest_mode: str = 'compact') -> tuple[str, str]:
    if pytest_mode not in ('compact', 'indexed', 'off'):
        raise ValueError('Unknown pytest mode')
    kind = command_type(command)
    if kind == 'pytest' and pytest_mode == 'off':
        return text, 'pytest_disabled'
    if len(text) < min_chars:
        return text, 'below_threshold'
    clean = ANSI.sub('', text)
    # Normalize only for recognition; keep original bytes in candidates and
    # artifacts. Protection anchors must work for CR-only output in every mode.
    clean = clean.replace('\r\n', '\n').replace('\r', '\n')
    if kind == 'git_diff' or DIFF_HEADER.search(clean) or TRACEBACK.search(clean):
        return text, 'protected_diff_or_traceback'
    if kind == 'pytest':
        if not PYTEST_SUMMARY.search(clean):
            return text, 'incomplete_pytest'
        if PYTEST_LIVE_CAPTURE.search(command) or _pytest_ambiguous_progress(clean):
            return text, 'ambiguous_pytest_live_output'
        inventory = PassedIndex() if pytest_mode == 'indexed' else None
        candidate = _aggregate(text, PYTEST_PASS, 'pytest PASSED', inventory, pytest_progress=True)
        strategy = 'pytest_pass_summary'
        if inventory is not None:
            index = inventory.render()
            if index is None:
                return text, 'pytest_index_budget_exceeded'
            candidate = index + candidate
            strategy = 'pytest_pass_summary_indexed'
    elif kind == 'unittest':
        if not re.search(r'(?m)^Ran \d+ tests? in [\d.]+s\s*$', clean) or not re.search(r'(?m)^(?:OK(?: \(.*\))?|FAILED \(.*\))\s*$', clean):
            return text, 'incomplete_unittest'
        candidate = summarize_unittest(text)
        if candidate is None:
            return text, 'ambiguous_unittest_output'
        strategy = 'unittest_pass_summary'
    elif kind == 'pip':
        # Require a whole known progress record. A bar/speed prefix alone may
        # precede interleaved errors or unknown diagnostics on the same line.
        candidate = _aggregate(text, PIP_PROGRESS, 'pip progress')
        strategy = 'pip_progress'
    elif kind == 'search':
        candidate = summarize_files(text, command, PROTECTED)
        if candidate is None:
            return text, 'ambiguous_search'
        strategy = 'file_search'
    else:
        candidate = _rle(text, allow_warning=True)
        strategy = 'adjacent_duplicates' if kind == 'build' else ('repeated_warnings' if WARNING.search(clean) else 'adjacent_duplicates')
    if len(candidate) >= len(text) * max_ratio:
        return text, 'insufficient_saving'
    return candidate, strategy
