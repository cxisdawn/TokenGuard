"""Local evidence contracts; no Docker daemon, model, network or credentials."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tokenguard import audit
from tokenguard.quality_protocol import evidence, pair_assessment, receipt, trace_tools
from tokenguard.quality_seed import build_seeds, checked_preflight, inside
from tokenguard.visible_tests import canonical_path, fingerprint, run_recorded


def fixture_receipt(folder, *, run_id='a' * 32, source='initial', code=0, **extra):
    folder.mkdir(exist_ok=True)
    raw = ('tests/test_x.py::test_a PASSED [100%]\n=== 1 passed in 0.1s ===\n'
           '[TokenGuard test_run=' + run_id + ']\n').encode()
    row = {'schema': 'tokenguard.visible-test/v1', 'run_id': run_id, 'session_id': 's',
           'experiment_id': 'e', 'origin': 'agent', 'image_id': 'pinned',
           'source_before': source, 'source_after': source, 'full_suite_requested': True,
           'arguments': [], 'exit_code': code, 'timed_out': False, 'outputs_truncated': False,
           'stdout_sha256': hashlib.sha256(raw).hexdigest(), 'stderr_sha256': hashlib.sha256(b'').hexdigest(),
           'stdout_bytes': len(raw), 'stderr_bytes': 0, **extra}
    path = folder / (run_id + '.json')
    path.write_text(json.dumps(row), encoding='utf-8')
    path.with_suffix('.stdout.txt').write_bytes(raw)
    path.with_suffix('.stderr.txt').write_bytes(b'')
    return path, raw.decode()


def tool_trace(run_id='a' * 32, key='t1', command='python pytest.py', text=None):
    return [{'type': 'assistant', 'session_id': 's', 'message': {'content': [
        {'type': 'tool_use', 'id': key, 'name': 'Bash', 'input': {'command': command}}]}},
        {'type': 'user', 'session_id': 's', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': key, 'content': text or '[TokenGuard test_run=' + run_id + ']'}]}}]


class TestQualityEvidence(unittest.TestCase):
    def inspect(self, folder, events, **kwargs):
        return evidence(events, [], folder, session='s', experiment='e', initial_source='initial',
                        final_source='final', required_command='python pytest.py', image_id='pinned',
                        expected_tests=1, **kwargs)

    def test_initial_and_final_require_distinct_actual_test_receipts(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            fixture_receipt(folder)
            one = self.inspect(folder, tool_trace())
            self.assertTrue(one['initial_test_observed'])
            self.assertFalse(one['agent_test_protocol_satisfied'])
            fixture_receipt(folder, run_id='b' * 32, source='final', code=1)
            both = self.inspect(folder, tool_trace() + tool_trace('b' * 32, 't2'))
            self.assertTrue(both['agent_test_protocol_satisfied'])
            self.assertEqual(both['test_runs'][1]['exit_code'], 1)
            reversed_runs = self.inspect(folder, tool_trace('b' * 32, 't2') + tool_trace())
            self.assertFalse(reversed_runs['agent_test_protocol_satisfied'])

    def test_test_command_mentions_without_receipts_do_not_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.inspect(Path(temporary), tool_trace(text='Command not found'))
            self.assertEqual(result['required_command_mentions'], 1)
            self.assertFalse(result['initial_test_observed'])

    def test_partial_timeout_modified_source_and_custom_selection_do_not_count(self):
        for extra in ({'timed_out': True}, {'outputs_truncated': True}, {'exit_code': 2},
                      {'source_after': 'changed'}, {'arguments': ['-k', 'one']},
                      {'full_suite_requested': False}, {'image_id': 'other'}, {'origin': 'controller'}):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                fixture_receipt(folder, **extra)
                result = self.inspect(folder, tool_trace())
                self.assertFalse(result['initial_test_observed'])

    def test_missing_or_nonboolean_execution_flags_are_unknown_not_success(self):
        for field in ('timed_out', 'outputs_truncated'):
            for value in ('missing', None, 0, 'false'):
                with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as temporary:
                    folder = Path(temporary)
                    fixture_receipt(folder)
                    path, _ = fixture_receipt(folder, run_id='b' * 32, source='final')
                    row = json.loads(path.read_text())
                    if value == 'missing':
                        row.pop(field)
                    else:
                        row[field] = value
                    path.write_text(json.dumps(row))
                    result = self.inspect(folder, tool_trace() + tool_trace('b' * 32, 't2'))
                    self.assertFalse(result['agent_test_protocol_satisfied'])
                    self.assertEqual(result['invalid_receipts'], 1)

    def test_boolean_or_noninteger_receipt_exit_code_is_invalid(self):
        for value in (False, True, 0.0, '0', None):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                fixture_receipt(folder, code=value)
                result = self.inspect(folder, tool_trace())
                self.assertFalse(result['initial_test_observed'])
                self.assertEqual(result['invalid_receipts'], 1)

    def test_wrong_test_count_is_incomplete(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            fixture_receipt(folder)
            result = evidence(tool_trace(), [], folder, session='s', experiment='e', initial_source='initial',
                              final_source='final', required_command='python pytest.py', expected_tests=169)
            self.assertFalse(result['initial_test_observed'])

    def test_missing_or_modified_logs_and_cross_session_are_rejected(self):
        for mutation in ('stdout', 'stderr', 'session'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                path, _ = fixture_receipt(folder, **({'session_id': 'other'} if mutation == 'session' else {}))
                if mutation != 'session':
                    path.with_suffix('.' + mutation + '.txt').write_bytes(b'tampered')
                with self.assertRaises(ValueError):
                    receipt(path, 's', 'e')
                self.assertEqual(self.inspect(folder, tool_trace())['invalid_receipts'], 1)

    def test_trace_conflicts_and_cross_session_prevent_compliance(self):
        calls, results, errors = trace_tools(tool_trace() * 2, 's')
        self.assertEqual((len(calls), len(results), errors), (1, 1, []))
        events = tool_trace() + tool_trace(command='different')
        self.assertIn('conflicting_tool_use', trace_tools(events, 's')[2])
        events.append({'type': 'system', 'session_id': 'other'})
        self.assertIn('mixed_or_invalid_session', trace_tools(events, 's')[2])

    def test_compression_visibility_requires_same_bash_tool_id_and_audit_scope(self):
        guide = '[TokenGuard artifact=' + 'c' * 64 + '; compression_id=' + 'd' * 32 + ']'
        row = {'event': 'compression', 'session_id': 's', 'experiment_id': 'e', 'source': 'claude_hook',
               'tool_use_id': 't1', 'artifact_id': 'c' * 64, 'compression_id': 'd' * 32}
        with tempfile.TemporaryDirectory() as temporary:
            for delta, expected in (({}, 1), ({'tool_use_id': 'wrong'}, 0), ({'source': 'manual'}, 0),
                                    ({'session_id': 'other'}, 0), ({'experiment_id': 'other'}, 0)):
                result = evidence(tool_trace(text=guide), [{**row, **delta}], Path(temporary), session='s',
                                  experiment='e', initial_source='initial', final_source=None, required_command=None)
                self.assertEqual(result['native_compressions_in_matching_bash_results'], expected)

    def test_source_fingerprint_is_byte_sensitive_and_manifest_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'pylint').mkdir()
            path = root / 'pylint/a.py'
            path.write_bytes(b'a\r\n')
            first = fingerprint(root, ['pylint/a.py'])
            path.write_bytes(b'a\n')
            self.assertNotEqual(first, fingerprint(root, ['pylint/a.py']))
            for files in ([], ['../secret'], ['pylint/../secret'], ['/pylint/a.py']):
                with self.assertRaises(ValueError):
                    fingerprint(root, files)

    def test_actual_process_exit_and_stdout_are_recorded_without_docker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'pylint').mkdir()
            (root / 'pylint/a.py').write_text('value = 1\n')
            env = {'TOKENGUARD_TEST_RECEIPTS_DIR': str(root / 'receipts'),
                   'TOKENGUARD_TEST_SESSION_ID': 's', 'TOKENGUARD_EXPERIMENT_ID': 'e'}
            stdout, stderr = type('Output', (), {'buffer': io.BytesIO()})(), type('Output', (), {'buffer': io.BytesIO()})()
            with patch.dict(os.environ, env), patch('sys.stdout', stdout), patch('sys.stderr', stderr):
                code = run_recorded(root, 'local-test', ['pylint/a.py'], [],
                                    [sys.executable, '-c', "print('FAILED actual child'); raise SystemExit(1)"])
            self.assertEqual(code, 1)
            path = next((root / 'receipts').glob('*.json'))
            row = receipt(path, 's', 'e')
            self.assertEqual(row['exit_code'], 1)
            self.assertEqual(row['source_before'], row['source_after'])
            self.assertIn(b'FAILED actual child', stdout.buffer.getvalue())

    def test_worker_timeout_receipt_is_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'pylint').mkdir()
            (root / 'pylint/a.py').write_text('a=1')
            env = {'TOKENGUARD_TEST_RECEIPTS_DIR': str(root / 'receipts')}
            output = type('Output', (), {'buffer': io.BytesIO()})()
            with patch.dict(os.environ, env), patch('sys.stdout', output), patch('sys.stderr', output), \
                 patch('tokenguard.visible_tests.subprocess.run', side_effect=subprocess.TimeoutExpired('test', 150)):
                code = run_recorded(root, 'local', ['pylint/a.py'], [], ['never'], cleanup_name='owned')
            self.assertEqual(code, 124)
            row = json.loads(next((root / 'receipts').glob('*.json')).read_text())
            self.assertTrue(row['timed_out'])

    def test_pair_retains_failure_and_never_claims_non_inferiority(self):
        base = {'grade': {'evaluation_valid': True, 'resolved': True},
                'source_changes': {'only_allowed_changes': True},
                'protocol_evidence': {'agent_test_protocol_satisfied': True}, 'compression': {'compressions': 0}}
        optimized = {**base, 'grade': {'evaluation_valid': True, 'resolved': False}, 'task_success': False,
                     'protocol_evidence': {'agent_test_protocol_satisfied': True,
                                           'native_compressions_in_matching_bash_results': 1}}
        result = pair_assessment(base, optimized, 'agent_driven')
        self.assertTrue(result['observed_code_quality_drop'])
        self.assertEqual(result['general_non_inferiority'], 'not_established')
        optimized['protocol_evidence']['agent_test_protocol_satisfied'] = False
        self.assertFalse(pair_assessment(base, optimized, 'agent_driven')['comparison_evidence_satisfied'])
        self.assertIn('pair_incomplete', pair_assessment(base, {'not_run': True}, 'agent_driven')['reasons'])

    def test_controlled_seed_cannot_be_counted_as_native_hook_evidence(self):
        row = {'grade': {'evaluation_valid': True, 'resolved': True},
               'source_changes': {'only_allowed_changes': True}, 'compression': {'compressions': 0}}
        b = {**row, 'seed_delivery': {'verified_before_launch': True, 'original_stdout_sha256': 'h', 'compressed': False}}
        o = {**row, 'seed_delivery': {'verified_before_launch': True, 'original_stdout_sha256': 'h', 'compressed': True}}
        self.assertTrue(pair_assessment(b, o, 'controlled_initial_log')['compression_exposed'])
        self.assertFalse(pair_assessment(b, o, 'agent_driven')['compression_exposed'])
        o['seed_delivery']['original_stdout_sha256'] = 'different'
        self.assertFalse(pair_assessment(b, o, 'controlled_initial_log')['comparison_evidence_satisfied'])

    def test_true_marker_without_bound_evidence_cannot_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / 'quality-preflight-latest.json').write_text(json.dumps({'passed': True, 'path': 'missing'}))
            with self.assertRaises(ValueError):
                checked_preflight(folder, {}, {})
            for name in ('../outside', str(folder.parent / 'outside')):
                with self.assertRaises(ValueError):
                    inside(folder, name)

    def test_bound_preflight_is_rechecked_instead_of_trusting_passed_flag(self):
        from tokenguard.quality_protocol import file_sha
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            plan = {'protocol': 'agent_driven'}
            (folder / 'quality-plan.json').write_text(json.dumps(plan))
            preflight = {'passed': True, 'protocol': 'agent_driven', 'visible_test_exit': 0,
                         'manual_hook_compressed': True, 'visible_receipt_verified': True,
                         'original': {'evaluation_valid': True, 'tests_status': {
                             'FAIL_TO_PASS': {'failure': list(range(4)), 'success': []},
                             'PASS_TO_PASS': {'success': list(range(171))}}},
                         'gold': {'evaluation_valid': True, 'resolved': True}}
            path = folder / 'preflight.json'
            path.write_text(json.dumps(preflight))
            marker = {'passed': True, 'path': 'preflight.json', 'sha256': file_sha(path),
                      'plan_sha256': file_sha(folder / 'quality-plan.json')}
            marker_path = folder / 'quality-preflight-latest.json'
            marker_path.write_text(json.dumps(marker))
            self.assertTrue(checked_preflight(folder, plan, {})['passed'])
            preflight['gold']['resolved'] = False
            path.write_text(json.dumps(preflight))
            with self.assertRaisesRegex(ValueError, 'changed'):
                checked_preflight(folder, plan, {})
            marker['sha256'] = file_sha(path)
            marker_path.write_text(json.dumps(marker))
            with self.assertRaisesRegex(ValueError, 'criteria'):
                checked_preflight(folder, plan, {})

    def test_source_link_is_rejected_without_reading_its_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'pylint').mkdir()
            (root / 'pylint/a.py').write_bytes(b'ordinary source')
            with patch('tokenguard.visible_tests.Path.is_symlink', return_value=True), \
                 patch('pathlib.Path.open') as opener:
                with self.assertRaisesRegex(ValueError, 'links'):
                    fingerprint(root, ['pylint/a.py'])
                opener.assert_not_called()

    def test_canonical_spelling_change_is_not_itself_a_link(self):
        # Windows hosted runners expose RUNNER~1 while resolve expands it. No
        # string equality between absolute()/resolve() can establish a link.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            expanded = path.parent / 'expanded-canonical-spelling'
            with patch('pathlib.Path.resolve', return_value=expanded):
                self.assertEqual(canonical_path(path), expanded)
            if os.name == 'nt':
                import ctypes
                get_short = ctypes.windll.kernel32.GetShortPathNameW
                get_short.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong]
                get_short.restype = ctypes.c_ulong
                buffer = ctypes.create_unicode_buffer(32768)
                self.assertGreater(get_short(str(path), buffer, len(buffer)), 0)
                alias = Path(buffer.value)
                self.assertEqual(canonical_path(alias), canonical_path(path))
                fixture_receipt(alias)
                self.assertTrue(self.inspect(alias, tool_trace())['initial_test_observed'])

    def test_junction_attribute_is_rejected_before_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            fake_stat = type('Stat', (), {'st_file_attributes': 0x400})()
            with patch('pathlib.Path.is_symlink', return_value=False), \
                 patch('pathlib.Path.lstat', return_value=fake_stat), patch('pathlib.Path.open') as opener:
                with self.assertRaisesRegex(ValueError, 'reparse'):
                    canonical_path(Path(temporary))
                opener.assert_not_called()

    def test_source_filtered_stats_join_recovery_by_compression_id(self):
        rows = [{'event': 'compression', 'source': source, 'session_id': 's', 'experiment_id': 'e',
                 'compression_id': source, 'original_chars': 100, 'optimized_chars': 10}
                for source in ('manual', 'claude_hook')]
        rows += [{'event': 'recovery', 'session_id': 's', 'experiment_id': 'e', 'compression_id': 'manual',
                  'returned_chars': 5}]
        with patch('tokenguard.audit.events', return_value=(rows, 0)):
            self.assertEqual(audit.summary('s', 'e', source='claude_hook')['recovery_count'], 0)
            self.assertEqual(audit.summary('s', 'e', source='manual')['recovery_count'], 1)
            self.assertEqual(audit.summary('s', 'e')['compressions'], 2)

    def test_controlled_seed_refuses_short_noncompressible_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            path, text = fixture_receipt(folder, session_id='controller-preflight', origin='controller')
            record = {'experiment_id': 'e', 'task': {'prompt': 'Fix fixture'},
                      'trials': [{'trial_id': '001-optimized', 'session_id': 's', 'arm': 'optimized'}]}
            visible = {'stdout': text, 'stderr': '', 'exit_code': 0, 'timed_out': False, 'outputs_truncated': False}
            with self.assertRaisesRegex(ValueError, 'actual compression'):
                build_seeds(folder, record, folder, visible, path)
