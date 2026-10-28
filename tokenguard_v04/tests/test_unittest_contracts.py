"""Independent unittest contracts must reject lost diagnostics and missing trials."""
import json
from pathlib import Path
import runpy
import subprocess
from unittest.mock import patch

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'benchmark/unittest_contracts.py'


@pytest.fixture
def api():
    return runpy.run_path(str(SCRIPT))


@pytest.mark.parametrize('ending', ['\n', '\r\n', '\r'])
def test_oracle_accepts_only_omission_of_generated_progress(api, ending):
    raw = ending.join(('test_080 (test_contract.ContractCase.test_080) ... ok',
                       '----------------------------------------------------------------------', 'Ran 1 test in 0.01s', 'OK', ''))
    delivered = raw.split(ending, 1)[1]
    assert api['contract_errors'](raw, delivered, ()) == []


@pytest.mark.parametrize('mutation', ['lost', 'changed', 'reordered', 'duplicate'])
def test_oracle_rejects_nonprogress_corruption(api, mutation):
    line = 'test_footer (custom.report.test_footer) ... ok\n'
    raw = 'Ran 1 test in 0.01s\nOK\n' + line * 2
    delivered = {
        'lost': raw.replace(line, ''),
        'changed': raw.replace('custom.report', 'custom.change'),
        'reordered': line * 2 + 'Ran 1 test in 0.01s\nOK\n',
        'duplicate': raw.replace(line * 2, line),
    }[mutation]
    assert 'non-progress line lost, changed or reordered' in api['contract_errors'](raw, delivered, ())


def test_oracle_rejects_missing_fixture_marker_and_summary(api):
    errors = api['contract_errors']('no summary\n', 'no summary\n', ('missing',))
    assert errors == ['fixture missing marker: missing', 'fixture missing test count summary',
                      'fixture missing terminal result']


def test_oracle_rejects_summary_without_terminal_result(api):
    errors = api['contract_errors']('Ran 120 tests in 0.01s\n', 'Ran 120 tests in 0.01s\n', ())
    assert 'fixture missing terminal result' in errors


def test_fixture_with_wrong_test_count_cannot_be_certified(api, tmp_path):
    run = api['run']
    case = api['Case']('success')
    response = subprocess.CompletedProcess('unittest', 0, b'Ran 0 tests in 0.00s\n\nOK\n', b'')
    with patch.dict(run.__globals__, {'cases': lambda: (case,)}), \
            patch.object(subprocess, 'run', return_value=response):
        folder = run(tmp_path)
    report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    assert report['fixtures'][0]['valid'] is False
    assert 'unexpected unittest fixture test count' in report['fixtures'][0]['error']
    assert report['expected_checks'] == len(report['rows']) == 6


@pytest.mark.parametrize('failure', ['timeout', 'invalid_utf8', 'bad_exit'])
def test_fixture_failure_exports_full_planned_denominator(api, tmp_path, failure):
    run = api['run']
    case = api['Case']('success')
    response = subprocess.CompletedProcess('unittest', 0 if failure == 'invalid_utf8' else 2, b'\xff', b'')
    kwargs = {'side_effect': subprocess.TimeoutExpired('unittest', 60)} if failure == 'timeout' else {'return_value': response}
    with patch.dict(run.__globals__, {'cases': lambda: (case,)}), \
            patch.object(subprocess, 'run', **kwargs):
        folder = run(tmp_path)
    report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    assert report['passed'] is False
    assert report['expected_checks'] == len(report['rows']) == 6
    assert report['successful_checks'] == 0
    assert (folder / 'report.csv').is_file() and (folder / 'report.md').is_file()


def test_real_unittest_hook_and_recovery_subprocesses(api, tmp_path):
    run = api['run']
    selected = tuple(case for case in api['cases']() if case.name in ('success', 'docstring', 'native_stderr'))
    with patch.dict(run.__globals__, {'cases': lambda: selected}):
        folder = run(tmp_path)
    report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    assert report['passed'] is True
    assert report['successful_checks'] == report['expected_checks'] == 14
    assert report['source_unchanged_during_run'] is True
    assert report['model_api_calls'] == 0 and report['actual_billed_cost'] == 'unavailable'


def test_missing_implementation_keeps_failed_trials(api, tmp_path):
    run = api['run']
    with patch.dict(run.__globals__, {'cases': lambda: (api['Case']('success'),)}):
        folder = run(tmp_path / 'output', tmp_path / 'missing')
    report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    assert report['passed'] is False
    assert report['expected_checks'] == len(report['rows']) == 6
    assert report['source_unchanged_during_run'] is False


def test_evaluator_can_read_long_generated_artifact_path(api, tmp_path, monkeypatch):
    from tokenguard.storage import save_original
    data = tmp_path / ('deep-' + 'x' * 140) / ('nested-' + 'y' * 40)
    monkeypatch.setenv('TOKENGUARD_DATA_DIR', str(data))
    identifier = save_original('synthetic original\r\n')
    target = data / 'artifacts' / (identifier + '.txt')
    assert len(str(target)) > 260
    assert api['artifact_bytes'](target) == b'synthetic original\r\n'
