"""Conservative unittest verbose progress; uncertain live streams pass through."""
from __future__ import annotations

import io
import re

ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
RECORD = re.compile(r'(?P<method>[^\s()]+) \((?P<identity>[^\s()]+)\)(?: \.\.\.(?P<status>.*))?')
BOUNDARY = re.compile(r'(?:[-=_]{3,}|Ran \d+ tests? in [\d.]+s|OK(?: \(.*\))?|FAILED \(.*\)|FAIL:.*|ERROR:.*)')
STATUS = re.compile(r'(?:ok|FAIL|ERROR|expected failure|unexpected success|skipped .+)')


def _known_identity(method: str, identity: str) -> bool:
    # Python 3.10 omits the method from the parenthesized identity, whereas
    # later versions normally include it. Preserve custom display names.
    return ((method.startswith('test') or method == 'runTest') and method.isidentifier()
            and '.' in identity and all(part.isidentifier() for part in identity.split('.')))


def summarize(text: str) -> str | None:
    """Aggregate only complete known successes before the first report boundary.

    An unfinished canonical header or detached status is evidence of docstrings,
    warnings or interleaved test output. Return None for the whole stream rather
    than guessing which apparently successful records came from the runner.
    """
    output: list[str] = []
    pending = 0
    report = False

    def flush() -> None:
        nonlocal pending
        if pending:
            output.append(f'[TokenGuard: {pending} unittest ok lines hidden; original retrievable]\n')
            pending = 0

    for line in io.StringIO(text, newline=''):
        clean = ANSI.sub('', line).rstrip('\r\n')
        if BOUNDARY.fullmatch(clean):
            report = True
        if report:
            flush()
            output.append(line)
            continue
        record = RECORD.fullmatch(clean)
        if STATUS.fullmatch(clean.strip()):
            return None
        if record and _known_identity(record['method'], record['identity']):
            status = record['status']
            if status is None or not STATUS.fullmatch(status.strip()):
                return None
            if status == ' ok':
                pending += 1
                continue
        flush()
        output.append(line)
    flush()
    return ''.join(output)
