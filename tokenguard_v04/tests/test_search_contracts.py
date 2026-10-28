"""Real search verifier: independent fixtures, explicit missing evidence and faults."""
import json
from pathlib import Path
import runpy
import shutil
import subprocess
from unittest.mock import patch

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'benchmark/search_contracts.py'


@pytest.fixture
def api():
    return runpy.run_path(str(SCRIPT))


def test_missing_ripgrep_keeps_full_failed_denominator(api, tmp_path):
    with patch.object(shutil, 'which', return_value=None):
        folder = api['run'](tmp_path)
    report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    assert report['passed'] is False
    assert report['expected_checks'] == len(report['rows']) == 62
    assert report['successful_checks'] == 0
    assert all(not item['valid'] for item in report['fixtures'])
    assert report['input_errors']
    assert (folder / 'report.csv').is_file() and (folder / 'report.md').is_file()


@pytest.mark.parametrize('failure', ['timeout', 'bad_exit', 'bad_utf8', 'wrong_files', 'extra_note'])
def test_invalid_real_tool_evidence_is_rejected(api, tmp_path, failure):
    tmp_path.mkdir(exist_ok=True)
    fixture = api['fixture']
    shared = fixture.__globals__['shared']
    raw = b'src/not_a_fixture.py\n'
    if failure == 'bad_utf8':
        raw = b'\xff'
    elif failure == 'extra_note':
        raw = ''.join(f'src/module_{i:04d}.py\n' for i in range(300)).encode() + b'NOTE: unexpected.py\n'
    proc = subprocess.CompletedProcess([], 2 if failure == 'bad_exit' else 0, raw, b'')
    kwargs = {'side_effect': subprocess.TimeoutExpired('rg', 60)} if failure == 'timeout' else {'return_value': proc}
    with patch.object(shared, 'execute', **kwargs), pytest.raises((ValueError, UnicodeDecodeError, subprocess.TimeoutExpired)):
        fixture(api['Case']('paths', True), 'rg', tmp_path)


def test_actual_ripgrep_hook_and_selective_recovery(api, tmp_path):
    if not shutil.which('rg'):
        pytest.skip('ripgrep unavailable; real output integration was not run')
    run = api['run']
    selected = tuple(case for case in api['cases']() if case.name in ('paths', 'compound', 'native_stderr'))
    with patch.dict(run.__globals__, {'cases': lambda: selected}):
        folder = run(tmp_path)
    report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    assert report['passed'] is True, report
    assert report['successful_checks'] == report['expected_checks'] == 14
    assert report['tool_unchanged_during_run'] is True
    assert set(report['verification_sources']) == {'search_contracts.py', 'build_pip_contracts.py'}
    assert report['source_unchanged_during_run'] is True
    assert report['actual_billed_cost'] == 'unavailable' and report['model_api_calls'] == 0


@pytest.mark.parametrize('fault', ['truncated_notice', 'wrong_prefix'])
def test_search_verifier_rejects_loss_and_incorrect_sample_prefix(api, tmp_path, fault):
    if not shutil.which('rg'):
        pytest.skip('ripgrep unavailable; real output negative control was not run')
    reference = tmp_path / 'reference'
    shutil.copytree(SCRIPT.parents[1] / 'tokenguard', reference / 'tokenguard',
                    ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copyfile(SCRIPT.parents[1] / 'tg.py', reference / 'tg.py')
    target = reference / 'tokenguard/file_search.py'
    with target.open('a', encoding='utf-8') as stream:
        stream.write('''
def summarize(text, command, protected):
    records = text.splitlines(keepends=True)
    return ''.join(records[:LIMIT]) + f'[TokenGuard: {len(records)-20} file paths hidden; use artifact grep/show]\\n'
'''.replace('LIMIT', '19' if fault == 'wrong_prefix' else '20'))
    run = api['run']
    selected = tuple(case for case in api['cases']() if case.name == ('paths' if fault == 'wrong_prefix' else 'compound'))
    with patch.dict(run.__globals__, {'cases': lambda: selected}):
        folder = run(tmp_path / 'reports', reference)
    report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    assert report['passed'] is False
    assert len(report['rows']) == report['expected_checks'] == 6
    assert report['successful_checks'] == 3
    reason = 'first twenty records' if fault == 'wrong_prefix' else 'non-progress line lost'
    assert all(any(reason in error for error in row['errors']) for row in report['rows'] if row['enabled'])
