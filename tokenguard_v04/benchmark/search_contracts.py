"""Real local ripgrep output, conservative summaries and recovery; no model calls."""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import uuid

# Reuse process/audit/artifact checks. Search's fixture oracle is independent of
# production recognizers; both verifier files are bound into the report hashes.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_pip_contracts as shared

BASE = Path(__file__).resolve().parents[1]
NOTICE = 'NOTICE: configure plugin.py'


@dataclass(frozen=True)
class Case:
    name: str
    compressed: bool = False
    expected_exit: int = 0
    variants: bool = True
    tool: str = 'search'


def cases() -> tuple[Case, ...]:
    return (Case('paths', True), Case('unicode_spaces', True), Case('extensionless'),
            Case('brackets'), Case('null'), Case('native_stderr', expected_exit=2, variants=False),
            Case('partial_stderr', True, 2), Case('empty', expected_exit=1), Case('compound'),
            Case('mixed_notice'), Case('mixed_location'))


def fixture(case: Case, rg: str, evidence: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix='tg-search-') as temporary:
        workspace = Path(temporary)
        names = [] if case.name in ('empty', 'native_stderr') else [f'src/module_{i:04d}.py' for i in range(300)]
        if case.name == 'unicode_spaces':
            names.append('项目/my module.py')
        elif case.name == 'extensionless':
            names.append('LICENSE')
        elif case.name == 'brackets':
            names.append('src/[generated].py')
        for name in names:
            target = workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'needle\n')
        arguments = ['--files', '--no-config', '--sort', 'path']
        if case.name == 'null':
            arguments.append('--null')
        elif case.name == 'native_stderr':
            arguments.append('missing.py')
        elif case.name == 'partial_stderr':
            arguments.extend(('src', 'missing.py'))
        command = 'rg ' + ' '.join(arguments)
        env = shared.environment(workspace / 'data')
        if case.name == 'compound':
            # Execute this fixed local shell command; no user-provided shell text.
            env['PATH'] = str(Path(rg).parent) + os.pathsep + env.get('PATH', '')
            if os.name == 'nt':
                command += "; Write-Output '" + NOTICE + "'"
                actual = '[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); ' + command
                argv = ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', actual]
            else:
                command += "; printf '%s\\n' '" + NOTICE + "'"
                argv = ['/bin/sh', '-c', command]
        else:
            argv = [rg, *arguments]
        proc = shared.execute(argv, workspace, env)
        (evidence / 'process.stdout').write_bytes(proc.stdout)
        (evidence / 'process.stderr').write_bytes(proc.stderr)
        raw, stderr = proc.stdout.decode('utf-8'), proc.stderr.decode('utf-8')
        if proc.returncode != case.expected_exit:
            raise ValueError('unexpected ripgrep exit code')
        if case.expected_exit == 2 and ('missing.py' not in stderr or not stderr.strip()):
            raise ValueError('missing actual ripgrep stderr diagnostic')
        if case.expected_exit != 2 and stderr:
            raise ValueError('unexpected fixture stderr')
        records = shared.lines(raw)
        normalize = lambda value: value.rstrip('\r\n').replace('\\', '/')
        if case.name == 'null':
            observed = raw.split('\x00')
            # Windows ripgrep emits backslashes even with NUL framing.
            if (observed[-1] != '' or len(observed) != len(names) + 1
                    or {x.replace('\\', '/') for x in observed[:-1]} != set(names)):
                raise ValueError('NUL fixture does not enumerate the exact files')
            removable = ()
        else:
            removable = tuple(line for line in records if normalize(line) in names)
            unknown = [line.rstrip('\r\n') for line in records if normalize(line) not in names]
            if len(removable) != len(names) or {normalize(line) for line in removable} != set(names):
                raise ValueError('ripgrep did not enumerate the exact fixture files')
            if unknown != ([NOTICE] if case.name == 'compound' else []):
                raise ValueError('unexpected non-filename fixture record')
        provenance = 'actual local rg --files'
        if case.name == 'compound':
            provenance += '; actual fixed shell command appends a diagnostic'
        elif case.name in ('mixed_notice', 'mixed_location'):
            raw += ('WARNING: missing configuration.py' if case.name == 'mixed_notice'
                    else 'src/file.py:10: use configuration.py') + '\n'
            provenance += '; explicitly appended synthetic diagnostic'
        manifest = {name: hashlib.sha256((workspace / name).read_bytes()).hexdigest() for name in names}
        observations = {'argv': argv, 'exit_code': proc.returncode,
                        'stdout_sha256': hashlib.sha256(proc.stdout).hexdigest(),
                        'stderr_sha256': hashlib.sha256(proc.stderr).hexdigest(), 'files': manifest}
        (evidence / 'observations.json').write_text(json.dumps(observations, indent=2), encoding='utf-8')
        return {'stdout': raw, 'stderr': stderr, 'command': command, 'removable': removable,
                'repeated': '', 'provenance': provenance, 'observations': observations}


def check(case: Case, data: dict, enabled: bool, folder: Path, implementation: Path) -> dict:
    common_case = shared.Case(case.name, case.tool, expected_exit=case.expected_exit,
                              compressed=case.compressed)
    row = shared.check(common_case, data, enabled, folder, implementation)
    if row['compressed']:
        delivered = (folder / 'delivered.txt').read_bytes().decode('utf-8')
        records = shared.lines(data['stdout'])
        prefix = ''.join(records[:20])
        marker = f'[TokenGuard: {len(records)-20} file paths hidden; use artifact grep/show]\n'
        if not delivered.startswith(prefix + marker):
            row['errors'].append('first twenty records or exact hidden count changed')
            row['passed'] = False
    return row


def verifier_hashes() -> dict[str, str]:
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (Path(__file__), Path(shared.__file__))}


def run(output: Path, implementation: Path = BASE, rg: str | None = None) -> Path:
    folder = output.resolve() / ('search-contracts-' + uuid.uuid4().hex)
    folder.mkdir(parents=True, exist_ok=False)
    implementation = implementation.resolve()
    planned = cases()
    manifest = [asdict(case) for case in planned]
    verifier_before = verifier_hashes()
    rows, fixtures, input_errors, hashes, tool = [], [], [], {}, {}
    try:
        hashes = shared.snapshot(implementation)
        executable = shutil.which(rg or 'rg')
        if not executable:
            raise FileNotFoundError('ripgrep unavailable; specify --rg PATH')
        if Path(executable).name.casefold() not in ('rg', 'rg.exe'):
            raise ValueError('ripgrep must be named rg or rg.exe for the fixed shell fixture')
        proc = shared.execute([executable, '--version'], folder, shared.environment(folder / 'data'))
        if proc.returncode or proc.stderr or not proc.stdout.startswith(b'ripgrep '):
            raise ValueError('invalid ripgrep version evidence')
        tool = {'path': executable, 'version': proc.stdout.decode('utf-8'),
                'sha256': hashlib.sha256(Path(executable).read_bytes()).hexdigest()}
    except Exception as exc:
        input_errors.append(type(exc).__name__ + ': ' + str(exc))
    for case in planned:
        evidence = folder / 'fixtures' / case.name
        evidence.mkdir(parents=True)
        fixture_error = None
        try:
            if input_errors:
                raise ValueError('preflight failed')
            data = fixture(case, executable, evidence)
            (evidence / 'stdout.txt').write_bytes(data['stdout'].encode('utf-8'))
            (evidence / 'stderr.txt').write_bytes(data['stderr'].encode('utf-8'))
            fixtures.append({'case': case.name, 'valid': True, 'provenance': data['provenance'],
                             'observations': data['observations']})
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
                                       removable=tuple(transform(line) for line in data['removable']))
                    row = check(case, transformed, enabled, folder / 'checks' / case.name / name / str(enabled), implementation)
                except Exception as exc:
                    row = {'passed': False, 'errors': [type(exc).__name__ + ': ' + str(exc)]}
                rows.append({'case': case.name, 'newline': name, 'enabled': enabled, **row})
    try:
        unchanged = bool(hashes) and hashes == shared.snapshot(implementation) and verifier_before == verifier_hashes()
        tool_unchanged = bool(tool) and tool['sha256'] == hashlib.sha256(Path(executable).read_bytes()).hexdigest()
    except OSError:
        unchanged = tool_unchanged = False
    expected = sum((3 if case.variants else 1) * 2 for case in planned)
    report = {'schema': 'tokenguard.search-contracts/v1', 'timestamp': datetime.now(timezone.utc).isoformat(),
              'python_version': sys.version.split()[0],
              'expected_checks': expected, 'successful_checks': sum(row['passed'] for row in rows),
              'passed': unchanged and tool_unchanged and not input_errors and expected > 0
                        and len(rows) == expected and all(item['valid'] for item in fixtures)
                        and all(row['passed'] for row in rows),
              'source_sha256': hashes, 'source_unchanged_during_run': unchanged,
              'verification_sources': verifier_before,
              'verification_source_sha256': hashlib.sha256(json.dumps(verifier_before, sort_keys=True).encode()).hexdigest(),
              'tool': tool, 'tool_unchanged_during_run': tool_unchanged,
              'fixture_manifest': manifest, 'fixture_manifest_sha256': hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
              'fixtures': fixtures, 'rows': rows, 'input_errors': input_errors, 'model_api_calls': 0,
              'coding_ability_evaluated': False, 'live_claude_integration': False,
              'actual_provider_usage': 'unavailable', 'actual_billed_cost': 'unavailable'}
    (folder / 'report.json').write_text(json.dumps(report, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    with (folder / 'report.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['case', 'newline', 'enabled', 'passed', 'compressed',
            'original_chars', 'delivered_chars', 'artifact_byte_identical', 'recovery_count', 'returned_chars', 'errors'])
        writer.writeheader()
        writer.writerows(rows)
    markdown = ['# Real ripgrep output contracts', '',
                f"Passed: {report['passed']}; checks: {report['successful_checks']}/{expected}.",
                'Actual local rg and manual Hook/CLI processes; no model or live host validation.',
                'Compound shell and synthetic suffixes are separate. Newline variants are transformed.',
                'Tool, implementation and both verifier files are hash-bound. Missing tools retain failed checks.', '',
                '| Case | Newline | Enabled | Passed | Errors |', '| --- | --- | --- | --- | --- |']
    for row in rows:
        markdown.append(f"| {row['case']} | {row['newline']} | {row['enabled']} | {row['passed']} | "
                        + '; '.join(row['errors']).replace('|', '/') + ' |')
    markdown += ['', *input_errors, '', 'Actual API usage and billed cost: unavailable.', '']
    (folder / 'report.md').write_text('\n'.join(markdown), encoding='utf-8')
    return folder


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=BASE / 'reports/search-contracts')
    parser.add_argument('--implementation', type=Path, default=BASE, help='trusted local v0.4 source to execute')
    parser.add_argument('--rg', help='local ripgrep executable; defaults to PATH')
    args = parser.parse_args()
    folder = run(args.output, args.implementation, args.rg)
    report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    print(json.dumps({'report': str(folder), 'passed': report['passed'],
                      'checks': report['successful_checks'], 'expected_checks': report['expected_checks']}, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
