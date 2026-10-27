"""Evidence integrity checks using local synthetic exports only."""
import copy
import json
from pathlib import Path

import pytest

from tokenguard.usage import import_usage, normalize
from tokenguard.usage_compare import compare


FIXTURES = Path(__file__).resolve().parent.parent / 'benchmark' / 'usage_fixtures'


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding='utf-8'))


def import_fixture(tmp_path, *, arm='baseline', rows=None, manifest=None, bill=None):
    def write(name, value):
        path = tmp_path / name
        path.write_text(json.dumps(value), encoding='utf-8')
        return path

    return import_usage(
        write('responses.json', rows if rows is not None else fixture(arm + '-responses.json')),
        write('manifest.json', manifest if manifest is not None else fixture(arm + '-manifest.json')),
        'deepseek', write('billing.json', bill) if bill is not None else None,
    )


def early(request_id='unresolved-request', **changes):
    return {'object': 'chat.completion.chunk', 'id': request_id, 'model': 'deepseek-flash',
            'usage': None, 'choices': [{'finish_reason': None}], **changes}


@pytest.mark.parametrize('position', ['first', 'last'])
def test_complete_export_rejects_observed_request_without_terminal_usage(tmp_path, position):
    rows = fixture('baseline-responses.json')
    rows.insert(0 if position == 'first' else len(rows), early())
    with pytest.raises(ValueError, match='finalized usage'):
        import_fixture(tmp_path, rows=rows)


@pytest.mark.parametrize('field,value', [
    ('id', None), ('id', True), ('id', 12), ('id', {}), ('id', []), ('id', ''),
    ('id', 'x\ny'), ('id', 'x' * 201), ('model', None), ('model', True),
    ('model', {}), ('model', []), ('model', ''), ('model', 'other-model'),
])
def test_nonfinal_chunks_require_valid_request_identity(field, value):
    with pytest.raises(ValueError):
        normalize(early(**{field: value}), 'deepseek', 'deepseek-flash')


def test_partial_export_retains_unresolved_request_evidence(tmp_path):
    rows = [early(), fixture('baseline-responses.json')[0], early()]
    manifest = {**fixture('baseline-manifest.json'), 'coverage': 'partial',
                'expected_request_count': None}
    result = import_fixture(tmp_path, rows=rows, manifest=manifest)
    assert result['imported_request_totals']['total_tokens'] == 1100
    assert result['complete_task_totals'] == 'unavailable'
    assert result['request_coverage'] == {
        'observed_requests': 2, 'finalized_requests': 1,
        'unresolved_request_ids': ['unresolved-request'],
        'all_observed_requests_finalized': False,
    }


def test_partial_export_cannot_be_relabelled_complete_with_unresolved_requests(tmp_path):
    baseline = import_fixture(tmp_path)
    manifest = {**fixture('optimized-manifest.json'), 'coverage': 'partial',
                'expected_request_count': None}
    optimized = import_fixture(tmp_path, arm='optimized', manifest=manifest,
                               rows=[*fixture('optimized-responses.json'), early()])
    optimized['manifest']['coverage'] = 'complete_task'
    optimized['manifest']['expected_request_count'] = optimized['unique_requests']
    optimized['complete_task_totals'] = optimized['imported_request_totals']
    with pytest.raises(ValueError, match='finalized usage'):
        compare([baseline, optimized])


def test_interleaved_streams_finalize_each_request_and_deduplicate_usage(tmp_path):
    finals = fixture('baseline-responses.json')
    for row in finals:
        row.update(object='chat.completion.chunk', choices=[{'finish_reason': 'stop'}])
    rows = [early(finals[0]['id']), early(finals[1]['id']), finals[1], finals[0],
            copy.deepcopy(finals[0])]
    result = import_fixture(tmp_path, rows=rows)
    assert result['imported_request_totals']['total_tokens'] == 3300
    assert result['duplicate_records_ignored'] == 1
    assert result['nonfinal_chunks_ignored'] == 2
    assert result['request_coverage']['all_observed_requests_finalized'] is True
    assert result['request_coverage']['unresolved_request_ids'] == []


def test_billing_binding_is_preserved_and_rechecked_at_comparison(tmp_path):
    bill = fixture('baseline-billing.json')
    baseline = import_fixture(tmp_path, bill=bill)
    normalized = baseline['reported_billing']
    assert normalized['schema_version'] == 1
    assert normalized['scope'] == 'session_total'
    assert normalized['session_id'] == bill['session_id']
    assert normalized['request_ids'] == sorted(bill['request_ids'])
    optimized = import_fixture(tmp_path, arm='optimized', bill=fixture('optimized-billing.json'))
    assert compare([baseline, optimized])['pairs'][0]['reported_billing_change']['reduction_ratio'] == -1
    optimized['reported_billing'] = copy.deepcopy(normalized)
    with pytest.raises(ValueError, match='billing.*(session|request)'):
        compare([baseline, optimized])


@pytest.mark.parametrize('version', [True, 1.0, '1', None, False, 0, 2])
def test_billing_schema_requires_exact_integer_version(tmp_path, version):
    bill = {**fixture('baseline-billing.json'), 'schema_version': version}
    with pytest.raises(ValueError, match='versioned'):
        import_fixture(tmp_path, bill=bill)


@pytest.mark.parametrize('field,value', [
    ('schema_version', True), ('schema_version', 1.0), ('scope', 'project_total'),
    ('session_id', 'unrelated-session'), ('request_ids', []),
    ('request_ids', ['synthetic-optimized-r1', 'synthetic-optimized-r1']),
    ('request_ids', [None]),
])
def test_tampered_normalized_billing_binding_is_rejected(tmp_path, field, value):
    baseline = import_fixture(tmp_path)
    optimized = import_fixture(tmp_path, arm='optimized', bill=fixture('optimized-billing.json'))
    optimized['reported_billing'][field] = value
    with pytest.raises(ValueError):
        compare([baseline, optimized])


def test_legacy_unbound_billing_is_unavailable_without_losing_usage(tmp_path):
    bundles = []
    for arm in ('baseline', 'optimized'):
        bundle = import_fixture(tmp_path, arm=arm, bill=fixture(arm + '-billing.json'))
        for field in ('schema_version', 'scope', 'session_id', 'request_ids'):
            bundle['reported_billing'].pop(field, None)
        bundles.append(bundle)
    report = compare(bundles)
    pair = report['pairs'][0]
    assert pair['metrics']['total_tokens']['baseline'] == 3300
    assert pair['reported_billing_change'] == 'unavailable'
    assert report['cohorts'][0]['reported_billing_change'] == 'unavailable'
    assert 'baseline_billing_binding_unavailable' in pair['issues']
    assert 'optimized_billing_binding_unavailable' in pair['issues']


@pytest.mark.parametrize('field', ['scope', 'session_id', 'request_ids', 'schema_version'])
def test_partially_removed_billing_binding_is_rejected(tmp_path, field):
    baseline = import_fixture(tmp_path)
    optimized = import_fixture(tmp_path, arm='optimized', bill=fixture('optimized-billing.json'))
    optimized['reported_billing'].pop(field, None)
    with pytest.raises(ValueError):
        compare([baseline, optimized])


def test_unknown_billing_remains_unavailable(tmp_path):
    baseline = import_fixture(tmp_path)
    optimized = import_fixture(tmp_path, arm='optimized')
    assert compare([baseline, optimized])['pairs'][0]['reported_billing_change'] == 'unavailable'


@pytest.mark.parametrize('field,value', [
    ('observed_requests', True), ('observed_requests', 2.0), ('observed_requests', -1),
    ('observed_requests', 3), ('finalized_requests', True), ('finalized_requests', 1),
    ('all_observed_requests_finalized', 1), ('all_observed_requests_finalized', False),
    ('unresolved_request_ids', None), ('unresolved_request_ids', [None]),
    ('unresolved_request_ids', ['same', 'same']),
    ('unresolved_request_ids', ['synthetic-optimized-r1']),
])
def test_normalized_request_coverage_is_revalidated(tmp_path, field, value):
    baseline = import_fixture(tmp_path)
    optimized = import_fixture(tmp_path, arm='optimized')
    optimized['request_coverage'][field] = value
    with pytest.raises(ValueError):
        compare([baseline, optimized])


@pytest.mark.parametrize('field,value', [
    ('unique_requests', 2.0), ('duplicate_records_ignored', True),
    ('nonfinal_chunks_ignored', -1), ('nonfinal_chunks_ignored', False),
])
def test_normalized_request_counters_require_exact_integers(tmp_path, field, value):
    baseline = import_fixture(tmp_path)
    optimized = import_fixture(tmp_path, arm='optimized')
    optimized[field] = value
    with pytest.raises(ValueError):
        compare([baseline, optimized])


def test_legacy_streaming_bundle_requires_reimport_for_missing_coverage(tmp_path):
    baseline = import_fixture(tmp_path)
    rows = fixture('optimized-responses.json')
    optimized = import_fixture(tmp_path, arm='optimized', rows=[early(rows[0]['id']), *rows])
    optimized.pop('request_coverage')
    with pytest.raises(ValueError, match='reimport'):
        compare([baseline, optimized])


def test_legacy_nonstreaming_usage_remains_comparable(tmp_path):
    bundles = [import_fixture(tmp_path, arm=arm) for arm in ('baseline', 'optimized')]
    for bundle in bundles:
        bundle.pop('request_coverage')
    result = compare(bundles)
    assert result['eligible_pairs'] == 1
    assert result['pairs'][0]['reported_billing_change'] == 'unavailable'


def test_legacy_missing_stream_count_is_unknown_not_zero(tmp_path):
    baseline = import_fixture(tmp_path)
    optimized = import_fixture(tmp_path, arm='optimized')
    optimized.pop('request_coverage')
    optimized.pop('nonfinal_chunks_ignored')
    with pytest.raises(ValueError, match='coverage evidence'):
        compare([baseline, optimized])


def test_partial_unresolved_export_does_not_gain_task_comparison(tmp_path):
    baseline = import_fixture(tmp_path)
    manifest = {**fixture('optimized-manifest.json'), 'coverage': 'partial'}
    optimized = import_fixture(tmp_path, arm='optimized', manifest=manifest,
                               rows=[*fixture('optimized-responses.json'), early()])
    pair = compare([baseline, optimized])['pairs'][0]
    assert pair['status'] == 'partial_usage'
    assert pair['metrics'] == 'unavailable'
    assert pair['reported_billing_change'] == 'unavailable'


def test_export_without_any_final_usage_never_reports_zero(tmp_path):
    manifest = {**fixture('baseline-manifest.json'), 'coverage': 'partial'}
    with pytest.raises(ValueError, match='No finalized usage'):
        import_fixture(tmp_path, manifest=manifest, rows=[early()])


def test_late_model_mismatch_is_not_hidden_by_prior_final_usage(tmp_path):
    rows = fixture('baseline-responses.json')
    with pytest.raises(ValueError, match='model'):
        import_fixture(tmp_path, rows=[*rows, early(rows[0]['id'], model='different')])
