"""Repeatable local examples through the real hook and recovery CLI; no model calls."""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from tokenguard.experiment_validation import run_process


@dataclass
class Case:
    name: str
    stdout: str
    command: str
    status: str
    strategy: str | None = None
    stderr: str = ''
    exit_code: int = 0
    provenance: str = 'synthetic fixture'
    required: tuple[str, ...] = ()
    occurrences: dict[str, int] = field(default_factory=dict)
    recovery: str = ''
    suffix: str = ''


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def execute(argv: list[str], cwd: Path, env: dict[str, str], *, stdin: str | None = None,
            expected_exit: int = 0) -> dict:
    result = run_process(argv, cwd, env, timeout=60, stdin=stdin)
    require(not result['timed_out'] and not result['outputs_truncated'], 'process timeout or output limit')
    require(result['exit_code'] == expected_exit, 'unexpected process exit')
    return result


def rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def local_cases(env: dict[str, str]) -> list[Case]:
    """Run real local tools against copies; never modify the original fixture."""
    fixture = BASE / 'benchmark' / 'fixture'
    original = {name: (fixture / name).read_bytes() for name in ('arithmetic.py', 'test_arithmetic.py')}
    cases = []
    with tempfile.TemporaryDirectory(prefix='tg-examples-') as temporary:
        work = Path(temporary)
        for name in original:
            shutil.copyfile(fixture / name, work / name)
        for fixed in (False, True):
            if fixed:
                source = (work / 'arithmetic.py').read_text(encoding='utf-8')
                require(source.count('a // b') == 1, 'fixture patch no longer applies')
                (work / 'arithmetic.py').write_text(source.replace('a // b', 'a / b'), encoding='utf-8')
            xml = work / 'pytest.xml'
            run = execute([sys.executable, '-m', 'pytest', '-v', '--color=no', '-p', 'no:cacheprovider',
                           '--junitxml', str(xml), '--', 'test_arithmetic.py'], work, env,
                          expected_exit=0 if fixed else 1)
            tests = list(ET.parse(xml).iter('testcase'))
            require(len(tests) == 504, 'fixture must run all 504 tests')
            require(sum(t.find('failure') is not None for t in tests) == (0 if fixed else 3),
                    'unexpected fixture failure count')
            require(not any(t.find('error') is not None or t.find('skipped') is not None for t in tests),
                    'fixture errors/skips are not expected')
            raw = run['stdout']
            suffix = ''
            if not fixed:
                header = re.search(r'(?m)^=+ FAILURES =+\r?$', raw)
                require(header is not None, 'pytest failure section missing')
                suffix = raw[header.start():]
            cases.append(Case(
                'pytest_pass' if fixed else 'pytest_fail', raw, 'python -m pytest -v',
                'compressed', 'pytest_pass_summary', run['stderr'], run['exit_code'],
                'actual local pytest: 504 cases, copied fixture',
                ('504 passed',) if fixed else ('3 failed, 501 passed', 'FAILED', 'AssertionError'),
                recovery='test_arithmetic.py::test_exact[123]', suffix=suffix))
        require((work / 'test_arithmetic.py').read_bytes() == original['test_arithmetic.py'],
                'copied tests must remain unchanged')
        run = execute([sys.executable, '-c',
                       "print('INFO local step complete\\n' * 700, end='', flush=True); "
                       "raise RuntimeError('TG_STDERR_SENTINEL')"], work, env, expected_exit=1)
        require('Traceback (most recent call last)' in run['stderr'] and
                'TG_STDERR_SENTINEL' in run['stderr'], 'real stderr traceback missing')
        cases.append(Case('stderr_traceback', run['stdout'], 'python demo.py', 'compressed',
                          'adjacent_duplicates', run['stderr'], 1, 'actual local Python exception',
                          recovery='INFO local step complete'))
    require(all((fixture / name).read_bytes() == content for name, content in original.items()),
            'original fixture changed')
    return cases


def synthetic_cases() -> list[Case]:
    passes = ''.join(f'test_mixed.py::test_ok[{i}] PASSED [ 90%]\n' for i in range(500))
    mixed = ('test_mixed.py::test_skip SKIPPED [ 91%]\n'
             'test_mixed.py::test_bug FAILED [ 95%]\n'
             'test_mixed.py::test_setup ERROR [100%]\n'
             'UNKNOWN_PLUGIN_NOTE: keep this verbatim\n'
             '================ FAILURES ================\n'
             'E assert 3 == 5\n'
             '================ ERRORS ================\n'
             'E fixture database not found\n'
             '======== 1 failed, 1 error, 1 skipped, 500 passed in 1.2s ========\n')
    warnings = 'src/legacy.c:12: warning: deprecated API\n' * 1000
    error = 'src/main.c:99: error: TG_UNDEFINED_SYMBOL\n'
    unit_tail = ('test_bug (test_mod.C.test_bug) ... FAIL\n'
                 'FAIL: test_bug (test_mod.C.test_bug)\n'
                 'AssertionError: TG_UNIT_FAILURE\nRan 401 tests in 0.42s\nFAILED (failures=1)\n')
    progress = '  ' + '\u2501' * 24 + ' 1/2 MB 5 MB/s\n'
    diff = ('diff --git a/demo.py b/demo.py\n--- a/demo.py\n+++ b/demo.py\n@@ -1,500 +1,500 @@\n' +
            '-old_value = 1\n+new_value = 2\n' * 500)
    trace = ('INFO prelude\n' * 700 + 'Traceback (most recent call last):\n'
             '  File "example.py", line 4, in <module>\n'
             '    raise ValueError("TG_TRACE_FAILURE")\nValueError: TG_TRACE_FAILURE\n')
    return [
        Case('pytest_mixed', passes + mixed, 'pytest -v', 'compressed', 'pytest_pass_summary',
             exit_code=1, required=('SKIPPED', 'FAILED', 'ERROR', 'UNKNOWN_PLUGIN_NOTE'),
             recovery='test_mixed.py::test_ok[123]', suffix=mixed),
        Case('build_errors', warnings + error * 3 + 'BUILD FAILED: exit 1\nUNKNOWN_BUILD_NOTE: keep\n',
             'cmake --build build', 'compressed', 'adjacent_duplicates', exit_code=1,
             required=('BUILD FAILED: exit 1', 'UNKNOWN_BUILD_NOTE: keep'), occurrences={error: 3},
             recovery='src/legacy.c:12: warning: deprecated API'),
        Case('unittest_fail', ''.join(f'test_{i} (test_mod.C.test_{i}) ... ok\n' for i in range(400)) + unit_tail,
             'python -m unittest -v', 'compressed', 'unittest_pass_summary', exit_code=1,
             required=('TG_UNIT_FAILURE', 'FAILED (failures=1)'), suffix=unit_tail,
             recovery='test_123 (test_mod.C.test_123)'),
        Case('pip_progress', 'Collecting example-pkg==1.2.3\n' + progress * 500 +
             'Successfully installed example-pkg-1.2.3\nWARNING: TG_PIP_NOTE\n',
             'pip install example-pkg==1.2.3', 'compressed', 'pip_progress',
             required=('Collecting example-pkg==1.2.3', 'Successfully installed example-pkg-1.2.3',
                       'WARNING: TG_PIP_NOTE'), recovery='1/2 MB 5 MB/s'),
        Case('file_search', ''.join(f'src/module_{i:04d}.py\n' for i in range(1000)),
             'rg --files', 'compressed', 'file_search', recovery='src/module_0777.py',
             required=('src/module_0000.py', 'src/module_0019.py', '980 file paths hidden')),
        Case('git_diff', diff, 'git diff', 'protected_diff_or_traceback'),
        Case('stdout_traceback', trace, 'python example.py', 'protected_diff_or_traceback', exit_code=1),
        Case('short_output', '1 passed in 0.01s\n', 'pytest -v', 'below_threshold'),
        Case('incomplete_pytest', passes, 'pytest -v', 'incomplete_pytest'),
        Case('unknown_format', ''.join(f'UNRECOGNIZED datum={i:05d} keep this unique line\n' for i in range(500)),
             'custom-tool', 'insufficient_saving'),
    ]


def verify(case: Case, output: Path, env: dict[str, str], experiment: str) -> dict:
    folder = output / case.name
    folder.mkdir()
    session = experiment + '-' + case.name
    response = {'stdout': case.stdout, 'stderr': case.stderr, 'exitCode': case.exit_code,
                'interrupted': False, 'isImage': False, 'extra': {'preserve': [1, 'unknown field']}}
    event = {'hook_event_name': 'PostToolUse', 'session_id': session, 'tool_name': 'Bash',
             'tool_input': {'command': case.command}, 'tool_response': response}
    run = execute([sys.executable, str(BASE / 'tokenguard/hook.py'), '--source', 'manual'], BASE, env,
                  stdin=json.dumps(event))
    require(not run['stderr'], case.name + ': unexpected hook stderr')
    visible = response
    if run['stdout']:
        hook = json.loads(run['stdout'])['hookSpecificOutput']
        require(hook['hookEventName'] == 'PostToolUse', 'wrong hook event')
        visible = hook['updatedToolOutput']
    require({k: v for k, v in visible.items() if k != 'stdout'} ==
            {k: v for k, v in response.items() if k != 'stdout'}, case.name + ': response structure changed')
    after = visible['stdout']
    diagnostics = [r for r in rows(output / 'tg-data/diagnostics-v04.jsonl') if r.get('session_id') == session]
    require(len(diagnostics) == 1 and diagnostics[0]['status'] == case.status, case.name + ': wrong status')
    require(all(token in after for token in case.required), case.name + ': important text lost')
    require(all(after.count(token) == count for token, count in case.occurrences.items()),
            case.name + ': critical repetitions changed')
    require(not case.suffix or case.suffix in after, case.name + ': failure suffix changed')
    (folder / 'before.stdout.txt').write_bytes(case.stdout.encode('utf-8'))
    (folder / 'after.stdout.txt').write_bytes(after.encode('utf-8'))
    (folder / 'stderr.txt').write_bytes(case.stderr.encode('utf-8'))
    audit = [r for r in rows(output / 'tg-data/audit-v04.jsonl') if r.get('session_id') == session]
    compressions = [r for r in audit if r['event'] == 'compression']
    returned = 0
    recovery_count = 0
    artifact_identical = None
    if case.status == 'compressed':
        require(len(compressions) == 1, case.name + ': missing/duplicate compression')
        record = compressions[0]
        require(record['strategy'] == case.strategy, case.name + ': unexpected strategy')
        require(record['original_chars'] == len(case.stdout) and record['optimized_chars'] == len(after),
                case.name + ': audit lengths mismatch')
        require(len(after) < len(case.stdout) * .85, case.name + ': metadata erased saving')
        artifact = output / 'tg-data/artifacts' / (record['artifact_id'] + '.txt')
        artifact_identical = artifact.read_bytes() == case.stdout.encode('utf-8')
        require(artifact_identical, case.name + ': original artifact corrupted')
        require(bool(case.recovery), case.name + ': missing recovery check')
        lines = case.stdout.splitlines(keepends=True)
        line_number = next(i + 1 for i, line in enumerate(lines) if case.recovery in line)
        common = ['--compression-id', record['compression_id'], '--max-chars', '500']
        for mode, args in [('grep', [case.recovery, '--context', '0', '--limit', '1']),
                           ('show', ['--start', str(line_number), '--count', '1'])]:
            recovered = execute([sys.executable, str(BASE / 'tg.py'), mode, record['artifact_id'],
                                 *args, *common], BASE, env)
            text = recovered['stdout']
            require(not recovered['stderr'] and case.recovery in text and len(text) <= 500,
                    case.name + ': selective recovery failed')
            if mode == 'show':
                require(text == lines[line_number - 1], case.name + ': line recovery not exact')
            (folder / (mode + '.txt')).write_bytes(text.encode('utf-8'))
            returned += len(text)
        recoveries = [r for r in rows(output / 'tg-data/audit-v04.jsonl')
                      if r.get('compression_id') == record['compression_id'] and r['event'] == 'recovery']
        recovery_count = len(recoveries)
        require(recovery_count == 2 and sum(r['returned_chars'] for r in recoveries) == returned,
                case.name + ': recovery audit mismatch')
        require(returned < len(case.stdout) / 10, case.name + ': recovery returned too much')
    else:
        require(not run['stdout'] and not compressions and after == case.stdout,
                case.name + ': passthrough changed or created an artifact')
    preview = after.split('\n[TokenGuard artifact=', 1)[0]
    (folder / 'preview.txt').write_bytes(preview.encode('utf-8'))
    return {'case': case.name, 'provenance': case.provenance, 'verified': True,
            'status': case.status, 'strategy': case.strategy, 'original_chars': len(case.stdout),
            'optimized_chars_including_metadata': len(after), 'saved_chars': len(case.stdout) - len(after),
            'character_reduction_ratio': 1 - len(after) / len(case.stdout),
            'response_fields_preserved': True, 'critical_text_preserved': True,
            'full_failure_suffix_preserved': True if case.suffix else None,
            'artifact_byte_identical': artifact_identical,
            'recovery_count': recovery_count, 'returned_chars': returned,
            'exit_code_preserved': case.exit_code, 'stderr_chars_preserved': len(case.stderr),
            'session_id': session, 'files': case.name + '/'}


def run(output_parent: Path) -> Path:
    experiment = 'examples-' + uuid.uuid4().hex
    output = output_parent.resolve() / experiment
    output.mkdir(parents=True)
    env = {**os.environ, 'TOKENGUARD_DATA_DIR': str(output / 'tg-data'), 'TOKENGUARD_ENABLED': '1',
           'TOKENGUARD_DIAGNOSTICS': '1', 'TOKENGUARD_EXPERIMENT_ID': experiment,
           'TOKENGUARD_SOURCE': 'manual', 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1',
           'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}
    cases = local_cases(env) + synthetic_cases()
    results = []
    for case in cases:
        row = verify(case, output, env, experiment)
        results.append(row)
        print(f"PASS {case.name}: {row['original_chars']} -> {row['optimized_chars_including_metadata']} chars; "
              f"{row['status']}; recoveries={row['recovery_count']}", flush=True)
    stats = execute([sys.executable, str(BASE / 'tg.py'), 'stats', '--experiment', experiment, '--json'], BASE, env)
    summary = json.loads(stats['stdout'])
    require(summary['compressions'] == sum(r['status'] == 'compressed' for r in results), 'stats compression mismatch')
    require(summary['recovery_count'] == sum(r['recovery_count'] for r in results), 'stats recovery mismatch')
    require(summary['saved_chars'] == sum(r['saved_chars'] for r in results), 'stats saving mismatch')
    report = {'verification': 'local tools + manual hook subprocess; NOT live Claude integration',
              'experiment_id': experiment, 'model_api_calls': 0, 'verified_cases': len(results),
              'fixture_original_unchanged': True, 'actual_api_usage': 'unavailable',
              'actual_billed_cost': 'unavailable',
              'measurement_scope': 'tool stdout characters including recovery guide; not task tokens or costs',
              'summary': summary, 'cases': results}
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    with (output / 'report.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(results[0]))
        writer.writeheader()
        writer.writerows(results)
    markdown = ['# Verified local examples', '', report['verification'], '',
                'No model calls. Character counts include the recovery guide; no task token/cost savings claim.', '',
                '| Case | Input chars | Visible chars | Status | Recovery reads | Returned chars |',
                '| --- | ---: | ---: | --- | ---: | ---: |']
    for row in results:
        markdown.append(f"| {row['case']} | {row['original_chars']} | {row['optimized_chars_including_metadata']} | "
                        f"{row['status']} | {row['recovery_count']} | {row['returned_chars']} |")
    markdown += ['', 'Each case folder contains before/after stdout and unchanged stderr. Compressed cases also',
                 'include exact one-line show and bounded keyword grep results. preview.txt omits only the guide',
                 'for readability; measured visible counts include it. Full originals and audit stay in tg-data.',
                 'pytest pass/fail examples actually run 504 tests on copied source; other fixtures are labelled.',
                 'All passthrough outputs are verified equal, and all non-stdout response fields are verified equal.',
                 'The entire failure suffix, including final summary, is retained in pytest/unittest failure cases.', '']
    (output / 'report.md').write_text('\n'.join(markdown), encoding='utf-8')
    print(str(output / 'report.md'))
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=BASE / 'reports' / 'examples')
    args = parser.parse_args()
    run(args.output)


if __name__ == '__main__':
    main()
