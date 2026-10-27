"""Failed or incomplete fixture evidence cannot disappear from the A/B denominator."""
import csv
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
from unittest.mock import patch

import pytest

from tokenguard import audit


BASE = Path(__file__).resolve().parents[1]


def api(name):
    return runpy.run_path(str(BASE / 'benchmark' / name))


def test_mock_worker_keeps_partial_audit_separate_from_task_check(tmp_path):
    module = api('run.py')
    project = tmp_path / 'project'
    shutil.copytree(BASE / 'benchmark/fixture', project)
    config = {'project': str(project), 'experiment_id': 'mock-integrity', 'session_id': 's',
              'variant': 'optimized', 'case': 'build_errors'}
    def corrupt(*args, **kwargs):
        path = audit.root_dir() / 'audit-v04.jsonl'
        with path.open('ab') as stream:
            stream.write(b'\xff\n')
        return audit.summary(*args, **kwargs)
    module['worker'].__globals__['summary'] = corrupt
    with patch.dict(os.environ):
        result = module['worker'](config)
    assert result['success'] is True  # Failure text really was preserved.
    assert result['audit_complete'] is False
    assert result['compressions'] is None
    assert result['observed_audit']['compressions'] == 1
    assert result['observed_audit']['malformed_rows'] == 1


@pytest.mark.parametrize('failure', ['nonzero', 'timeout', 'malformed', 'truncated', 'exception'])
def test_mock_failure_exports_all_planned_trials_and_stops(tmp_path, monkeypatch, failure):
    module = api('run.py')
    state = module['run'].__globals__
    state['CASES'] = ('build_errors',)
    calls = []
    def failed(*args, **kwargs):
        calls.append(1)
        if failure == 'exception':
            raise OSError('private detail')
        return {'exit_code': 7 if failure == 'nonzero' else 0,
                'timed_out': failure == 'timeout', 'outputs_truncated': failure == 'truncated',
                'elapsed_ms': 5, 'stdout': '{invalid', 'stderr': 'fixture failure'}
    state['run_process'] = failed
    # Old implementation calls subprocess.run and drops its first failure.
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k:
                        subprocess.CompletedProcess(a, 7, '{invalid', 'fixture failure'))
    folder = module['run'](tmp_path / 'reports', 2)
    result = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    assert result['planned_trials'] == 4 and result['attempted_trials'] == 1
    assert result['completed_trials'] == 0
    assert len(calls) == 1
    assert result['rows'][0]['status'] == 'infrastructure_failed'
    assert all(row['status'] == 'not_run' for row in result['rows'][1:])
    assert all(row['success'] is None for row in result['rows'])
    assert result['aggregate'][0]['pairs'] == 2
    assert result['aggregate'][0]['eligible_pairs'] == 0
    assert result['aggregate'][0]['mean_character_reduction_after_recovery'] is None
    assert 'private detail' not in (folder / 'report.json').read_text(encoding='utf-8')
    assert (folder / 'report.csv').is_file() and (folder / 'report.md').is_file()


@pytest.mark.parametrize('fault', ['not_run', 'incomplete_audit', 'missing_metric', 'negative_metric'])
def test_baseline_study_keeps_missing_measurements_unknown(tmp_path, fault):
    module = api('baseline_demo.py')
    state = module['run'].__globals__
    rows = [{'arm': arm, 'status': 'completed', 'task_success': True,
             'elapsed_ms': 10, 'hook_process_count': 1, 'hook_process_elapsed_ms': 3,
             'compression': {'data_available': True, 'compressions': 1, 'saved_chars': 100}}
            for arm in ('baseline', 'optimized')]
    if fault == 'not_run':
        rows[1] = {'arm': 'optimized', 'status': 'not_run', 'task_success': None}
    elif fault == 'incomplete_audit':
        rows[1]['compression']['data_available'] = False
    elif fault == 'missing_metric':
        rows[1]['compression']['compressions'] = None
    else:
        rows[1]['compression']['compressions'] = -5
    state.update({'BASELINE_MODES': ('no_hook',), 'prepare': lambda *a: tmp_path / 'experiment',
                  'execute': lambda *a, **k: None, 'report': lambda *a: {'runs': rows}})
    result = module['run'](tmp_path / 'reports', 1)
    assert result['trials'] == 2
    optimized = result['summary'][1]
    assert optimized['compressions'] is None
    assert optimized['measurement_coverage']['compressions'] == {'observed_trials': 0, 'planned_trials': 1}
    folder = Path(result['report_folder'])
    assert 'unavailable' in (folder / 'report.md').read_text(encoding='utf-8')
    with (folder / 'report.csv').open(encoding='utf-8', newline='') as stream:
        assert list(csv.DictReader(stream))[1]['compressions'] == 'unavailable'


def stub_runner(tmp_path, mutate=None):
    module = api('run.py')
    state = module['run'].__globals__
    state['CASES'] = ('build_errors',)
    calls = []
    def local(argv, *args, **kwargs):
        config = json.loads(Path(argv[-1]).read_text(encoding='utf-8'))
        calls.append(config['variant'])
        with patch.dict(os.environ):
            row = module['worker'](config)
        if mutate:
            mutate(row)
        return {'exit_code': 0, 'timed_out': False, 'outputs_truncated': False,
                'elapsed_ms': 5, 'stdout': json.dumps(row), 'stderr': ''}
    state['run_process'] = local
    folder = module['run'](tmp_path/'reports', 2)
    return json.loads((folder/'report.json').read_text(encoding='utf-8')), calls


def test_complete_mock_pairs_have_measured_zero_and_counterbalanced_order(tmp_path):
    result, calls = stub_runner(tmp_path)
    assert calls == ['baseline', 'optimized', 'optimized', 'baseline']
    assert result['attempted_trials'] == result['completed_trials'] == result['planned_trials'] == 4
    assert result['aggregate'][0]['eligible_pairs'] == 2
    assert 0 < result['aggregate'][0]['mean_character_reduction_after_recovery'] < 1
    assert all(r['comparison_eligible'] for r in result['rows'])
    assert all(r['recovery_count'] == 0 for r in result['rows'])
    assert result['rows'][0]['compressions'] == 0


@pytest.mark.parametrize('field,value', [
    ('session_id', 'different-session'), ('round', True), ('initial_snapshot_sha256', 'a'*64),
    ('final_code_sha256', 'b'*64), ('success', 1), ('audit_complete', None),
    ('compressions', -1), ('tool_calls', True), ('visible_tool_chars', None),
    ('estimated_visible_tool_tokens', float('inf')), ('recovery_latency_ms', float('nan')),
    ('compressions', 100), ('recovered_chars', 9000), ('recovery_latency_ms', 123456),
])
def test_invalid_worker_measurement_stops_with_unknown_outcome(tmp_path, field, value):
    result, calls = stub_runner(tmp_path, lambda row: row.update({field: value}))
    assert len(calls) == 1
    assert result['rows'][0]['status'] == 'infrastructure_failed'
    assert result['rows'][0]['success'] is None
    assert result['aggregate'][0]['eligible_pairs'] == 0
    assert result['aggregate'][0]['mean_character_reduction_after_recovery'] is None


def test_incomplete_mock_audit_preserves_task_fact_but_stops_comparison(tmp_path):
    def incomplete(row):
        row.update(audit_complete=False, compressions=None, recovery_count=None, recovered_chars=None)
        row['observed_audit'].update(data_available=False, malformed_rows=1)
    result, calls = stub_runner(tmp_path, incomplete)
    assert len(calls) == 1
    assert result['rows'][0]['success'] is True
    assert result['rows'][0]['status'] == 'evidence_incomplete'
    assert result['rows'][0]['comparison_eligible'] is False
    assert result['aggregate'][0]['mean_character_reduction_after_recovery'] is None


def test_failed_task_remains_failed_without_savings_subset(tmp_path):
    result, calls = stub_runner(tmp_path, lambda row: row.update(success=False))
    assert len(calls) == 4
    assert result['completed_trials'] == 4
    assert result['aggregate'][0]['baseline_successes'] == 0
    assert result['aggregate'][0]['unknown_outcomes'] == 0
    assert all(r['success'] is False for r in result['rows'])
    assert result['aggregate'][0]['mean_character_reduction_after_recovery'] is None


@pytest.mark.parametrize('stage', ['prepare', 'execute', 'report'])
def test_baseline_study_exports_plan_when_a_stage_raises(tmp_path, stage):
    module = api('baseline_demo.py')
    state = module['run'].__globals__
    calls = []
    def fail(*args, **kwargs):
        calls.append(stage)
        raise OSError('private failure detail')
    state.update({'BASELINE_MODES': ('no_hook', 'disabled_hook'),
                  'prepare': lambda *args: tmp_path/'experiment',
                  'execute': lambda *args, **kwargs: None,
                  'report': lambda *args: (_ for _ in ()).throw(FileNotFoundError())})
    state[stage] = fail
    result = module['run'](tmp_path/'reports', 2)
    assert len(calls) == 1
    assert result['trials'] == 8 and result['successful_trials'] == 0
    assert len(result['summary']) == 4
    assert all(r['trials'] == 2 and r['unknown_outcomes'] == 2 for r in result['summary'])
    assert all(r['compressions'] is None for r in result['summary'])
    folder = Path(result['report_folder'])
    assert (folder/'plan.json').is_file() and (folder/'report.csv').is_file()
    assert result['execution_errors']
    assert 'private failure detail' not in (folder/'report.json').read_text(encoding='utf-8')


def test_large_recovery_cannot_qualify_with_explicit_failed_recovery(tmp_path):
    from tokenguard.mock_reporting import export, unrun
    rows = []
    for arm in ('baseline', 'optimized'):
        row = unrun({'case':'large_artifact_recovery', 'variant':arm, 'round':1})
        row.update(status='completed', success=True, failure_details_preserved=True,
                   failure_recovery_verified=False, initial_snapshot_sha256='a'*64,
                   final_code_sha256='a'*64, visible_tool_chars=100)
        rows.append(row)
    result = export(tmp_path, 'mock', 'fixture', rows, ('large_artifact_recovery',), 1)
    assert result['aggregate'][0]['eligible_pairs'] == 0
    assert result['aggregate'][0]['mean_character_reduction_after_recovery'] is None
