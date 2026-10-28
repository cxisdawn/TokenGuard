"""Independent recovery contracts must detect incomplete or misleading evidence."""
import json
from pathlib import Path
import runpy
import subprocess
from unittest.mock import patch

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'benchmark' / 'recovery_contracts.py'


@pytest.fixture
def api():
    assert SCRIPT.is_file(), 'Standalone recovery contracts are not implemented'
    return runpy.run_path(str(SCRIPT))


def test_context_loss_negative_control_is_not_certified(api):
    case = api['Case']('lost_context', 'grep', 'NEEDLE\nNEEDLE\n', pattern='needle')
    errors = api['content_errors'](case, ['1: NEEDLE\n'], [(1, 0)])
    assert 'matching lines lost: 2' in errors


def test_show_oracle_rejects_same_length_changed_text(api):
    case = api['Case']('changed_show', 'show', 'original\r\n')
    assert 'show differs from original characters' in api['content_errors'](
        case, ['changed!\r\n'], [(1, 0)])


def test_show_oracle_preserves_literal_no_match_status(api):
    case = api['Case']('literal_status', 'show', '(no matches)\n')
    assert api['content_errors'](case, ['(no matches)\n'], [(1, 0)]) == []


def test_marker_removal_uses_exact_suffix_not_first_occurrence(api):
    literal = '\n[TokenGuard: retrieval limit reached; next_start_line=999; next_start_char=3]\n'
    actual = '\n[TokenGuard: retrieval limit reached; next_start_line=2; next_start_char=4]\n'
    assert api['page_body']('before' + literal + 'after' + actual, True, (2, 4)) == 'before' + literal + 'after'


def test_missing_required_marker_is_rejected(api):
    with pytest.raises(ValueError, match='marker'):
        api['page_body']('ordinary data', True, (2, 0))


def test_wrong_line_payload_cannot_count_as_recovered_match(api):
    case = api['Case']('forged_line', 'grep', 'NEEDLE correct\n', pattern='needle')
    errors = api['content_errors'](case, ['1: NEEDLE changed\n'], [(1, 0)])
    assert any('source text differs' in error for error in errors)


def test_window_oracle_accepts_original_unicode_character_offset(api):
    case = api['Case']('unicode_window', 'grep', '😀😀Straße\r\n', pattern='STRASSE')
    assert api['content_errors'](case, ['1: [char 2] Straße\r\n'], [(1, 0)]) == []


@pytest.mark.parametrize('failure', ['timeout', 'invalid_json', 'missing_rows'])
def test_worker_failures_keep_full_denominator_and_export_reports(api, tmp_path, failure):
    run = api['run']
    first = api['Case']('first', 'show', 'one\n')
    second = api['Case']('second', 'grep', 'NEEDLE\n', pattern='needle')
    result = (subprocess.TimeoutExpired('worker', 120) if failure == 'timeout' else
              subprocess.CompletedProcess('worker', 0, b'bad' if failure == 'invalid_json' else b'{"rows":[]}', b''))
    kwargs = {'side_effect': result} if isinstance(result, Exception) else {'return_value': result}
    with patch.dict(run.__globals__, {'cases': lambda: (first, second)}), \
            patch.object(subprocess, 'run', **kwargs):
        folder = run(tmp_path)
    report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    assert report['passed'] is False
    assert report['expected_checks'] == 2
    assert len(report['rows']) == 2
    assert all(row['passed'] is False for row in report['rows'])
    assert (folder / 'report.csv').is_file() and (folder / 'report.md').is_file()


def test_actual_worker_fixed_context_case_and_show_reconstruct(api, tmp_path):
    run = api['run']
    all_cases = api['cases']()
    selected = tuple(case for case in all_cases if case.name in ('context_cut', 'show_controls'))
    assert len(selected) == 2
    with patch.dict(run.__globals__, {'cases': lambda: selected}):
        folder = run(tmp_path)
    report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    assert report['passed'] is True
    assert report['expected_checks'] == 2 and len(report['rows']) == 2
    assert report['source_unchanged_during_run'] is True
    assert report['model_api_calls'] == 0
    assert report['actual_billed_cost'] == 'unavailable'


@pytest.mark.parametrize('ending', ['\n', '\r', '\r\n'])
@pytest.mark.parametrize('budget', [90, 91])
def test_grep_page_never_ends_in_a_partial_context_line_label(tmp_path, monkeypatch, ending, budget):
    from tokenguard.audit import events
    from tokenguard.storage import grep_lines, save_original
    monkeypatch.setenv('TOKENGUARD_DATA_DIR', str(tmp_path / 'data'))
    identifier = save_original('NEEDLE' + ending + 'x' * 500 + ending)
    result = grep_lines(identifier, 'needle', limit=1, context=1, max_chars=budget)
    row = events()[0][-1]
    marker = (f"\n[TokenGuard: retrieval limit reached; next_start_line={row['next_start_line']}; "
              f"next_start_char={row['next_start_char']}]\n")
    assert result.endswith(marker)
    assert result[:-len(marker)] == '1: NEEDLE' + ending
    assert (row['next_start_line'], row['next_start_char']) == (2, 0)


@pytest.mark.parametrize('worker_rows', [None, {}, 'invalid'])
def test_wrong_worker_rows_type_becomes_failed_report(api, tmp_path, worker_rows):
    run = api['run']
    case = api['Case']('first', 'show', 'one\n')
    response = subprocess.CompletedProcess('worker', 0, json.dumps({'rows': worker_rows}).encode(), b'')
    with patch.dict(run.__globals__, {'cases': lambda: (case,)}), patch.object(subprocess, 'run', return_value=response):
        folder = run(tmp_path)
    report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    assert report['passed'] is False
    assert report['expected_checks'] == 1 and len(report['rows']) == 1
    assert report['rows'][0]['passed'] is False


@pytest.mark.parametrize('change', [{'completed': False}, {'pages': True}, {'returned_chars': -1},
                                    {'max_returned_chars': 99999}, {'errors': ['failure']},
                                    {'artifact_byte_identical': False}, {'latency_ms': float('nan')},
                                    {'latency_ms': 10 ** 400}])
def test_inconsistent_worker_success_cannot_be_certified(api, tmp_path, change):
    run = api['run']
    case = api['Case']('first', 'show', 'one\n')
    row = {**api['fixture'](case), 'passed': True, 'completed': True, 'pages': 1,
           'returned_chars': 4, 'max_returned_chars': 4, 'scanned_chars': 4,
           'artifact_byte_identical': True, 'latency_ms': 1.0, 'errors': [], **change}
    response = subprocess.CompletedProcess('worker', 0, json.dumps({'rows': [row]}).encode(), b'')
    with patch.dict(run.__globals__, {'cases': lambda: (case,)}), patch.object(subprocess, 'run', return_value=response):
        folder = run(tmp_path)
    report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    assert report['passed'] is False
    assert report['successful_checks'] == 0


def test_added_source_during_worker_prevents_certification(api, tmp_path):
    run = api['run']
    case = api['Case']('first', 'show', 'one\n')
    implementation = tmp_path / 'implementation'
    package = implementation / 'tokenguard'
    package.mkdir(parents=True)
    for path in (package / '__init__.py', package / 'storage.py', implementation / 'tg.py'):
        path.write_text('# fixed fixture\n', encoding='utf-8')
    row = {**api['fixture'](case), 'passed': True, 'completed': True, 'pages': 1,
           'returned_chars': 4, 'max_returned_chars': 4, 'scanned_chars': 4,
           'artifact_byte_identical': True, 'latency_ms': 1.0, 'errors': []}

    def changed(*args, **kwargs):
        (package / 'added.py').write_text('# added during execution\n', encoding='utf-8')
        return subprocess.CompletedProcess('worker', 0, json.dumps({'rows': [row]}).encode(), b'')

    with patch.dict(run.__globals__, {'cases': lambda: (case,)}), patch.object(subprocess, 'run', side_effect=changed):
        folder = run(tmp_path / 'reports', implementation)
    report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    assert report['passed'] is False
    assert report['source_unchanged_during_run'] is False
    assert report['expected_checks'] == 1
