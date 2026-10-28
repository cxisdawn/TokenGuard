"""Offline build/pip contracts: actual local tools, explicit mixed fixtures, no model."""
from __future__ import annotations

import argparse
import base64
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid
import zipfile

BASE = Path(__file__).resolve().parents[1]
ADVISORY = 'warning: synthetic duplicate advisory\n'
MODULE = b'VALUE = 42\n'
RENDERER = '''from pip._internal.cli.progress_bars import get_download_progress_renderer
import sys
print("PIP_VERSION=" + __import__("pip").__version__, file=sys.stderr)
for index in range(100):
    render = get_download_progress_renderer(bar_type="on", size=1048576)
    list(render([b"x" * 1048576]))
'''


@dataclass(frozen=True)
class Case:
    name: str
    tool: str
    expected_exit: int = 0
    compressed: bool = False
    variants: bool = True
    mode: str = 'native'


def cases() -> tuple[Case, ...]:
    return (
        Case('compile_success', 'build'),
        Case('compile_error', 'build', 1),
        Case('compile_error_mixed', 'build', 1, True, mode='mixed'),
        Case('pip_install', 'pip'),
        Case('pip_error_native', 'pip', 1, variants=False),
        Case('pip_error_merged', 'pip', 1, mode='merged'),
        Case('pip_renderer', 'pip', compressed=True, mode='renderer'),
        Case('pip_renderer_error', 'pip', 1, True, mode='renderer_merged'),
        Case('pip_renderer_native_error', 'pip', 1, True, mode='renderer_native'),
        Case('pip_renderer_inline_error', 'pip', 1, mode='renderer_inline'),
    )


def lines(text: str) -> list[str]:
    return list(io.StringIO(text, newline=''))


def preservation_errors(raw: str, delivered: str, removable: tuple[str, ...]) -> list[str]:
    """Allow only records proven to originate solely from the progress renderer."""
    progress = set(removable)
    sequence = iter(lines(delivered))
    for line in lines(raw):
        if line not in progress and not any(candidate == line for candidate in sequence):
            return ['non-progress line lost, changed or reordered']
    return []


def rle_errors(raw: str, delivered: str, repeated: str) -> list[str]:
    """Independently expand the documented RLE marker for this fixture only."""
    marker = re.compile(r'\[TokenGuard: ([0-9]+) identical adjacent non-critical lines hidden\]\r?\n|'
                        r'\[TokenGuard: ([0-9]+) identical adjacent non-critical lines hidden\]\r')
    expanded = marker.sub(lambda match: repeated * int(match[1] or match[2]), delivered)
    # Hook retrieval metadata follows the core candidate. It is not original content.
    suffix = expanded.find('[TokenGuard artifact=')
    if suffix >= 0:
        expanded = expanded[:suffix]
    return [] if expanded == raw else ['RLE expansion differs from original records']


def environment(data: Path, enabled: bool = True) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('TOKENGUARD_') and key not in (
               'PYTHONPATH', 'PYTHONPYCACHEPREFIX', 'SOURCE_DATE_EPOCH', 'PYTEST_ADDOPTS',
               'PYTEST_PLUGINS', 'FORCE_COLOR', 'CLICOLOR_FORCE')}
    env.update(TOKENGUARD_DATA_DIR=str(data), TOKENGUARD_ENABLED='1' if enabled else '0',
               TOKENGUARD_DIAGNOSTICS='1', TOKENGUARD_SOURCE='manual', PYTHONUTF8='1',
               PYTHONIOENCODING='utf-8', PYTHONWARNINGS='default', TERM='dumb', NO_COLOR='1')
    return env


def execute(argv: list[str], workspace: Path, env: dict[str, str], payload: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=workspace, env=env, input=payload,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)


def write_wheel(path: Path) -> None:
    """Build a pure Python wheel with RECORD hashes; no wheel/setuptools dependency."""
    directory = 'tg_contract-1.0.dist-info/'
    entries = {'tg_contract.py': MODULE,
               directory + 'METADATA': b'Metadata-Version: 2.1\nName: tg-contract\nVersion: 1.0\n',
               directory + 'WHEEL': b'Wheel-Version: 1.0\nGenerator: TokenGuard-local-fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n'}
    record = io.StringIO(newline='')
    writer = csv.writer(record, lineterminator='\n')
    for name, data in entries.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode('ascii')
        writer.writerow([name, 'sha256=' + digest, len(data)])
    writer.writerow([directory + 'RECORD', '', ''])
    entries[directory + 'RECORD'] = record.getvalue().encode('utf-8')
    with zipfile.ZipFile(path, 'w') as wheel:
        for name, data in entries.items():
            wheel.writestr(name, data)


def fixture(case: Case, workspace: Path, evidence: Path) -> dict:
    """Run tools in a short temporary workspace, retain original streams separately."""
    env = environment(workspace / 'data')
    observations, removable, repeated, tool_version = [], (), '', sys.version.split()[0]

    def invoke(args: list[str], expected_exit: int) -> tuple[str, str]:
        proc = execute([sys.executable, '-X', 'utf8', *args], workspace, env)
        number = len(observations)
        (evidence / f'process-{number}.stdout').write_bytes(proc.stdout)
        (evidence / f'process-{number}.stderr').write_bytes(proc.stderr)
        observations.append({'argv': args, 'exit_code': proc.returncode,
                             'stdout_sha256': hashlib.sha256(proc.stdout).hexdigest(),
                             'stderr_sha256': hashlib.sha256(proc.stderr).hexdigest()})
        # Decode strictly even if the fixture is otherwise invalid.
        stdout, stderr = proc.stdout.decode('utf-8'), proc.stderr.decode('utf-8')
        if proc.returncode != expected_exit:
            raise ValueError('unexpected local tool exit code')
        return stdout, stderr

    if case.tool == 'build':
        source = workspace / 'src'
        source.mkdir()
        for index in range(90):
            (source / f'module_{index:03d}.py').write_bytes(MODULE)
        if case.expected_exit:
            (source / 'broken.py').write_bytes(b'def broken(:\n')
        stdout, stderr = invoke(['-m', 'compileall', '-f', str(source)], case.expected_exit)
        products = list(source.glob('__pycache__/module_*.pyc'))
        if len(products) != 90 or any(not product.stat().st_size for product in products):
            raise ValueError('missing compiled products')
        if any(path.read_bytes() != MODULE for path in source.glob('module_*.py')):
            raise ValueError('compiler changed source fixture')
        if case.expected_exit and ('SyntaxError:' not in stdout or 'def broken(:' not in stdout):
            raise ValueError('missing actual compiler diagnostics')
        if case.mode == 'mixed':
            repeated = ADVISORY
            stdout = repeated * 200 + stdout
        command = 'python -m compileall -f src'
        provenance = 'actual local compileall' + ('; prepended synthetic duplicate advisories' if repeated else '')
    else:
        tool_version, version_error = invoke(['-m', 'pip', '--version'], 0)
        if not tool_version.startswith('pip ') or version_error:
            raise ValueError('missing pip version evidence')
        tool_version = tool_version.strip()
        if case.mode.startswith('renderer'):
            progress, version = invoke(['-c', RENDERER], 0)
            if len(progress) < 3000 or len(lines(progress)) < 100 or not version.startswith('PIP_VERSION='):
                raise ValueError('pip renderer produced insufficient progress/version evidence')
            (evidence / 'renderer-version.txt').write_text(version, encoding='utf-8')
            removable = tuple(lines(progress))
        wheel = workspace / 'tg_contract-1.0-py3-none-any.whl'
        write_wheel(wheel)
        target = workspace / 'installed'
        requirement = str(wheel) if case.expected_exit == 0 else 'tokenguard-contract-never-published==0.0'
        if case.mode == 'renderer':
            stdout, stderr = progress, ''
        else:
            stdout, stderr = invoke(['-m', 'pip', '--isolated', '--disable-pip-version-check', 'install',
                '--no-index', '--no-deps', '--no-compile', '--no-cache-dir', '--no-warn-script-location',
                '--progress-bar', 'off', '--target', str(target), requirement], case.expected_exit)
            if case.expected_exit == 0:
                if (target / 'tg_contract.py').read_bytes() != MODULE or 'Successfully installed' not in stdout:
                    raise ValueError('pip install missing verified product/success result')
            elif 'ERROR:' not in stderr or 'No matching distribution found' not in stderr:
                raise ValueError('missing actual pip error diagnostics')
            if case.mode in ('merged', 'renderer_merged'):
                stdout, stderr = stdout + stderr, ''
            if case.mode in ('renderer_merged', 'renderer_native'):
                stdout = progress + stdout
            elif case.mode == 'renderer_inline':
                diagnostic = lines(stderr)[0].rstrip('\r\n')
                stdout = ''.join(line.rstrip('\r\n') + ' ' + diagnostic + '\n' for line in lines(progress))
                stdout, stderr = stdout + stderr, ''
                removable = ()  # Each frame now carries actual diagnostic content.
        command = 'python -m pip install --no-index --no-deps --target TEMP ' + requirement
        provenance = 'actual local pip --no-index install into temporary target'
        if case.mode.startswith('renderer'):
            provenance = ('actual pip download renderer, repeated completed in-memory frames; no download'
                          + ('; combined with actual offline pip errors' if case.expected_exit else ''))
    (evidence / 'observations.json').write_text(json.dumps(observations, indent=2), encoding='utf-8')
    return {'stdout': stdout, 'stderr': stderr, 'command': command, 'provenance': provenance,
            'removable': removable, 'repeated': repeated, 'observations': observations, 'tool_version': tool_version}


def artifact_bytes(path: Path) -> bytes:
    name = str(path.absolute())
    if os.name == 'nt' and not name.startswith('\\\\?\\'):
        name = '\\\\?\\UNC\\' + name[2:] if name.startswith('\\\\') else '\\\\?\\' + name
    return Path(name).read_bytes()


def check(case: Case, data: dict, enabled: bool, folder: Path, implementation: Path) -> dict:
    folder.mkdir(parents=True, exist_ok=False)
    env = environment(folder / 'data', enabled)
    session = 'manual-build-pip-' + uuid.uuid4().hex
    raw, stderr = data['stdout'], data['stderr']
    response = {'stdout': raw, 'stderr': stderr, 'exit_code': case.expected_exit,
                'interrupted': False, 'isImage': False, 'extra': {'preserve': [1, None, '原文']}}
    event = {'hook_event_name': 'PostToolUse', 'tool_name': 'Bash', 'session_id': session,
             'tool_input': {'command': data['command']}, 'tool_response': response}
    hook = execute([sys.executable, '-B', '-X', 'utf8', str(implementation / 'tokenguard/hook.py'),
                    '--source', 'manual'], folder, env, json.dumps(event).encode('utf-8'))
    errors = []
    if hook.returncode or hook.stderr:
        errors.append('hook exit/stderr indicates failure')
    compressed = bool(hook.stdout.strip())
    changed = response
    if compressed:
        output = json.loads(hook.stdout)['hookSpecificOutput']
        if output['hookEventName'] != 'PostToolUse':
            errors.append('replacement event changed')
        changed = output['updatedToolOutput']
    if {key: value for key, value in response.items() if key != 'stdout'} != {
            key: value for key, value in changed.items() if key != 'stdout'}:
        errors.append('non-stdout response fields changed')
    delivered = changed['stdout']
    (folder / 'original.txt').write_bytes(raw.encode('utf-8'))
    (folder / 'delivered.txt').write_bytes(delivered.encode('utf-8'))
    if compressed != (enabled and case.compressed):
        errors.append('compression decision differs from contract')
    if not (enabled and case.compressed) and delivered != raw:
        errors.append('required passthrough changed original output')
    if compressed and data['repeated']:
        errors.extend(rle_errors(raw, delivered, data['repeated']))
    else:
        errors.extend(preservation_errors(raw, delivered, data['removable']))

    def cli(*args: str) -> bytes:
        proc = execute([sys.executable, '-B', '-X', 'utf8', str(implementation / 'tg.py'), *args], folder, env)
        if proc.returncode or proc.stderr:
            raise ValueError('CLI exit/stderr indicates failure: ' + proc.stderr.decode('utf-8', errors='replace')[-300:])
        return proc.stdout

    stats = json.loads(cli('stats', '--session', session, '--json'))
    recoveries = returned = 0
    exact = None
    if compressed and len(delivered) >= len(raw) * .85:
        errors.append('net character threshold violated')
    if compressed and (stats['compressions'] != 1 or len(stats['records']) != 1):
        errors.append('compression audit missing or invalid')
    elif compressed:
        record = stats['records'][0]
        artifact, cid = record['artifact_id'], record['compression_id']
        if record['source'] != 'manual' or record['command_type'] != case.tool:
            errors.append('incorrect audit provenance/command type')
        if (record['original_chars'] != len(raw) or record['optimized_chars'] != len(delivered)
                or record['saved_chars'] != len(raw) - len(delivered)):
            errors.append('audit character counts differ from delivered output')
        exact = artifact_bytes(folder / 'data/artifacts' / (artifact + '.txt')) == raw.encode('utf-8')
        if not exact:
            errors.append('artifact bytes differ')
        number = 80
        original_line = lines(raw)[number - 1]
        keyword = original_line.rstrip('\r\n').strip()[:64]
        shown = cli('show', artifact, '--start', str(number), '--count', '1', '--compression-id', cid)
        # Bar-only prefixes can begin with "--". Terminate options rather than
        # accidentally treating original content as an argparse option.
        found = cli('grep', '--context', '0', '--limit', '1', '--compression-id', cid, '--', artifact, keyword)
        first, first_line = next((n, line) for n, line in enumerate(lines(raw), 1) if keyword in line)
        if shown != original_line.encode('utf-8') or not found.startswith((str(first) + ': ' + first_line).encode('utf-8')):
            errors.append('selective recovery differs from original record')
        recoveries, returned = 2, len(shown.decode('utf-8')) + len(found.decode('utf-8'))
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
    folder = output.resolve() / ('build-pip-contracts-' + uuid.uuid4().hex)
    folder.mkdir(parents=True, exist_ok=False)
    implementation = implementation.resolve()
    planned = cases()
    manifest = [asdict(case) for case in planned]
    harness = Path(__file__).resolve()
    harness_hash = hashlib.sha256(harness.read_bytes()).hexdigest()
    rows, fixtures, input_errors, hashes = [], [], [], {}
    try:
        hashes = snapshot(implementation)
    except OSError as exc:
        input_errors.append(type(exc).__name__)
    for case in planned:
        evidence = folder / 'fixtures' / case.name
        evidence.mkdir(parents=True)
        fixture_error = None
        try:
            if input_errors:
                raise ValueError('implementation source unavailable')
            with tempfile.TemporaryDirectory(prefix='tg-build-pip-') as temporary:
                data = fixture(case, Path(temporary), evidence)
            (evidence / 'stdout.txt').write_bytes(data['stdout'].encode('utf-8'))
            (evidence / 'stderr.txt').write_bytes(data['stderr'].encode('utf-8'))
            fixtures.append({'case': case.name, 'valid': True, 'provenance': data['provenance'],
                             'observations': data['observations'], 'tool_version': data['tool_version'],
                             'stdout_sha256': hashlib.sha256(data['stdout'].encode('utf-8')).hexdigest(),
                             'stderr_sha256': hashlib.sha256(data['stderr'].encode('utf-8')).hexdigest()})
        except Exception as exc:
            fixture_error = type(exc).__name__ + ': ' + str(exc)
            fixtures.append({'case': case.name, 'valid': False, 'error': fixture_error})
        endings = (('lf', '\n'), ('crlf', '\r\n'), ('cr', '\r')) if case.variants else (('native', None),)
        for name, ending in endings:
            for enabled in (True, False):
                try:
                    if fixture_error:
                        raise ValueError(fixture_error)
                    def transform(text: str) -> str:
                        return text.replace('\r\n', '\n').replace('\r', '\n').replace('\n', ending) if ending else text
                    transformed = dict(data, stdout=transform(data['stdout']),
                                       removable=tuple(transform(line) for line in data['removable']),
                                       repeated=transform(data['repeated']))
                    row = check(case, transformed, enabled, folder / 'checks' / case.name / name / str(enabled), implementation)
                except Exception as exc:
                    row = {'passed': False, 'errors': [type(exc).__name__ + ': ' + str(exc)]}
                rows.append({'case': case.name, 'newline': name, 'enabled': enabled, **row})
    try:
        unchanged = bool(hashes) and hashes == snapshot(implementation) and hashlib.sha256(harness.read_bytes()).hexdigest() == harness_hash
    except OSError:
        unchanged = False
    expected = sum((3 if case.variants else 1) * 2 for case in planned)
    report = {'schema': 'tokenguard.build-pip-contracts/v1', 'timestamp': datetime.now(timezone.utc).isoformat(),
              'python_version': sys.version.split()[0], 'expected_checks': expected,
              'successful_checks': sum(row['passed'] for row in rows),
              'passed': unchanged and not input_errors and expected > 0 and len(rows) == expected
                        and all(item['valid'] for item in fixtures) and all(row['passed'] for row in rows),
              'source_sha256': hashes, 'source_unchanged_during_run': unchanged,
              'verification_source_sha256': harness_hash, 'fixture_manifest': manifest,
              'fixture_manifest_sha256': hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
              'fixtures': fixtures, 'rows': rows, 'input_errors': input_errors, 'model_api_calls': 0,
              'coding_ability_evaluated': False, 'live_claude_integration': False,
              'actual_provider_usage': 'unavailable', 'actual_billed_cost': 'unavailable',
              'scope': 'actual compileall, offline pip target install, pip renderer; mixed fixtures explicit; newline variants transformed'}
    (folder / 'report.json').write_text(json.dumps(report, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    with (folder / 'report.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['case', 'newline', 'enabled', 'passed', 'compressed',
            'original_chars', 'delivered_chars', 'artifact_byte_identical', 'recovery_count', 'returned_chars', 'errors'])
        writer.writeheader()
        writer.writerows(rows)
    markdown = ['# Local build/pip output contracts', '',
                f"Passed: {report['passed']}; checks: {report['successful_checks']}/{expected}.",
                'Actual local tools and manual Hook/CLI subprocesses. No model or live host validation.',
                'pip uses no index/dependencies and temporary targets. Renderer frames use in-memory bytes, no download.',
                'Mixed advisory/diagnostic fixtures and transformed newlines are explicit in JSON.',
                'Each compression exercises show/grep; this does not measure task recovery efficiency or cost.', '',
                '| Case | Newline | Enabled | Passed | Errors |', '| --- | --- | --- | --- | --- |']
    for row in rows:
        markdown.append(f"| {row['case']} | {row['newline']} | {row['enabled']} | {row['passed']} | "
                        + '; '.join(row['errors']).replace('|', '/') + ' |')
    markdown += ['', *input_errors, '', 'Actual API usage and billed cost: unavailable.', '']
    (folder / 'report.md').write_text('\n'.join(markdown), encoding='utf-8')
    return folder


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=BASE / 'reports/build-pip-contracts')
    parser.add_argument('--implementation', type=Path, default=BASE, help='trusted local v0.4 source to execute')
    args = parser.parse_args()
    folder = run(args.output, args.implementation)
    report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    print(json.dumps({'report': str(folder), 'passed': report['passed'],
                      'checks': report['successful_checks'], 'expected_checks': report['expected_checks']}, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
