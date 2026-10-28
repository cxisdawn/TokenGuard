"""Independent local build/pip evidence must fail closed on broken fixtures."""
import json
from pathlib import Path
import runpy
import shutil
import subprocess
from unittest.mock import patch

import pytest

from tokenguard.core import command_type

SCRIPT = Path(__file__).resolve().parents[1] / 'benchmark/build_pip_contracts.py'


@pytest.mark.parametrize('module', ['compileall', 'py_compile'])
def test_python_byte_compilation_is_audited_as_build(module):
    assert command_type(f'python -m {module} src') == 'build'


@pytest.fixture
def api():
    return runpy.run_path(str(SCRIPT))


@pytest.mark.parametrize('ending', ['\n', '\r\n', '\r'])
def test_oracle_allows_only_fixture_proven_progress_frames(api, ending):
    frame = '   ---- 1.0/1.0 MB ? eta 0:00:00' + ending
    raw = frame * 100 + 'ERROR: missing dependency' + ending
    assert api['preservation_errors'](raw, 'ERROR: missing dependency' + ending, (frame,)) == []


@pytest.mark.parametrize('mutation', ['lost', 'changed', 'reordered', 'duplicate'])
def test_oracle_rejects_diagnostic_corruption(api, mutation):
    raw = 'ERROR: missing dependency\nreason\nreason\n'
    damaged = {'lost': 'reason\nreason\n', 'changed': raw.replace('dependency', 'wheel'),
               'reordered': 'reason\nreason\nERROR: missing dependency\n',
               'duplicate': 'ERROR: missing dependency\nreason\n'}[mutation]
    assert api['preservation_errors'](raw, damaged, ())


def test_progress_prefix_does_not_authorize_removing_inline_error(api):
    frame = '   ---- 1.0/1.0 MB ? eta 0:00:00\n'
    raw = frame.rstrip('\n') + ' ERROR: preserve\n'
    assert api['preservation_errors'](raw, '', (frame,))


def test_repeated_build_record_must_be_reconstructable(api):
    line = 'warning: synthetic noncritical advisory\n'
    raw = line * 200 + 'SyntaxError: invalid syntax\n'
    marker = '[TokenGuard: 199 identical adjacent non-critical lines hidden]\n'
    assert api['rle_errors'](raw, line + marker + 'SyntaxError: invalid syntax\n', line) == []
    assert api['rle_errors'](raw, line + marker.replace('199', '198') + 'SyntaxError: invalid syntax\n', line)


@pytest.mark.parametrize('ending', ['\n', '\r\n', '\r'])
def test_rle_oracle_preserves_final_terminator_before_retrieval_guide(api, ending):
    raw = 'warning: synthetic duplicate advisory' + ending
    assert api['rle_errors'](raw, raw + '[TokenGuard artifact=example]\n', raw) == []


@pytest.mark.parametrize('failure', ['timeout', 'invalid_utf8', 'bad_exit', 'missing_product'])
def test_fixture_failures_keep_all_planned_checks(api, tmp_path, failure):
    run = api['run']
    selected = tuple(case for case in api['cases']() if case.name == 'compile_success')
    proc = subprocess.CompletedProcess([], 2 if failure == 'bad_exit' else 0,
                                       b'\xff' if failure == 'invalid_utf8' else b'Listing src\n', b'')
    kwargs = {'side_effect': subprocess.TimeoutExpired('compileall', 60)} if failure == 'timeout' else {'return_value': proc}
    with patch.dict(run.__globals__, {'cases': lambda: selected}), patch.object(subprocess, 'run', **kwargs):
        folder = run(tmp_path)
    report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    assert report['passed'] is False
    assert report['expected_checks'] == len(report['rows']) == 6
    assert report['successful_checks'] == 0
    assert report['fixtures'][0]['valid'] is False
    assert (folder / 'report.csv').is_file() and (folder / 'report.md').is_file()


def test_missing_implementation_is_not_certified(api, tmp_path):
    run = api['run']
    with patch.dict(run.__globals__, {'cases': lambda: (api['cases']()[0],)}):
        folder = run(tmp_path / 'reports', tmp_path / 'absent')
    report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    assert report['passed'] is False
    assert report['source_unchanged_during_run'] is False
    assert len(report['rows']) == report['expected_checks']


def test_fixture_environment_isolates_cache_color_and_user_hook_overrides(api, tmp_path, monkeypatch):
    overrides = ('PYTHONPATH', 'PYTHONPYCACHEPREFIX', 'SOURCE_DATE_EPOCH', 'FORCE_COLOR',
                 'CLICOLOR_FORCE', 'TOKENGUARD_EXPERIMENT_ID')
    for name in overrides:
        monkeypatch.setenv(name, 'user-override')
    env = api['environment'](tmp_path, False)
    assert all(name not in env for name in overrides)
    assert env['TOKENGUARD_ENABLED'] == '0'
    assert env['TOKENGUARD_SOURCE'] == 'manual'
    assert env['TOKENGUARD_DATA_DIR'] == str(tmp_path)
    assert env['TERM'] == 'dumb' and env['PYTHONIOENCODING'] == 'utf-8'


def test_actual_offline_build_install_renderer_and_recovery(api, tmp_path):
    run = api['run']
    names = ('compile_success', 'compile_error_mixed', 'pip_install', 'pip_renderer', 'pip_error_native')
    selected = tuple(case for case in api['cases']() if case.name in names)
    with patch.dict(run.__globals__, {'cases': lambda: selected}):
        folder = run(tmp_path)
    report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    assert report['passed'] is True, report
    assert report['successful_checks'] == report['expected_checks'] == 26
    assert report['source_unchanged_during_run'] is True
    assert all(row['valid'] for row in report['fixtures'])
    assert report['model_api_calls'] == 0
    assert report['live_claude_integration'] is False
    assert report['actual_billed_cost'] == 'unavailable'


@pytest.mark.parametrize('mutation,case_name,relative,old,new,expected_error', [
    ('prefix', 'pip_renderer_inline_error', 'tokenguard/core.py',
     'match = matcher.fullmatch(clean)', 'match = matcher.match(clean)', 'non-progress line lost'),
    ('stderr', 'pip_renderer_native_error', 'tokenguard/hook.py',
     "changed['stdout'] = candidate", "changed['stdout'] = candidate\n    changed['stderr'] = ''", 'non-stdout response fields changed'),
    ('audit', 'pip_renderer', 'tokenguard/hook.py',
     "'original_chars': len(original)", "'original_chars': len(original) + 1", 'compression audit missing or invalid'),
])
def test_actual_pipeline_rejects_injected_regression(api, tmp_path, mutation, case_name, relative, old, new, expected_error):
    run = api['run']
    reference = tmp_path / mutation
    shutil.copytree(SCRIPT.parents[1] / 'tokenguard', reference / 'tokenguard',
                    ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copyfile(SCRIPT.parents[1] / 'tg.py', reference / 'tg.py')
    target = reference / relative
    source = target.read_text(encoding='utf-8')
    assert source.count(old) == 1
    target.write_text(source.replace(old, new), encoding='utf-8')
    selected = tuple(case for case in api['cases']() if case.name == case_name)
    with patch.dict(run.__globals__, {'cases': lambda: selected}):
        folder = run(tmp_path / 'reports', reference)
    report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    assert report['passed'] is False
    assert len(report['rows']) == report['expected_checks'] == 6
    assert report['successful_checks'] == 3  # Disabled arm retains the original.
    assert report['source_unchanged_during_run'] is True
    for row in report['rows']:
        if row['enabled']:
            assert any(expected_error in error for error in row['errors'])


def test_empty_fixture_plan_cannot_report_success(api, tmp_path):
    run = api['run']
    with patch.dict(run.__globals__, {'cases': lambda: ()}):
        folder = run(tmp_path)
    report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    assert report['passed'] is False
    assert report['expected_checks'] == report['successful_checks'] == 0
