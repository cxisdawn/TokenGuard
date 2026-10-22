"""Offline contracts for the opt-in quality pilot; no Docker/model dependency."""
import json
import os
from pathlib import Path
import tempfile
import unittest
import runpy
from unittest.mock import patch

from tokenguard.swebench_quality import (candidate_patch, execute_quality, parse_stream,
                                        prepare_quality, report_quality, visible_wrapper)


class TestQualityPilot(unittest.TestCase):
    def test_stream_deduplicates_tool_chunks_and_checks_session(self):
        tool = {'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'id': 'tool-1', 'name': 'Bash'},
            {'type': 'tool_use', 'id': 'tool-2', 'name': 'Read'}]}}
        raw = '\n'.join(json.dumps(e) for e in (tool, tool, {'type': 'result', 'session_id': 's', 'is_error': False}))
        _, metrics = parse_stream(raw, 's')
        self.assertEqual(metrics, {'tool_calls': 2, 'bash_calls': 1})
        with self.assertRaises(ValueError):
            parse_stream(raw, 'different-session')

    def test_stream_rejects_missing_or_duplicate_final_results(self):
        result = json.dumps({'type': 'result', 'session_id': 's'})
        malformed = json.dumps({'type': 'assistant', 'message': {'content': ['not-a-block']}})
        mixed = json.dumps({'type': 'system', 'session_id': 'different'})
        for raw in ('', result + '\n' + result, '[1,2]', '{broken', malformed + '\n' + result, mixed + '\n' + result):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    parse_stream(raw, 's')

    def test_paid_gate_default_and_limit_precede_any_agent_launch(self):
        record = {'trials': [{}] * 6}
        with patch('tokenguard.swebench_quality.load', return_value=(record, {}, {})), \
             patch('tokenguard.swebench_quality.run_process') as runner:
            for args in ({}, {'allow_paid': True, 'max_paid_sessions': 2},
                         {'allow_paid': True, 'max_paid_sessions': True}):
                with self.assertRaises(ValueError):
                    execute_quality(Path('.'), execute=True, **args)
            runner.assert_not_called()

    def test_no_compression_does_not_claim_quality_effect(self):
        record = {'trials': [{'trial_id': '001-baseline', 'pair_id': 'round-001', 'arm': 'baseline'},
                             {'trial_id': '001-optimized', 'pair_id': 'round-001', 'arm': 'optimized'}],
                  'task': {'model': 'test-model'}}
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            for trial in record['trials']:
                root = folder / 'trials' / trial['trial_id']
                root.mkdir(parents=True)
                row = {**trial, 'task_success': True, 'compression': {'compressions': 0}}
                (root / 'quality-result.json').write_text(json.dumps(row), encoding='utf-8')
            with patch('tokenguard.swebench_quality.load', return_value=(record, {'scope': 'fixture'}, {})):
                report = report_quality(folder)
            self.assertFalse(report['pairs'][0]['compression_exposed'])
            self.assertEqual(report['pairs'][0]['observed_quality_drop'], 'not_evaluable')
            self.assertEqual(report['actual_billed_cost'], 'unavailable')

    def test_protected_test_change_cannot_enter_candidate_patch(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            work = folder / 'worktree'
            work.mkdir()
            (work / 'test_original.py').write_text('modified', encoding='utf-8')
            import hashlib
            record = {'files_sha256': {'test_original.py': hashlib.sha256(b'original').hexdigest()},
                      'task': {'editable_files': ['library.py']}}
            content, check = candidate_patch(folder, record, work)
            self.assertEqual(content, '')
            self.assertFalse(check['only_allowed_changes'])

    def test_visible_wrapper_pins_image_and_mounts_only_source_readonly(self):
        wrapper = visible_wrapper('sha256:' + 'a' * 64)
        self.assertIn('sha256:' + 'a' * 64, wrapper)
        self.assertIn('target=/testbed/pylint,readonly', wrapper)
        self.assertIn("'--network', 'none'", wrapper)
        self.assertNotIn('test_patch', wrapper)
        compile(wrapper, 'pytest.py', 'exec')

    def test_analysis_separates_valid_patch_from_turn_limit_exit(self):
        analyze = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'benchmark/quality_analysis.py'))['analyze']
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            trial = {'trial_id': '001-optimized', 'pair_id': 'round-001', 'arm': 'optimized', 'session_id': 's'}
            (folder / 'experiment.json').write_text(json.dumps({'experiment_id': 'e', 'trials': [trial]}))
            (folder / 'quality-plan.json').write_text(json.dumps({'instance_id': 'fixture'}))
            root = folder / 'trials' / trial['trial_id']
            root.mkdir(parents=True)
            row = {**trial, 'grade': {'resolved': True, 'evaluation_valid': True, 'tests_status': {}},
                   'task_success': False, 'agent_exit_code': 1, 'agent_elapsed_ms': 1, 'tool_calls': 2,
                   'compression': {'compressions': 0, 'recovery_count': 0, 'saved_chars': 0},
                   'source_changes': {'changed_files': ['pylint/x.py'], 'only_allowed_changes': True},
                   'model_cited_matching_metadata_count': 0, 'cli_aggregate_usage_unverified': {}}
            (root / 'quality-result.json').write_text(json.dumps(row))
            (root / 'agent.stream.jsonl').write_text(json.dumps({'type': 'result', 'session_id': 's',
                                                               'subtype': 'error_max_turns'}) + '\n')
            result = analyze(folder)
            self.assertEqual(result['summary']['optimized']['code_resolved'], 1)
            self.assertEqual(result['summary']['optimized']['tasks_completed_and_valid'], 0)
            self.assertEqual(result['summary']['optimized']['max_turns_exhausted'], 1)
            self.assertEqual(result['actual_billed_cost'], 'unavailable')
            # A guide from another compression/session must not become our
            # treatment-visibility evidence or silently contaminate a control.
            guide = '[TokenGuard artifact=' + 'a' * 64 + '; compression_id=' + 'b' * 32 + ']'
            user = {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': guide}]}}
            with (root / 'agent.stream.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(user) + '\n')
            result = analyze(folder)
            self.assertEqual(result['runs'][0]['compressed_messages_in_host_transcript'], 0)
            self.assertEqual(result['runs'][0]['unattributed_host_metadata_pairs'], 1)

    def test_candidate_diff_retains_no_final_newline_marker(self):
        import hashlib
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            for name in ('snapshot', 'worktree'):
                (folder / name).mkdir()
            (folder / 'snapshot/library.py').write_bytes(b'old\n')
            (folder / 'worktree/library.py').write_bytes(b'new')
            record = {'files_sha256': {'library.py': hashlib.sha256(b'old\n').hexdigest()},
                      'task': {'editable_files': ['library.py']}}
            content, check = candidate_patch(folder, record, folder / 'worktree')
            self.assertTrue(check['only_allowed_changes'])
            self.assertIn('+new\n\\ No newline at end of file\n', content)

    def test_invalid_turn_bounds_rejected_before_docker(self):
        with patch('tokenguard.swebench_quality.instance') as source:
            for value in (0, 101, True, '40'):
                with self.subTest(value=value):
                    with self.assertRaises(ValueError):
                        prepare_quality(Path('unused'), Path('unused'), Path('unused'), max_turns=value)
            source.assert_not_called()

    def test_dry_run_reports_prepared_turn_limit_without_agent_launch(self):
        record = {'trials': [{}] * 2, 'task': {'model': 'fixture', 'max_turns': 40}}
        with patch('tokenguard.swebench_quality.load', return_value=(record, {}, {})), \
             patch('tokenguard.swebench_quality.run_process') as runner:
            result = execute_quality(Path('.'))
            self.assertEqual(result['max_turns_flag'], 40)
            self.assertTrue(result['dry_run'])
            runner.assert_not_called()
