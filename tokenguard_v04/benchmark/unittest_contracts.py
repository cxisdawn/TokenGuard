"""Real local unittest output contracts; no model, live host or cost claims."""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

BASE = Path(__file__).resolve().parents[1]
PREFIX = '''import atexit
import sys
import unittest
import warnings

class ContractCase(unittest.TestCase):
    pass

for index in range(120):
    setattr(ContractCase, f"test_{index:03d}", lambda self: None)

'''


@dataclass(frozen=True)
class Case:
    name: str
    source: str = PREFIX
    expected_exit: int = 0
    compressed: bool = True
    merged: bool = True
    markers: tuple[str, ...] = ()
    args: tuple[str, ...] = ()
    expected_tests: int = 120


def cases() -> tuple[Case, ...]:
    return (
        Case('success'),
        Case('statuses', PREFIX + '''
@unittest.skip("SKIP::reason")
def skipped(self): pass
@unittest.expectedFailure
def expected(self): self.fail("EXPECTED::detail")
@unittest.expectedFailure
def unexpected(self): pass
ContractCase.test_skip = skipped
ContractCase.test_expected = expected
ContractCase.test_unexpected = unexpected
''', 1, markers=('skipped', 'expected failure', 'unexpected success'), expected_tests=123),
        Case('failure', PREFIX + '''
def broken(self): self.assertEqual(1, 2, "ASSERT::detail")
ContractCase.test_failure = broken
''', 1, False, markers=('ASSERT::detail',), expected_tests=121),
        Case('setup_error', PREFIX + '''
class SetupCase(unittest.TestCase):
    def setUp(self): raise ValueError("SETUP::detail")
    def test_setup(self): pass
''', 1, False, markers=('SETUP::detail',), expected_tests=121),
        Case('subtest', PREFIX + '''
def subtest(self):
    for index in range(2):
        with self.subTest(index=index):
            self.assertEqual(index, 0, "SUBTEST::detail")
ContractCase.test_subtest = subtest
''', 1, False, markers=('SUBTEST::detail', 'index=1'), expected_tests=121),
        Case('buffered_failure', PREFIX + '''
def buffered(self):
    print("CAPTURED::stdout")
    print("CAPTURED::stderr", file=sys.stderr)
    self.fail("BUFFERED::detail")
ContractCase.test_buffered = buffered
''', 1, False, markers=('CAPTURED::stdout', 'CAPTURED::stderr', 'BUFFERED::detail'), args=('-b',), expected_tests=121),
        Case('docstring', PREFIX + '''
def documented(self):
    """IMPORTANT_DOC (configuration missing)"""
ContractCase.test_documented = documented
''', compressed=False, markers=('IMPORTANT_DOC',), expected_tests=121),
        Case('live_stderr', PREFIX + '''
def live(self):
    sys.stderr.write("\\ntest_payload (custom.report.test_payload) ... ok\\n")
    sys.stderr.flush()
ContractCase.test_live = live
''', compressed=False, markers=('test_payload',), expected_tests=121),
        Case('live_stdout', PREFIX + '''
def live(self):
    print("\\ntest_payload (custom.report.test_payload) ... ok", flush=True)
ContractCase.test_live = live
''', compressed=False, markers=('test_payload',), expected_tests=121),
        Case('report_tail', PREFIX + '''
atexit.register(lambda: print("test_footer (custom.report.test_footer) ... ok", file=sys.stderr))
''', markers=('test_footer',)),
        Case('warning', PREFIX + '''
def warning(self): warnings.warn("WARNING::detail", UserWarning)
ContractCase.test_warning = warning
''', compressed=False, markers=('WARNING::detail',), expected_tests=121),
        Case('unknown_diagnostic', PREFIX + '''
sys.stderr.write("WARNING: (configuration unavailable) ... ok\\n")
sys.stderr.write("CRITICAL (custom.report) ... ok\\n")
''', markers=('WARNING:', 'CRITICAL')),
        # Default unittest writes its report to stderr. The hook must not move
        # it into stdout or count stderr as compressed.
        Case('native_stderr', compressed=False, merged=False),
    )


def physical_lines(text: str) -> list[str]:
    return list(io.StringIO(text, newline=''))


def contract_errors(raw: str, delivered: str, markers: tuple[str, ...]) -> list[str]:
    """Only generated success records may disappear; preserve all other lines in order."""
    required = []
    for line in physical_lines(raw):
        # Independent fixture provenance: exactly the 120 generated methods.
        # Do not import the target implementation's progress recognizer.
        match = re.fullmatch(r'test_(\d{3}) \(test_contract\.ContractCase(?:\.test_(\d{3}))?\) \.\.\. ok',
                             line.rstrip('\r\n'))
        if match and int(match[1]) < 120 and (match[2] is None or match[1] == match[2]):
            continue
        required.append(line)
    errors = []
    sequence = iter(physical_lines(delivered))
    for line in required:
        if not any(candidate == line for candidate in sequence):
            errors.append('non-progress line lost, changed or reordered')
            break
    for marker in markers:
        if marker not in raw:
            errors.append('fixture missing marker: ' + marker)
    normalized = raw.replace('\r', '\n')
    if not re.search(r'(?m)^Ran \d+ tests? in [\d.]+s[ \t]*$', normalized):
        errors.append('fixture missing test count summary')
    if not re.search(r'(?m)^(?:OK(?: \(.*\))?|FAILED \(.*\))[ \t]*$', normalized):
        errors.append('fixture missing terminal result')
    return errors


def environment(data: Path, enabled: bool = True) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('TOKENGUARD_') and key not in ('PYTHONPATH', 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS')}
    env.update(TOKENGUARD_DATA_DIR=str(data), TOKENGUARD_ENABLED='1' if enabled else '0',
               TOKENGUARD_DIAGNOSTICS='1', TOKENGUARD_SOURCE='manual',
               PYTHONUTF8='1', PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1', PYTHONWARNINGS='default')
    return env


def execute(argv: list[str], folder: Path, env: dict[str, str], payload: bytes | None = None,
            *, merged: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=folder, env=env, input=payload, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT if merged else subprocess.PIPE, timeout=60)


def artifact_bytes(path: Path) -> bytes:
    # The evaluator must also be able to inspect generated artifacts when the
    # report root pushes the filename past Win32's ordinary path limit.
    name = str(path.absolute())
    if os.name == 'nt' and not name.startswith('\\\\?\\'):
        name = '\\\\?\\UNC\\' + name[2:] if name.startswith('\\\\') else '\\\\?\\' + name
    return Path(name).read_bytes()


def check(case: Case, raw: str, stderr: str, enabled: bool, folder: Path, implementation: Path) -> dict:
    folder.mkdir(parents=True, exist_ok=False)
    env = environment(folder / 'data', enabled)
    session = 'manual-unittest-' + uuid.uuid4().hex
    response = {'stdout': raw, 'stderr': stderr, 'exit_code': case.expected_exit,
                'interrupted': False, 'isImage': False, 'extra': {'preserve': [1, None, '原文']}}
    event = {'hook_event_name': 'PostToolUse', 'tool_name': 'Bash', 'session_id': session,
             'tool_input': {'command': 'python -m unittest -v test_contract' + (' 2>&1' if case.merged else '')},
             'tool_response': response}
    hook = execute([sys.executable, '-B', '-X', 'utf8', str(implementation / 'tokenguard/hook.py'),
                    '--source', 'manual'], folder, env, json.dumps(event).encode('utf-8'))
    errors = []
    if hook.returncode or hook.stderr:
        errors.append('hook exit/stderr indicates failure')
    changed, compressed = response, bool(hook.stdout.strip())
    if compressed:
        output = json.loads(hook.stdout)['hookSpecificOutput']
        if output.get('hookEventName') != 'PostToolUse':
            errors.append('replacement event changed')
        changed = output['updatedToolOutput']
    if {key: value for key, value in changed.items() if key != 'stdout'} != {
            key: value for key, value in response.items() if key != 'stdout'}:
        errors.append('non-stdout response fields changed')
    delivered = changed['stdout']
    (folder / 'original.txt').write_bytes(raw.encode('utf-8'))
    (folder / 'delivered.txt').write_bytes(delivered.encode('utf-8'))
    expected = enabled and case.compressed
    if compressed != expected:
        errors.append('compression decision differs from contract')
    if not expected and delivered != raw:
        errors.append('required passthrough changed original output')
    if case.merged:
        errors.extend(contract_errors(raw, delivered, case.markers))
    elif not stderr or raw:
        errors.append('fixture did not exercise native stderr')

    def cli(*arguments: str) -> bytes:
        proc = execute([sys.executable, '-B', '-X', 'utf8', str(implementation / 'tg.py'), *arguments], folder, env)
        if proc.returncode or proc.stderr:
            raise ValueError('recovery/statistics CLI failed')
        return proc.stdout

    stats = json.loads(cli('stats', '--session', session, '--json'))
    returned = recoveries = 0
    exact = None
    if compressed:
        if stats['compressions'] != 1 or len(delivered) >= len(raw) * .85:
            errors.append('compression audit or net character threshold violated')
        record = stats['records'][0]
        artifact, cid = record['artifact_id'], record['compression_id']
        exact = artifact_bytes(folder / 'data/artifacts' / (artifact + '.txt')) == raw.encode('utf-8')
        if not exact:
            errors.append('artifact bytes differ')
        if record['source'] != 'manual':
            errors.append('manual invocation mislabeled')
        number, line = next((n, text) for n, text in enumerate(physical_lines(raw), 1) if text.startswith('test_080 ('))
        shown = cli('show', artifact, '--start', str(number), '--count', '1', '--compression-id', cid)
        found = cli('grep', artifact, 'test_080 (', '--context', '0', '--limit', '1', '--compression-id', cid)
        if shown != line.encode('utf-8') or not found.startswith((str(number) + ': ' + line).encode('utf-8')):
            errors.append('selective recovery differs from original record')
        returned, recoveries = len(shown.decode('utf-8')) + len(found.decode('utf-8')), 2
    stats = json.loads(cli('stats', '--session', session, '--json'))
    if (stats['compressions'] != int(compressed) or stats['recovery_count'] != recoveries or
            stats['returned_chars'] != returned):
        errors.append('audit counters differ from observed calls')
    return {'passed': not errors, 'compressed': compressed, 'original_chars': len(raw),
            'delivered_chars': len(delivered), 'artifact_byte_identical': exact,
            'recovery_count': recoveries, 'returned_chars': returned, 'errors': errors}


def snapshot(implementation: Path) -> dict[str, str]:
    paths = sorted((implementation / 'tokenguard').glob('*.py')) + [implementation / 'tg.py']
    if not (implementation / 'tokenguard/hook.py').is_file():
        raise FileNotFoundError('implementation hook missing')
    return {path.relative_to(implementation).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def run(output: Path, implementation: Path = BASE) -> Path:
    folder = output.resolve() / ('unittest-contracts-' + uuid.uuid4().hex)
    folder.mkdir(parents=True, exist_ok=False)
    implementation = implementation.resolve()
    planned = cases()
    manifest = [{'case': case.name, 'source_sha256': hashlib.sha256(case.source.encode('utf-8')).hexdigest(),
                 'expected_exit': case.expected_exit, 'expect_compression': case.compressed,
                 'merged': case.merged, 'markers': case.markers, 'args': case.args,
                 'expected_tests': case.expected_tests} for case in planned]
    harness = Path(__file__).resolve()
    harness_hash = hashlib.sha256(harness.read_bytes()).hexdigest()
    rows, fixtures, input_errors, hashes = [], [], [], {}
    try:
        hashes = snapshot(implementation)
    except OSError as exc:
        input_errors.append(type(exc).__name__)
    for case in planned:
        workspace = folder / 'fixtures' / case.name
        workspace.mkdir(parents=True)
        (workspace / 'test_contract.py').write_text(case.source, encoding='utf-8')
        fixture_error = None
        try:
            if input_errors:
                raise ValueError('implementation source unavailable')
            proc = execute([sys.executable, '-u', '-B', '-X', 'utf8', '-m', 'unittest', '-v',
                            *case.args, 'test_contract'], workspace, environment(workspace / 'data'), merged=case.merged)
            stdout, stderr = proc.stdout, proc.stderr or b''
            (workspace / 'stdout.txt').write_bytes(stdout)
            (workspace / 'stderr.txt').write_bytes(stderr)
            raw, err = stdout.decode('utf-8'), stderr.decode('utf-8')
            if proc.returncode != case.expected_exit:
                raise ValueError('unexpected unittest fixture exit code')
            report_text = (raw + err).replace('\r', '\n')
            counts = re.findall(r'(?m)^Ran (\d+) tests? in [\d.]+s[ \t]*$', report_text)
            if counts != [str(case.expected_tests)]:
                raise ValueError('unexpected unittest fixture test count')
            fixture_errors = contract_errors(raw + err, raw + err, case.markers)
            if fixture_errors:
                raise ValueError('; '.join(fixture_errors))
            fixtures.append({'case': case.name, 'valid': True, 'exit_code': proc.returncode,
                             'tests_run': case.expected_tests,
                             'stdout_sha256': hashlib.sha256(stdout).hexdigest(),
                             'stderr_sha256': hashlib.sha256(stderr).hexdigest()})
        except Exception as exc:
            fixture_error = type(exc).__name__ + ': ' + str(exc)
            fixtures.append({'case': case.name, 'valid': False, 'error': fixture_error})
        endings = (('lf', '\n'), ('crlf', '\r\n'), ('cr', '\r')) if case.merged else (('native', None),)
        for name, ending in endings:
            for enabled in (True, False):
                try:
                    if fixture_error:
                        raise ValueError(fixture_error)
                    transformed = raw.replace('\r\n', '\n').replace('\r', '\n').replace('\n', ending) if ending else raw
                    row = check(case, transformed, err, enabled,
                                folder / 'checks' / case.name / name / str(enabled), implementation)
                except Exception as exc:
                    row = {'passed': False, 'errors': [type(exc).__name__ + ': ' + str(exc)]}
                rows.append({'case': case.name, 'newline': name, 'enabled': enabled, **row})
    try:
        unchanged = bool(hashes) and hashes == snapshot(implementation) and hashlib.sha256(harness.read_bytes()).hexdigest() == harness_hash
    except OSError:
        unchanged = False
    expected = sum((3 if case.merged else 1) * 2 for case in planned)
    report = {'schema': 'tokenguard.unittest-contracts/v1', 'timestamp': datetime.now(timezone.utc).isoformat(),
              'python_version': sys.version.split()[0], 'expected_checks': expected,
              'successful_checks': sum(row['passed'] for row in rows),
              'passed': unchanged and not input_errors and len(rows) == expected and all(row['passed'] for row in rows),
              'source_sha256': hashes, 'source_unchanged_during_run': unchanged,
              'verification_source_sha256': harness_hash, 'fixture_manifest': manifest,
              'fixture_manifest_sha256': hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
              'fixtures': fixtures, 'rows': rows, 'input_errors': input_errors, 'model_api_calls': 0,
              'coding_ability_evaluated': False, 'live_claude_integration': False,
              'actual_provider_usage': 'unavailable', 'actual_billed_cost': 'unavailable',
              'fixture_source': 'actual local unittest; merged stderr is explicit; newline variants transformed'}
    (folder / 'report.json').write_text(json.dumps(report, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    with (folder / 'report.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['case', 'newline', 'enabled', 'passed', 'compressed',
            'original_chars', 'delivered_chars', 'artifact_byte_identical', 'recovery_count', 'returned_chars', 'errors'])
        writer.writeheader()
        writer.writerows(rows)
    lines = ['# Actual unittest output contracts', '',
             f"Passed: {report['passed']}; checks: {report['successful_checks']}/{expected}.",
             'Actual local unittest and manual Hook/CLI subprocesses. No model calls or live host validation.',
             'Native stderr and explicitly merged streams are separate; newline variants are transformed.',
             'All compressed cases deliberately exercise show and grep; not recovery efficiency or task savings.', '',
             '| Case | Newline | Enabled | Passed | Errors |', '| --- | --- | --- | --- | --- |']
    for row in rows:
        lines.append(f"| {row['case']} | {row['newline']} | {row['enabled']} | {row['passed']} | "
                     + '; '.join(row['errors']).replace('|', '/') + ' |')
    lines += ['', *input_errors, '', 'Actual API usage and billed cost: unavailable.', '']
    (folder / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
    return folder


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=BASE / 'reports/unittest-contracts')
    parser.add_argument('--implementation', type=Path, default=BASE, help='trusted local v0.4 source to execute')
    args = parser.parse_args()
    folder = run(args.output, args.implementation)
    report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    print(json.dumps({'report': str(folder), 'passed': report['passed'],
                      'checks': report['successful_checks'], 'expected_checks': report['expected_checks']}, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
