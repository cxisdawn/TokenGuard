"""Audit damage must not fabricate metrics or block intact artifact reads."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tokenguard import audit
from tokenguard.storage import read_lines, save_original


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv('TOKENGUARD_DATA_DIR', str(tmp_path))
    return tmp_path


def compression(**changes):
    return {'event': 'compression', 'compression_id': 'c1', 'session_id': 's1',
            'experiment_id': 'e1', 'source': 'manual', 'artifact_id': 'a'*64,
            'original_chars': 1000, 'optimized_chars': 100,
            'original_token_estimate': {'estimated_tokens': 250},
            'optimized_token_estimate': {'estimated_tokens': 25}, **changes}


def write_rows(folder, *rows):
    (folder/'audit-v04.jsonl').write_bytes(b''.join(
        row if isinstance(row, bytes) else (json.dumps(row)+'\n').encode() for row in rows))


def test_invalid_utf8_truncated_and_oversized_lines_preserve_valid_rows(data):
    write_rows(data, b'\xff\n', b'x'*(300*1024)+b'\n', compression(), b'{"event":')
    result = audit.summary()
    assert result['compressions'] == 1
    assert result['malformed_rows'] == 3
    assert result['data_available'] is False


@pytest.mark.parametrize('field,value', [('original_chars', True), ('optimized_chars', -1),
    ('compression_latency_ms', float('nan')), ('original_token_estimate', []),
    ('original_token_estimate', {'estimated_tokens': float('inf')}),
    ('original_chars', '1000'), ('original_chars', 10**400),
    ('compression_latency_ms', 10**400), ('compression_latency_ms', 1e308),
    ('compression_id', ['bad']), ('source', {'name': 'manual'}), ('session_id', 's\n1')])
def test_invalid_metrics_are_quarantined(data, field, value):
    changes = {'compression_id': 'bad', field: value}
    write_rows(data, compression(), compression(**changes))
    result = audit.summary()
    assert result['compressions'] == 1
    assert result['original_chars'] == 1000
    assert result['invalid_rows']+result['malformed_rows'] == 1
    assert result['data_available'] is False


def test_duplicate_compression_is_counted_once_and_conflicts_are_excluded(data):
    write_rows(data, compression(), compression(),
               compression(compression_id='conflict'),
               compression(compression_id='conflict', session_id='other'))
    result = audit.summary()
    assert result['compressions'] == 1
    assert result['duplicate_rows'] == 1
    assert result['conflicting_rows'] == 2
    assert result['data_available'] is False


def test_missing_estimates_stay_null_with_coverage(data):
    row = compression()
    del row['original_token_estimate']
    write_rows(data, row)
    result = audit.summary()
    assert result['estimated_original_tokens'] is None
    assert result['estimated_token_reduction_ratio'] is None
    assert result['token_estimate_coverage']['complete_records'] == 0
    assert result['token_estimate_coverage']['total_records'] == 1


def test_recovery_does_not_join_null_or_mismatched_context(data):
    write_rows(data, compression(), {'event': 'recovery', 'compression_id': None,
        'returned_chars': 5}, {'event': 'recovery', 'compression_id': 'c1',
        'session_id': 'other', 'experiment_id': 'e1', 'artifact_id': 'a'*64,
        'returned_chars': 20})
    result = audit.summary('s1')
    assert result['records'][0]['recovery_count'] == 0
    assert result['recovery_count'] == 0
    assert result['data_available'] is False


def test_invalid_utf8_does_not_block_unattributed_recovery(data):
    identifier = save_original('important\n')
    write_rows(data, b'\xff\n')
    assert read_lines(identifier, 1, 1) == 'important\n'
    assert audit.events()[0][-1]['attribution'] == 'unattributed'


def test_duplicate_owner_is_safe_but_conflicted_explicit_owner_rejected(data):
    identifier = save_original('important\n')
    row = compression(artifact_id=identifier)
    write_rows(data, row, row)
    assert read_lines(identifier, 1, 1, compression_id='c1') == 'important\n'
    audit.append(compression(artifact_id=identifier, session_id='other'))
    with pytest.raises(ValueError):
        read_lines(identifier, 1, 1, compression_id='c1')
    assert read_lines(identifier, 1, 1) == 'important\n'


def test_new_record_after_truncated_tail_preserves_old_bytes(data):
    original = b'{"event":"broken"'
    write_rows(data, original)
    audit.append(compression())
    assert (data/'audit-v04.jsonl').read_bytes().startswith(original+b'\n')
    rows, malformed = audit.events()
    assert len(rows) == 1 and malformed == 1


def test_real_concurrent_append_after_truncated_tail(data):
    write_rows(data, b'{broken')
    code = ('from tokenguard.audit import append; import sys; '
            '[append({"event":"probe", "worker":sys.argv[1], "n":i}) for i in range(30)]')
    workers = [subprocess.Popen([sys.executable, '-X', 'utf8', '-c', code, str(i)],
                cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=os.environ.copy()) for i in range(4)]
    for process in workers:
        _, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr.decode()
    rows, malformed = audit.events()
    assert malformed == 1
    assert len(rows) == len({(row['worker'], row['n']) for row in rows}) == 120


def test_record_limit_does_not_change_full_aggregate(data):
    write_rows(data, *(compression(compression_id=str(i)) for i in range(5)))
    result = audit.summary(record_limit=2)
    assert result['compressions'] == result['records_total'] == 5
    assert len(result['records']) == 2 and result['records_omitted'] == 3
    assert result['original_chars'] == 5000


def test_delivery_requires_valid_unambiguous_receipt(data):
    write_rows(data, compression(), compression(compression_id='c2'),
               {'event': 'replacement_delivery', 'compression_id': 'c1', 'session_id': 's1',
                'experiment_id': 'e1', 'source': 'manual', 'status': 'stdout_write_succeeded'})
    result = audit.summary()
    assert result['prepared_candidates'] == 2
    assert result['confirmed_stdout_deliveries'] == 1
    assert result['delivery_unknown'] == 1
    audit.append({'event': 'replacement_delivery', 'compression_id': 'c1', 'session_id': 's1',
                'experiment_id': 'e1', 'source': 'manual', 'status': 'stdout_write_failed'})
    result = audit.summary()
    assert result['confirmed_stdout_deliveries'] == 0
    assert result['delivery_unknown'] == 2
    assert result['conflicting_rows'] == 2
    assert result['data_available'] is False


def test_bom_and_final_valid_record_without_newline_are_readable(data):
    raw = json.dumps(compression()).encode()
    write_rows(data, b'\xef\xbb\xbf'+raw)
    assert audit.events() == ([compression()], 0)
    before = (data/'audit-v04.jsonl').read_bytes()
    audit.append(compression(compression_id='c2'))
    assert (data/'audit-v04.jsonl').read_bytes().startswith(before+b'\n')
    assert audit.summary()['compressions'] == 2


@pytest.mark.parametrize('field,value', [('status', []), ('source', {}),
                                        ('tool_use_id', 'x\x1b[31m'), ('session_id', 'x'*257)])
def test_diagnostics_rejects_malformed_metadata_but_recovers_good_rows(data, field, value):
    row = {'event': 'hook_invocation', 'status': 'below_threshold', field: value}
    (data/'diagnostics-v04.jsonl').write_bytes(b'\xff\n'+(json.dumps(row)+'\n'+
        json.dumps({'event': 'hook_invocation', 'status': 'compressed'})+'\n').encode())
    result = audit.diagnostics()
    assert result['malformed_rows'] == result['invalid_rows'] == 1
    assert result['matching_invocations'] == 1 and result['data_available'] is False


def test_no_compression_id_cannot_claim_a_session(data):
    write_rows(data, compression(), {'event': 'recovery', 'session_id': 's1', 'returned_chars': 20})
    result = audit.summary('s1')
    assert result['recovery_count'] == result['records'][0]['recovery_count'] == 0
    assert result['invalid_rows'] == 1


def test_recovery_duplicate_identity_and_cross_artifact_receipt(data):
    row = {'event': 'recovery', 'recovery_id': 'r1', 'compression_id': 'c1',
           'session_id': 's1', 'experiment_id': 'e1', 'artifact_id': 'a'*64, 'returned_chars': 20}
    write_rows(data, compression(), row, row, {**row, 'recovery_id': 'r2', 'artifact_id': 'b'*64})
    result = audit.summary('s1')
    assert result['recovery_count'] == 1 and result['returned_chars'] == 20
    assert result['duplicate_rows'] == result['invalid_rows'] == 1


def test_missing_delivery_context_is_unknown(data):
    write_rows(data, compression(), {'event': 'replacement_delivery', 'compression_id': 'c1',
                                   'status': 'stdout_write_succeeded'})
    result = audit.summary()
    assert result['confirmed_stdout_deliveries'] == 0
    assert result['delivery_unknown'] == 1 and result['data_available'] is False


def test_invalid_and_valid_occurrences_of_one_id_cannot_select_good_one(data):
    identifier = save_original('intact\n')
    row = compression(artifact_id=identifier)
    write_rows(data, row, {**row, 'optimized_chars': None})
    with pytest.raises(ValueError):
        read_lines(identifier, 1, 1, compression_id='c1')
    assert read_lines(identifier, 1, 1) == 'intact\n'
    result = audit.summary()
    assert result['compressions'] == 0 and result['conflicting_rows'] == 2


def test_unrequested_owner_is_not_guessed_even_with_one_candidate(data):
    identifier = save_original('intact\n')
    write_rows(data, compression(artifact_id=identifier))
    assert read_lines(identifier, 1, 1) == 'intact\n'
    assert audit.events()[0][-1]['compression_id'] is None
    assert audit.summary('s1')['recovery_count'] == 0
    assert audit.summary()['recovery_count'] == 1


def test_recovery_explicit_experiment_must_agree(data):
    identifier = save_original('intact\n')
    write_rows(data, compression(artifact_id=identifier))
    with pytest.raises(ValueError):
        read_lines(identifier, 1, 1, compression_id='c1', experiment_id='wrong')
    assert read_lines(identifier, 1, 1, compression_id='c1', experiment_id='e1') == 'intact\n'


def test_partial_os_writes_complete_and_zero_write_fails(data, monkeypatch):
    original_write = audit.os.write
    monkeypatch.setattr(audit.os, 'write', lambda fd, content: original_write(fd, content[:3]))
    audit.append(compression())
    assert audit.events()[0][0]['compression_id'] == 'c1'
    before = (data/'audit-v04.jsonl').read_bytes()
    monkeypatch.setattr(audit.os, 'write', lambda fd, content: 0)
    with pytest.raises(OSError, match='incomplete audit write'):
        audit.append(compression(compression_id='c2'))
    assert (data/'audit-v04.jsonl').read_bytes() == before


def test_append_os_lock_times_out_and_recovers_after_holder_exit(data, monkeypatch):
    ready = data/'ready'
    code = ('from pathlib import Path; import sys, time; from tokenguard.audit import _append_lock; '
            'exec("with _append_lock(Path(sys.argv[1])):\\n Path(sys.argv[2]).touch()\\n time.sleep(20)")')
    holder = subprocess.Popen([sys.executable, '-c', code, str(data/'audit-v04.jsonl.lock'), str(ready)],
                              cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        import time
        deadline = time.monotonic()+10
        while not ready.exists() and holder.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        monkeypatch.setattr(audit, 'LOCK_TIMEOUT_SECONDS', 0.05)
        with pytest.raises(TimeoutError):
            audit.append(compression())
        assert not (data/'audit-v04.jsonl').exists()
    finally:
        holder.terminate()
        holder.communicate(timeout=10)
    audit.append(compression())
    assert len(audit.events()[0]) == 1


def test_reject_oversized_self_written_record_without_damaging_existing_data(data):
    audit.append(compression())
    before = (data/'audit-v04.jsonl').read_bytes()
    with pytest.raises(ValueError, match='bounded reader'):
        audit.append({'event': 'probe', 'text': 'x'*audit.MAX_LINE_BYTES})
    assert (data/'audit-v04.jsonl').read_bytes() == before


def test_missing_optional_recovery_measurements_are_not_zero(data):
    write_rows(data, compression(), {'event': 'recovery', 'compression_id': 'c1', 'returned_chars': 5})
    result = audit.summary()
    assert result['data_available'] is True
    assert result['recovery_count'] == 1 and result['returned_chars'] == 5
    assert result['scanned_chars'] is None and result['recovery_latency_ms'] is None
    assert result['index_build_bytes'] is None and result['indexed_recoveries'] is None
    assert result['measurement_coverage']['scanned_chars'] == {'observed_records': 0, 'total_records': 1}
    write_rows(data, compression())
    result = audit.summary()
    assert result['scanned_chars'] == result['recovery_latency_ms'] == result['index_build_bytes'] == 0


def test_source_only_filter_reports_filtered_scope(data):
    write_rows(data, compression())
    assert audit.summary(source='manual')['scope'] == 'filtered'


@pytest.mark.parametrize('value', [-1, True, float('nan'), 10**400])
def test_diagnostic_character_counts_are_validated(data, value):
    (data/'diagnostics-v04.jsonl').write_text(json.dumps({'event': 'hook_invocation',
        'status': 'below_threshold', 'stdout_chars': value})+'\n', encoding='utf-8')
    result = audit.diagnostics()
    assert result['invalid_rows']+result['malformed_rows'] == 1 and result['matching_invocations'] == 0


def test_session_only_recovery_cannot_guess_among_valid_and_quarantined_owners(data):
    identifier = save_original('intact\n')
    write_rows(data, compression(artifact_id=identifier),
               compression(compression_id='bad', artifact_id=identifier, optimized_chars=None))
    with pytest.raises(ValueError):
        read_lines(identifier, 1, 1, session_id='s1')
    assert read_lines(identifier, 1, 1, compression_id='c1', session_id='s1') == 'intact\n'
    assert read_lines(identifier, 1, 1) == 'intact\n'


def test_diagnostic_requires_status(data):
    (data/'diagnostics-v04.jsonl').write_text(json.dumps({'event': 'hook_invocation',
        'session_id': 's', 'experiment_id': 'e'})+'\n', encoding='utf-8')
    result = audit.diagnostics()
    assert result['invalid_rows'] == 1 and result['data_available'] is False
    assert result['invocations'] == []


def test_delivery_tool_identity_conflict_cannot_confirm_stdout(data):
    write_rows(data, compression(tool_use_id='tool1'), {'event': 'replacement_delivery',
        'compression_id': 'c1', 'session_id': 's1', 'experiment_id': 'e1', 'source': 'manual',
        'tool_use_id': 'different', 'status': 'stdout_write_succeeded'})
    result = audit.summary()
    assert result['confirmed_stdout_deliveries'] == 0 and result['delivery_unknown'] == 1
    assert result['data_available'] is False


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -float('inf')])
def test_nonfinite_json_in_metadata_is_rejected_at_parse_boundary(data, value):
    row = compression(original_token_estimate={'estimated_tokens': 250, 'method': value})
    write_rows(data, row)
    result = audit.summary()
    assert result['malformed_rows'] == 1 and result['compressions'] == 0
    assert result['data_available'] is False
    json.dumps(result, allow_nan=False)


def test_nonfinite_mocked_events_are_quarantined_by_schema(data, monkeypatch):
    row = compression(original_token_estimate={'estimated_tokens': 250, 'method': float('nan')})
    monkeypatch.setattr(audit, 'events', lambda: ([row], 0))
    result = audit.summary()
    assert result['invalid_rows'] == 1 and result['compressions'] == 0
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize('duplicate', ['"compression_id":"other",',
                                    '"original_chars":10000,',
                                    '"original_token_estimate":{"estimated_tokens":1,"estimated_tokens":2},'])
def test_duplicate_json_keys_cannot_create_false_unambiguous_evidence(data, duplicate):
    raw = json.dumps(compression())
    write_rows(data, ('{'+duplicate+raw[1:]+'\n').encode())
    result = audit.summary()
    assert result['malformed_rows'] == 1 and result['compressions'] == 0
    assert result['data_available'] is False


def test_null_recovery_id_does_not_hide_event_deduplication_identity(data):
    row = {'event': 'recovery', 'event_id': 'r1', 'recovery_id': None,
           'compression_id': 'c1', 'session_id': 's1', 'experiment_id': 'e1',
           'artifact_id': 'a'*64, 'returned_chars': 7}
    write_rows(data, compression(), row, row)
    result = audit.summary()
    assert result['recovery_count'] == 1 and result['returned_chars'] == 7
    assert result['duplicate_rows'] == 1 and result['data_available'] is False


def test_standard_json_exponent_overflow_in_nested_metadata_is_rejected(data):
    raw = json.dumps(compression())
    write_rows(data, (raw[:-1]+',"metadata":{"nested":[1e999]}}\n').encode())
    result = audit.summary()
    assert result['malformed_rows'] == 1 and result['compressions'] == 0
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize('scope', [{}, {'source': 'manual'}, {'session_id': 's1'},
    {'experiment_id': 'e1'}, {'session_id': 's1', 'experiment_id': 'e1'},
    {'session_id': 's1', 'experiment_id': 'e1', 'source': 'manual'}])
def test_legacy_recovery_derives_missing_context_from_unique_owner(data, scope):
    row = {'event': 'recovery', 'compression_id': 'c1', 'returned_chars': 7}
    write_rows(data, compression(), row)
    before = (data/'audit-v04.jsonl').read_bytes()
    result = audit.summary(**scope)
    assert result['recovery_count'] == 1 and result['returned_chars'] == 7
    assert result['records'][0]['recovery_count'] == 1
    assert result['data_available'] is True
    assert (data/'audit-v04.jsonl').read_bytes() == before
    assert audit.events()[0][1] == row


@pytest.mark.parametrize('field', ['session_id', 'experiment_id', 'artifact_id'])
def test_explicit_null_context_is_not_derived_from_owner(data, field):
    row = {'event': 'recovery', 'compression_id': 'c1', 'returned_chars': 7, field: None}
    write_rows(data, compression(), row)
    result = audit.summary(session_id='s1', experiment_id='e1')
    assert result['recovery_count'] == 0 and result['invalid_rows'] == 1
    assert result['data_available'] is False


def test_derived_recovery_view_never_mutates_input_rows(data, monkeypatch):
    row = {'event': 'recovery', 'compression_id': 'c1', 'returned_chars': 7}
    rows = [compression(), row]
    before = json.dumps(rows, sort_keys=True)
    monkeypatch.setattr(audit, 'events', lambda: (rows, 0))
    assert audit.summary(session_id='s1')['returned_chars'] == 7
    assert json.dumps(rows, sort_keys=True) == before
