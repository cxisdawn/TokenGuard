"""Fixed-log navigation/recovery comparison; NO model calls or coding-quality score."""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import re
import sys
import uuid

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from tokenguard.experiment import audit_scope
from tokenguard.experiment_validation import run_process
from tokenguard.audit import summary, diagnostics


@dataclass
class Case:
    name: str
    raw: str
    query: str = 'test_ignore_path'
    retained: str = ''


def fixtures() -> list[Case]:
    header = 'platform local-fixture\ncollected 480 items\n'
    passed = ''.join(f'tests/test_paths.py::TestPaths::{name}[case {n}] PASSED [ 50%]\n'
                     for name in ('test_normal_path', 'test_ignore_path') for n in range(240))
    final = '===== 480 passed in 0.5s =====\n'
    important = ('test_x.py::test_skip SKIPPED\ntest_x.py::test_expected XFAIL\n'
                 'test_x.py::test_surprise XPASS\ntest_x.py::test_bug FAILED\n'
                 'test_x.py::test_setup ERROR\n=== FAILURES ===\n'
                 'AssertionError: expected=4 actual=2\nunknown diagnostic must survive\n'
                 'test_x.py::test_literal PASSED\n'
                 '===== 1 failed, 1 error, 480 passed, 1 skipped in 0.6s =====\n')
    many = ''.join(f'test_many.py::test_ignore_path_{n} PASSED [ 50%]\n' for n in range(160))
    long = ''.join(f'test_long.py::test_ignore_path_{"x" * 2500}[{n}] PASSED\n' for n in range(10))
    return [Case('parameterized', header + passed + final),
            Case('mixed_failures', header + passed + important, retained=important),
            Case('crlf_unicode', (header + passed + final).replace('tests/test_paths.py', '测试[目录]/test_paths.py')
                 .replace('PASSED', '\x1b[32mPASSED\x1b[0m').replace('\n', '\r\n')),
            Case('node_budget', header + many + '===== 160 passed in 0.5s =====\n'),
            Case('name_budget', header + long + '===== 10 passed in 0.5s =====\n'),
            Case('traceback', header + passed + 'Traceback (most recent call last):\nValueError: keep exact\n' + final),
            Case('incomplete', header + passed)]


def require(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def execute(argv: list[str], env: dict[str, str], *, stdin: str | None = None) -> str:
    result = run_process(argv, BASE, env, 30, stdin)
    require(result['exit_code'] == 0 and not result['timed_out'] and not result['outputs_truncated'],
            'Local Hook/recovery process failed')
    return result['stdout']


def indexed_start(text: str, query: str) -> int | None:
    for line in text.splitlines():
        match = re.fullmatch(r'("(?:[^"\\]|\\.)*"): \d+ passed; raw lines (\d+)-\d+', line)
        if match and query.casefold() in json.loads(match.group(1)).casefold():
            return int(match.group(2))
    return None


def compare(output: Path) -> Path:
    folder = output.resolve() / ('pytest-navigation-' + uuid.uuid4().hex)
    folder.mkdir(parents=True, exist_ok=False)
    rows = []
    for case in fixtures():
        for mode in ('off', 'compact', 'indexed'):
            root = folder / case.name / mode
            root.mkdir(parents=True)
            env = {**os.environ, 'PYTHONUTF8': '1', 'TOKENGUARD_ENABLED': '1',
                   'TOKENGUARD_DIAGNOSTICS': '1', 'TOKENGUARD_DATA_DIR': str(root / 'data'),
                   'TOKENGUARD_EXPERIMENT_ID': folder.name}
            event = {'hook_event_name': 'PostToolUse', 'session_id': 'offline-' + uuid.uuid4().hex,
                     'tool_name': 'Bash', 'tool_input': {'command': 'python -m pytest -v'},
                     'tool_response': {'stdout': case.raw, 'stderr': 'ERROR stderr retained', 'exit_code': 1}}
            emitted = execute([sys.executable, str(BASE / 'tokenguard/hook.py'), '--source', 'manual',
                               '--pytest-mode', mode], env, stdin=json.dumps(event))
            changed = json.loads(emitted)['hookSpecificOutput']['updatedToolOutput'] if emitted.strip() else event['tool_response']
            delivered = changed['stdout']
            require({k:v for k,v in changed.items() if k != 'stdout'} ==
                    {k:v for k,v in event['tool_response'].items() if k != 'stdout'}, 'Response structure changed')
            require(not case.retained or case.retained in delivered, 'Important output removed')
            with audit_scope(root / 'data'):
                initial = summary(event['session_id'], folder.name, source='manual')
            compressed = bool(emitted.strip())
            artifact_exact = None
            if compressed:
                row = initial['records'][0]
                identifier, cid = row['artifact_id'], row['compression_id']
                artifact_exact = (root / 'data/artifacts' / (identifier + '.txt')).read_bytes() == case.raw.encode('utf-8')
                require(artifact_exact and len(delivered) < len(case.raw) * .85, 'Artifact or net compression failed')
                start = indexed_start(delivered, case.query) if mode == 'indexed' else None
                recipe = 'index then show' if start is not None else 'grep'
                cli = [sys.executable, str(BASE / 'tg.py')]
                owner = ['--compression-id', cid]
                if start is None:
                    found = execute([*cli, 'grep', identifier, case.query, '--context', '0', '--limit', '1', *owner], env)
                    match = re.match(r'(\d+): ', found)
                    require(match is not None, 'Known fixture query was not recovered')
                    start = int(match.group(1))
                    # grep already returns the needed original line. Requiring
                    # an additional show would inflate compact-mode recovery cost.
                    recovered = found[len(match.group(0)):]
                else:
                    recovered = execute([*cli, 'show', identifier, '--start', str(start), '--count', '1', *owner], env)
                original_line = next(line for n, line in enumerate(io.StringIO(case.raw, newline=''), 1) if n == start)
                require(recovered == original_line and case.query in recovered, 'Selective recovery changed source line')
            else:
                require(delivered == case.raw and case.query in delivered, 'Passthrough is not identical')
                recipe = 'original already visible'
            with audit_scope(root / 'data'):
                stats = summary(event['session_id'], folder.name, source='manual')
                native = summary(event['session_id'], folder.name, source='claude_hook')
                status = diagnostics(event['session_id'], folder.name)['invocations'][-1]['status']
            require(native['compressions'] == 0, 'Manual probe incorrectly counted as native integration')
            (root / 'original.txt').write_bytes(case.raw.encode('utf-8'))
            (root / 'delivered.txt').write_bytes(delivered.encode('utf-8'))
            rows.append({'case': case.name, 'mode': mode, 'status': status, 'compressed': compressed,
                         'original_chars': len(case.raw), 'delivered_chars': len(delivered),
                         'query_visible_before_recovery': case.query in delivered,
                         'lookup_recipe': recipe, 'selective_lookup_verified': True,
                         'important_output_preserved': True, 'artifact_byte_identical': artifact_exact,
                         'recovery_count': stats['recovery_count'], 'returned_chars': stats['returned_chars'],
                         'scanned_chars': stats['scanned_chars'], 'index_build_bytes': stats['index_build_bytes'],
                         'net_saved_chars_after_recovery': len(case.raw)-len(delivered)-stats['returned_chars'],
                         'native_hook_compressions': native['compressions']})
    result = {'schema': 'tokenguard.pytest-navigation/v1', 'model_api_calls': 0,
              'decision_driver': 'fixed queries and deterministic lookup recipes; real local Hook/recovery processes',
              'coding_ability_evaluated': False, 'live_claude_integration': False,
              'fixture_source': 'synthetic; no historical sessions modified',
              'actual_provider_usage': 'unavailable', 'actual_billed_cost': 'unavailable', 'rows': rows}
    (folder / 'report.json').write_text(json.dumps(result, indent=2, ensure_ascii=True)+'\n', encoding='utf-8')
    with (folder / 'report.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    lines = ['# Fixed pytest log navigation', '',
             'ZERO model calls. Synthetic logs, known queries, real local Hook/recovery subprocesses.',
             'This measures available navigation information and deterministic recovery, not coding ability.', '',
             '| Case | Mode | Status | Original | Delivered | Query visible | Recoveries | Returned | Index build bytes |',
             '| --- | --- | --- | ---: | ---: | --- | ---: | ---: | ---: |']
    for r in rows:
        lines.append(f"| {r['case']} | {r['mode']} | {r['status']} | {r['original_chars']} | "
                     f"{r['delivered_chars']} | {r['query_visible_before_recovery']} | "
                     f"{r['recovery_count']} | {r['returned_chars']} | {r['index_build_bytes']} |")
    lines += ['', 'Index bounds may cause complete passthrough, retaining all test names.',
              'Returned/scanned characters and first-use line-index construction bytes are separate costs.',
              'Character reduction excludes task context and is not verified API token/cost savings.',
              'Actual provider usage and billed cost: unavailable.', '']
    (folder / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
    return folder


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=BASE / 'reports/navigation')
    args = parser.parse_args()
    print(compare(args.output))


if __name__ == '__main__':
    main()
