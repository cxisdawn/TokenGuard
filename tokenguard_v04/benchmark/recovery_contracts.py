"""Offline selective-recovery contracts; synthetic fixtures, no model calls."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import time
from unittest.mock import patch
import uuid

BASE = Path(__file__).resolve().parents[1]
MAX_PAGES = 120


@dataclass(frozen=True)
class Case:
    name: str
    mode: str
    raw: str
    pattern: str = ''
    budget: int = 320
    context: int = 0
    limit: int = 20
    start: int = 1
    stress: bool = False
    driver: str = 'api'


def cases() -> tuple[Case, ...]:
    result = [
        Case('context_cut', 'grep', 'IMPORTANT_FAILURE\nxxxxxIMPORTANT_FAILURE' + 'x' * 100 + '\n',
             'important_failure', 110, 1, 1),
        Case('context_default_budget', 'grep', 'IMPORTANT_FAILURE\n' + 'x' * 11890 +
             'IMPORTANT_FAILURE' + 'y' * 100 + '\n', 'important_failure', 12000, 1, 1, driver='cli'),
        Case('cursor_digits', 'grep', 'skip\n' * 8 + '😀' * 249 + 'Straße\n' +
             'x' * 388 + 'Straße\r\n' + 'z' * 128 + '\rlast\n', 'STRASSE', 337, 2, 3, 9),
        Case('scan_eof', 'grep', 'x' * 256, 'absent', 200, stress=True),
        Case('scan_crossing', 'grep', 'x' * 253 + 'Straße' + 'y' * 300, 'STRASSE',
             240, stress=True),
        Case('scan_default_eof', 'grep', 'x' * (8 * 1024 * 1024), 'absent', 13, driver='cli'),
        Case('show_controls', 'show', '(no matches)\n' + 'a' * 255 + '\r\n' +
             '原😀\u2028\x0b\x85\ufeff' * 60 +
             '\n[TokenGuard: retrieval limit reached; next_start_line=999; next_start_char=3]\n'
             + 'z' * 300 + '\rlast', budget=220, stress=True),
    ]
    for seed in range(12):
        generator = random.Random(8191 + seed)
        raw = ''.join(generator.choice(['a', '原', '😀', '\u2028']) * generator.randrange(1, 401)
                      + generator.choice(['\n', '\r', '\r\n']) for _ in range(6)) + 'last'
        result.append(Case(f'show_{seed:02d}', 'show', raw, budget=generator.randrange(180, 401), stress=True))
    for seed in range(48):
        generator = random.Random(999983 + seed)
        phrase, pattern = generator.choice([('IMPORTANT_FAILURE', 'important_failure'),
                                             ('Straße', 'STRASSE'), ('İstanbul', 'i\u0307stanbul')])
        raw = ''.join(generator.choice(['x', '原', '😀']) * generator.randrange(100, 601)
                      + (phrase if generator.randrange(2) else '')
                      + generator.choice(['\n', '\r', '\r\n']) for _ in range(7)) + phrase
        context, limit, budget = generator.randrange(3), generator.randrange(1, 4), generator.randrange(240, 401)
        result.append(Case(f'scan_{seed:02d}', 'grep', raw, pattern, budget, context, limit, stress=True))
    return tuple(result)


def fixture(case: Case) -> dict:
    return {'case': case.name, 'mode': case.mode,
            'input_sha256': hashlib.sha256(case.raw.encode('utf-8')).hexdigest(),
            'input_chars': len(case.raw), 'pattern': case.pattern, 'max_chars': case.budget,
            'context': case.context, 'limit': case.limit, 'start': case.start,
            'profile': 'scan256_index64' if case.stress else 'production_bounds', 'driver': case.driver}


def page_body(text: str, truncated: bool, following: tuple[int, int]) -> str:
    if not truncated:
        return text
    marker = (f'\n[TokenGuard: retrieval limit reached; next_start_line={following[0]}; '
              f'next_start_char={following[1]}]\n')
    if not text.endswith(marker):
        raise ValueError('continuation marker differs from audited cursor')
    return text[:-len(marker)]


def content_errors(case: Case, bodies: list[str], cursors: list[tuple[int, int]]) -> list[str]:
    lines = list(io.StringIO(case.raw, newline=''))
    if case.mode == 'show':
        expected = ''.join(lines[case.start - 1:])
        return [] if ''.join(bodies) == expected else ['show differs from original characters']
    expected = {n for n, line in enumerate(lines, 1)
                if n >= case.start and case.pattern.casefold() in line.casefold()}
    recovered = set()
    errors = []
    for body, cursor in zip(bodies, cursors):
        if body == '(no matches)\n':
            continue
        for line in io.StringIO(body, newline=''):
            if re.fullmatch(r'\[TokenGuard: omitted \d+ context lines; use show for original\]\n', line):
                continue
            match = re.fullmatch(r'(\d+): (.*)', line, re.DOTALL)
            if not match or not 1 <= int(match[1]) <= len(lines):
                errors.append('unrecognized recovered record')
                continue
            number, payload = int(match[1]), match[2]
            offset = cursor[1] if number == cursor[0] else 0
            candidates = [(offset, payload)]
            window = re.fullmatch(r'\[char (\d+)\] (.*)', payload, re.DOTALL)
            if window:
                candidates.append((int(window[1]), window[2]))
            valid = [(column, text) for column, text in candidates
                     if column <= len(lines[number - 1]) and lines[number - 1][column:].startswith(text)]
            if not valid:
                errors.append(f'source text differs at line {number}')
            elif any(case.pattern.casefold() in text.casefold() for _, text in valid):
                recovered.add(number)
    missing = expected - recovered
    if missing:
        errors.append('matching lines lost: ' + ','.join(map(str, sorted(missing))))
    return sorted(set(errors))


def _check(case: Case, implementation: Path, data: Path) -> dict:
    from tokenguard import storage, line_index
    from tokenguard.audit import events
    started = time.perf_counter()
    row = {**fixture(case), 'passed': False, 'completed': False, 'pages': 0,
           'returned_chars': 0, 'max_returned_chars': 0, 'scanned_chars': 0, 'errors': []}
    bodies, cursors = [], []
    os.environ['TOKENGUARD_DATA_DIR'] = str(data)
    os.environ['TOKENGUARD_INDEX_ENABLED'] = '1'
    try:
        with ExitStack() as stack:
            if case.stress:
                stack.enter_context(patch.object(storage, 'MAX_SCAN', 256))
                stack.enter_context(patch.object(line_index, 'CHUNK', 64))
                stack.enter_context(patch.object(line_index, 'BYTE_STRIDE', 64))
            identifier = storage.save_original(case.raw)
            cursor = (case.start, 0)
            for page in range(MAX_PAGES):
                if case.driver == 'cli':
                    args = [sys.executable, '-B', '-X', 'utf8', str(implementation / 'tg.py'),
                            case.mode, identifier, case.pattern, '--context', str(case.context),
                            '--limit', str(case.limit), '--max-chars', str(case.budget),
                            '--start', str(cursor[0]), '--start-char', str(cursor[1])]
                    process = subprocess.run(args, capture_output=True, timeout=30)
                    if process.returncode or process.stderr:
                        raise ValueError('recovery CLI failed')
                    text = process.stdout.decode('utf-8')
                elif case.mode == 'show':
                    text = storage.read_lines(identifier, cursor[0], 100, char_offset=cursor[1], max_chars=case.budget)
                else:
                    text = storage.grep_lines(identifier, case.pattern, start=cursor[0], char_offset=cursor[1],
                                              context=case.context, limit=case.limit, max_chars=case.budget)
                audited, malformed = events()
                recoveries = [event for event in audited if event.get('event') == 'recovery']
                if malformed or len(recoveries) != page + 1:
                    raise ValueError('recovery audit count differs')
                event = recoveries[-1]
                if event.get('artifact_id') != identifier or event.get('returned_chars') != len(text):
                    raise ValueError('recovery audit differs from delivered text')
                if len(text) > case.budget:
                    raise ValueError('return budget exceeded')
                row['pages'] += 1
                row['returned_chars'] += len(text)
                row['max_returned_chars'] = max(row['max_returned_chars'], len(text))
                row['scanned_chars'] += event['scanned_chars']
                following = (event['next_start_line'], event['next_start_char'])
                body = page_body(text, event['truncated'], following)
                # An empty suffix status is not part of the original show text.
                source_lines = list(io.StringIO(case.raw, newline=''))
                suffix = ''.join(source_lines[cursor[0] - 1:])
                if cursor[0] <= len(source_lines):
                    suffix = source_lines[cursor[0] - 1][cursor[1]:] + ''.join(source_lines[cursor[0]:])
                if case.mode == 'show' and not suffix and body == '(no matches)\n':
                    body = ''
                bodies.append(body)
                cursors.append(cursor)
                if not event['truncated']:
                    row['completed'] = True
                    break
                if following <= cursor:
                    raise ValueError('continuation cursor did not advance')
                cursor = following
            else:
                raise ValueError('page limit reached before completion')
            row['errors'].extend(content_errors(case, bodies, cursors))
            row['artifact_byte_identical'] = storage.artifact_path(identifier).read_bytes() == case.raw.encode('utf-8')
            if not row['artifact_byte_identical']:
                row['errors'].append('artifact bytes changed')
    except Exception as exc:
        row['errors'].append(type(exc).__name__ + ': ' + str(exc))
    row['passed'] = row['completed'] and not row['errors']
    row['latency_ms'] = round((time.perf_counter() - started) * 1000, 3)
    return row


def _snapshot(implementation: Path) -> dict[str, str]:
    required = [implementation / 'tokenguard/__init__.py', implementation / 'tokenguard/storage.py', implementation / 'tg.py']
    for path in required:
        if not path.is_file():
            raise ValueError('required implementation source missing')
    sources = sorted((implementation / 'tokenguard').glob('*.py')) + [implementation / 'tg.py']
    return {path.relative_to(implementation).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}


def _export(folder: Path, report: dict) -> None:
    (folder / 'report.json').write_text(json.dumps(report, indent=2, ensure_ascii=True, allow_nan=False) + '\n', encoding='utf-8')
    with (folder / 'report.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['case', 'mode', 'profile', 'driver', 'passed', 'completed',
            'pages', 'returned_chars', 'max_returned_chars', 'scanned_chars', 'latency_ms', 'errors'], extrasaction='ignore')
        writer.writeheader()
        writer.writerows(report['rows'])
    lines = ['# Selective recovery contracts', '',
             f"Passed: {report['passed']}; checks: {report['successful_checks']}/{report['expected_checks']}.",
             'Synthetic fixtures and local processes only. API and CLI drivers are identified per row.',
             'The scan256_index64 profile lowers scan/index bounds; production_bounds keeps actual defaults.',
             'Checks line-level keyword reachability, exact show reconstruction, budgets, cursors and audit counts.',
             'Not coding ability, live integration, task token savings, billing, or a performance comparison.', '',
             '| Case | Profile | Driver | Passed | Pages | Errors |', '| --- | --- | --- | --- | --- | --- |']
    for row in report['rows']:
        errors = '; '.join(row['errors']).replace('|', '/')
        lines.append(f"| {row['case']} | {row['profile']} | {row['driver']} | {row['passed']} | {row.get('pages', 0)} | {errors} |")
    lines += ['', *report['input_errors'], '', 'Actual provider usage and billed cost: unavailable.', '']
    (folder / 'report.md').write_text('\n'.join(lines), encoding='utf-8')


def _valid_row(row: dict, spec: dict) -> bool:
    allowed = set(spec) | {'passed', 'completed', 'pages', 'returned_chars', 'max_returned_chars',
                           'scanned_chars', 'errors', 'artifact_byte_identical', 'latency_ms'}
    if set(row) - allowed or any(type(row.get(key)) is not type(value) or row[key] != value for key, value in spec.items()):
        return False
    if any(type(row.get(key)) is not bool for key in ('passed', 'completed')):
        return False
    if not isinstance(row.get('errors'), list) or any(not isinstance(error, str) for error in row['errors']):
        return False
    for key in ('pages', 'returned_chars', 'max_returned_chars', 'scanned_chars'):
        if type(row.get(key)) is not int or row[key] < 0:
            return False
    if (row['pages'] > MAX_PAGES or row['max_returned_chars'] > spec['max_chars'] or
            row['returned_chars'] < row['max_returned_chars']):
        return False
    latency = row.get('latency_ms')
    if type(latency) not in (int, float) or latency < 0:
        return False
    try:
        if not math.isfinite(latency):
            return False
    except OverflowError:
        return False
    if row['passed']:
        return (row['completed'] and row['pages'] > 0 and row['errors'] == [] and
                row.get('artifact_byte_identical') is True and row['max_returned_chars'] > 0)
    return bool(row['errors'])


def run(output: Path, implementation: Path = BASE) -> Path:
    implementation = implementation.resolve()
    folder = output.resolve() / ('recovery-contracts-' + uuid.uuid4().hex)
    folder.mkdir(parents=True, exist_ok=False)
    planned = tuple(cases())
    manifest = [fixture(case) for case in planned]
    errors, sources, worker_rows = [], {}, []
    harness = Path(__file__).resolve()
    harness_hash = hashlib.sha256(harness.read_bytes()).hexdigest()
    started = time.perf_counter()
    try:
        sources = _snapshot(implementation)
        env = os.environ.copy()
        for key in tuple(env):
            if key.startswith('TOKENGUARD_') or key == 'PYTHONPATH':
                env.pop(key)
        env.update(PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1')
        result = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(harness), '--_worker',
                                '--implementation', str(implementation), '--output', str(folder / 'data')],
                                input=json.dumps(manifest).encode('utf-8'), capture_output=True, env=env, timeout=120)
        (folder / 'worker.stdout.txt').write_bytes(result.stdout)
        (folder / 'worker.stderr.txt').write_bytes(result.stderr)
        if result.returncode or result.stderr:
            raise ValueError('worker exit/stderr indicates failure')
        parsed_rows = json.loads(result.stdout.decode('utf-8'))['rows']
        if not isinstance(parsed_rows, list):
            raise ValueError('worker rows must be a list')
        worker_rows = parsed_rows
    except Exception as exc:
        errors.append(type(exc).__name__ + ': ' + str(exc))
    rows = []
    for spec in manifest:
        matching = [row for row in worker_rows if isinstance(row, dict) and row.get('case') == spec['case']]
        if len(matching) != 1 or not _valid_row(matching[0], spec):
            rows.append({**spec, 'passed': False, 'completed': False, 'pages': 0,
                         'errors': ['missing, duplicate, invalid or mismatched worker evidence']})
        else:
            rows.append(matching[0])
    if len(worker_rows) != len(manifest) or any(not isinstance(row, dict) or row.get('case') not in
                                               {spec['case'] for spec in manifest} for row in worker_rows):
        errors.append('worker result count/identity differs from plan')
    try:
        unchanged = bool(sources) and sources == _snapshot(implementation) and hashlib.sha256(harness.read_bytes()).hexdigest() == harness_hash
    except (OSError, ValueError):
        unchanged = False
    successful = sum(row.get('passed') is True for row in rows)
    report = {'schema': 'tokenguard.recovery-contracts/v1', 'timestamp': datetime.now(timezone.utc).isoformat(),
              'python_version': sys.version.split()[0], 'passed': bool(manifest) and unchanged and not errors and successful == len(manifest),
              'expected_checks': len(manifest), 'successful_checks': successful, 'source_sha256': sources,
              'verification_source_sha256': harness_hash, 'source_unchanged_during_run': unchanged,
              'fixture_manifest_sha256': hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
              'fixture_manifest': manifest, 'input_errors': errors, 'rows': rows,
              'elapsed_seconds': round(time.perf_counter() - started, 3), 'model_api_calls': 0,
              'coding_ability_evaluated': False, 'live_claude_integration': False,
              'actual_provider_usage': 'unavailable', 'actual_billed_cost': 'unavailable'}
    _export(folder, report)
    return folder


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=BASE / 'reports/recovery-contracts')
    parser.add_argument('--implementation', type=Path, default=BASE,
                        help='trusted local v0.4 source directory to execute in an isolated worker')
    parser.add_argument('--_worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args._worker:
        requested = json.loads(sys.stdin.buffer.read())
        available = {case.name: case for case in cases()}
        if not isinstance(requested, list) or any(spec != fixture(available[spec['case']]) for spec in requested):
            raise ValueError('worker fixture manifest differs')
        sys.path.insert(0, str(args.implementation.resolve()))
        rows = [_check(available[spec['case']], args.implementation.resolve(), args.output / spec['case']) for spec in requested]
        print(json.dumps({'rows': rows}, ensure_ascii=True))
        return 0
    folder = run(args.output, args.implementation)
    report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    print(json.dumps({'report': str(folder), 'passed': report['passed'], 'checks': report['successful_checks'],
                      'expected_checks': report['expected_checks']}, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
