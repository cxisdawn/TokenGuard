"""Recovery I/O measurements and cursors remain strict, optional audit evidence."""
import json

import pytest

from tokenguard import audit


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv('TOKENGUARD_DATA_DIR', str(tmp_path))
    return tmp_path


def recovery(**changes):
    return {'event': 'recovery', 'returned_chars': 9, **changes}


def write_rows(folder, *rows):
    path = folder / 'audit-v04.jsonl'
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
    return path


def test_integrity_bytes_sum_only_complete_selected_recoveries(data):
    write_rows(data, recovery(integrity_status='built', integrity_build_bytes=100,
                              integrity_checked_bytes=40),
               recovery(integrity_status='reused', integrity_build_bytes=0,
                        integrity_checked_bytes=20),
               recovery(integrity_status='uncached', integrity_build_bytes=200,
                        integrity_checked_bytes=5))
    result = audit.summary()
    assert result['data_available'] is True
    assert result['recovery_count'] == 3
    assert result['integrity_build_bytes'] == 300
    assert result['integrity_checked_bytes'] == 65
    for key in ('integrity_status', 'integrity_build_bytes', 'integrity_checked_bytes'):
        assert result['measurement_coverage'][key] == {'observed_records': 3, 'total_records': 3}


@pytest.mark.parametrize('missing', ['integrity_status', 'integrity_build_bytes', 'integrity_checked_bytes'])
def test_missing_integrity_measurement_stays_unknown_with_coverage(data, missing):
    complete = recovery(integrity_status='built', integrity_build_bytes=100, integrity_checked_bytes=40)
    partial = dict(complete)
    del partial[missing]
    original_path = write_rows(data, complete, partial)
    original_bytes = original_path.read_bytes()
    result = audit.summary()
    assert result['data_available'] is True
    assert result['measurement_coverage'][missing] == {'observed_records': 1, 'total_records': 2}
    for key, value in (('integrity_build_bytes', 200), ('integrity_checked_bytes', 80)):
        assert result[key] == (None if key == missing else value)
    assert original_path.read_bytes() == original_bytes


def test_legacy_recovery_has_unknown_integrity_measurements(data):
    write_rows(data, recovery())
    result = audit.summary()
    assert result['data_available'] is True
    assert result['integrity_build_bytes'] is None
    assert result['integrity_checked_bytes'] is None
    for key in ('integrity_status', 'integrity_build_bytes', 'integrity_checked_bytes'):
        assert result['measurement_coverage'][key] == {'observed_records': 0, 'total_records': 1}


def test_no_recoveries_is_measured_zero(data):
    result = audit.summary()
    assert result['integrity_build_bytes'] == 0
    assert result['integrity_checked_bytes'] == 0
    for key in ('integrity_status', 'integrity_build_bytes', 'integrity_checked_bytes'):
        assert result['measurement_coverage'][key] == {'observed_records': 0, 'total_records': 0}


@pytest.mark.parametrize('field', ['integrity_build_bytes', 'integrity_checked_bytes'])
@pytest.mark.parametrize('value', [True, False, -1, 1.5, '10', None, 2**63])
def test_invalid_integrity_counts_are_quarantined(data, field, value):
    write_rows(data, recovery(), recovery(**{field: value}))
    result = audit.summary()
    assert result['recovery_count'] == 1
    assert result['returned_chars'] == 9
    assert result['invalid_rows'] == 1
    assert result['data_available'] is False


@pytest.mark.parametrize('value', ['unknown', '', None, True, 1, ['built'], {'status': 'built'}])
def test_invalid_integrity_status_is_quarantined(data, value):
    write_rows(data, recovery(), recovery(integrity_status=value))
    result = audit.summary()
    assert result['recovery_count'] == 1
    assert result['invalid_rows'] == 1
    assert result['data_available'] is False


@pytest.mark.parametrize('field', ['requested_start_line', 'requested_end_line', 'next_start_line'])
@pytest.mark.parametrize('value', [True, 0, -1, 1.5, '10', 2**63])
def test_invalid_line_cursors_are_quarantined(data, field, value):
    write_rows(data, recovery(), recovery(**{field: value}))
    result = audit.summary()
    assert result['recovery_count'] == 1
    assert result['invalid_rows'] == 1
    assert result['data_available'] is False


@pytest.mark.parametrize('field', ['requested_start_char', 'next_start_char'])
@pytest.mark.parametrize('value', [True, False, -1, 1.5, '10', 2**63])
def test_invalid_character_cursors_are_quarantined(data, field, value):
    write_rows(data, recovery(), recovery(**{field: value}))
    result = audit.summary()
    assert result['recovery_count'] == 1
    assert result['invalid_rows'] == 1
    assert result['data_available'] is False


@pytest.mark.parametrize('field', ['requested_start_line', 'requested_start_char'])
def test_requested_start_cannot_be_null_when_present(data, field):
    write_rows(data, recovery(**{field: None}))
    result = audit.summary()
    assert result['recovery_count'] == 0
    assert result['invalid_rows'] == 1


@pytest.mark.parametrize('changes', [
    {},
    {'requested_start_line': 1, 'requested_end_line': None, 'requested_start_char': 0,
     'next_start_line': None, 'next_start_char': None},
    {'requested_start_line': 1, 'requested_end_line': 10, 'requested_start_char': 0,
     'next_start_line': 2, 'next_start_char': 0},
    {'requested_start_line': 2**63-1, 'requested_end_line': 2**63-1,
     'requested_start_char': 2**63-1, 'next_start_line': 2**63-1, 'next_start_char': 2**63-1,
     'integrity_build_bytes': 2**63-1, 'integrity_checked_bytes': 2**63-1},
])
def test_valid_optional_cursor_boundaries_preserve_legacy_rows(data, changes):
    original_path = write_rows(data, recovery(**changes))
    original_bytes = original_path.read_bytes()
    result = audit.summary()
    assert result['data_available'] is True
    assert result['recovery_count'] == 1
    assert result['invalid_rows'] == 0
    assert original_path.read_bytes() == original_bytes


def test_integrity_measurements_follow_session_selection(data):
    compressions = [{'event': 'compression', 'compression_id': name, 'session_id': name,
                     'original_chars': 100, 'optimized_chars': 10} for name in ('one', 'two')]
    write_rows(data, *compressions,
               recovery(compression_id='one', integrity_status='built',
                        integrity_build_bytes=100, integrity_checked_bytes=20),
               recovery(compression_id='two'))
    result = audit.summary(session_id='one')
    assert result['data_available'] is True
    assert result['integrity_build_bytes'] == 100
    assert result['integrity_checked_bytes'] == 20
    assert result['measurement_coverage']['integrity_build_bytes'] == {'observed_records': 1, 'total_records': 1}
    assert audit.summary()['integrity_build_bytes'] is None
