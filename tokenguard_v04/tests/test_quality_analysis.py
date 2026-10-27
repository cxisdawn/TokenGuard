"""Reporting must distinguish invalid/missing observations from measured failure."""
import json
from pathlib import Path
import tempfile
import unittest

from test_quality_compare import API, add_results, project_experiment
from tokenguard.experiment import write_json

analyze = API['analyze']


class TestQualityAnalysis(unittest.TestCase):
    def test_invalid_grade_and_disallowed_changes_never_count_as_success(self):
        for field in ('evaluation_valid', 'only_allowed_changes'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                folder = project_experiment(Path(temporary))
                add_results(folder)
                path = folder / 'trials/001-optimized/quality-result.json'
                row = json.loads(path.read_text())
                row['grade' if field == 'evaluation_valid' else 'source_changes'][field] = False
                write_json(path, row)
                result = analyze(folder, write=False)
                self.assertIsNone(result['runs'][1]['code_resolved'])
                self.assertIsNone(result['runs'][1]['task_completed_and_valid'])
                self.assertEqual(result['summary']['optimized']['code_resolved'], 0)
                self.assertEqual(result['summary']['optimized']['tasks_completed_and_valid'], 0)
                self.assertEqual(result['summary']['optimized']['valid_grades'], 0)

    def test_observed_unresolved_patch_stays_false_and_in_denominator(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = project_experiment(Path(temporary))
            add_results(folder)
            path = folder / 'trials/001-optimized/quality-result.json'
            row = json.loads(path.read_text())
            row['grade']['resolved'] = False
            row['task_success'] = False
            write_json(path, row)
            # This fixture represents a measured failure, so its archived grade
            # must agree with the cached result (tampering has separate tests).
            write_json(path.parent / 'evaluation/grade.json', row['grade'])
            result = analyze(folder, write=False)
            self.assertIs(result['runs'][1]['code_resolved'], False)
            self.assertEqual(result['summary']['optimized']['valid_grades'], 1)
            self.assertEqual(result['summary']['optimized']['planned'], 1)

    def test_absent_counts_are_unknown_and_malformed_statuses_do_not_crash(self):
        for statuses in ({}, None, [], {'FAIL_TO_PASS': None},
                         {'FAIL_TO_PASS': {'success': None}}, {'FAIL_TO_PASS': {'success': 'fake'}}):
            with self.subTest(statuses=statuses), tempfile.TemporaryDirectory() as temporary:
                folder = project_experiment(Path(temporary))
                add_results(folder)
                path = folder / 'trials/001-optimized/quality-result.json'
                row = json.loads(path.read_text())
                row['grade']['tests_status'] = statuses
                row.pop('seed_compression', None)
                row['compression'].pop('compressions')
                row['compression'].pop('recovery_count')
                row['cli_aggregate_usage_unverified'] = None
                write_json(path, row)
                result = analyze(folder, write=False)
                run = result['runs'][1]
                self.assertIsNone(run['f2p_success'])
                self.assertIsNone(run['p2p_success'])
                self.assertIsNone(run['seed_compressions'])
                self.assertIsNone(result['summary']['optimized']['compressions'])
                self.assertFalse(result['summary']['optimized']['compression_data_complete'])
                group = API['scorecard']([{'record': json.loads((folder / 'experiment.json').read_text()),
                    'plan': json.loads((folder / 'quality-plan.json').read_text()), 'analysis': result}])['cohorts'][0]
                self.assertIsNone(group['arms']['optimized']['native_compressions']['sum'])
                self.assertIsNone(group['arms']['optimized']['cli_usage_UNVERIFIED']['input_tokens']['sum'])

    def test_broken_result_remains_ungraded_without_hiding_planned_pair(self):
        for raw in ('{broken', '[]', 'null', '{"trial_id":"wrong"}'):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                folder = project_experiment(root / 'experiment')
                add_results(folder)
                (folder / 'trials/001-optimized/quality-result.json').write_text(raw)
                items, _ = API['collect']([folder])
                result = items[0]['analysis']
                self.assertEqual(result['summary']['optimized']['planned'], 1)
                self.assertEqual(result['summary']['optimized']['code_resolved'], 0)
                self.assertEqual(result['runs'][1]['state'], 'evidence_invalid')
                group = API['scorecard'](items)['cohorts'][0]
                self.assertEqual(group['all_planned_code_outcomes']['ungraded_or_incomplete'], 1)
                self.assertEqual(group['all_planned_code_outcomes']['neither_resolved'], 0)

    def test_cached_task_success_cannot_override_failed_agent_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = project_experiment(Path(temporary))
            add_results(folder)
            stream = folder / 'trials/001-optimized/agent.stream.jsonl'
            events = [json.loads(line) for line in stream.read_text().splitlines()]
            events[-1].update(subtype='error_max_turns', is_error=True)
            stream.write_text('\n'.join(json.dumps(e) for e in events) + '\n')
            result = analyze(folder, write=False)
            self.assertIs(result['runs'][1]['code_resolved'], True)
            self.assertIs(result['runs'][1]['task_completed_and_valid'], False)
            self.assertEqual(result['summary']['optimized']['tasks_completed_and_valid'], 0)

    def test_invalid_test_identifiers_and_conflicting_statuses_invalidate_grade(self):
        for group in ({'success': [None]}, {'success': ['test_a', 'test_a']},
                      {'success': ['test_a'], 'failure': ['test_a']},
                      {'success': [], 'failure': 'test_a'}, {'success': [], 'failure': [None]},
                      {'success': [], 'failure': ['test_a', 'test_a']},
                      {'success': [], 'failure': ['test_a']}):
            with self.subTest(group=group), tempfile.TemporaryDirectory() as temporary:
                folder = project_experiment(Path(temporary))
                add_results(folder)
                path = folder / 'trials/001-optimized/quality-result.json'
                row = json.loads(path.read_text())
                row['grade']['tests_status'] = {'FAIL_TO_PASS': group}
                write_json(path, row)
                result = analyze(folder, write=False)
                self.assertFalse(result['runs'][1]['evaluation_valid'])
                self.assertIsNone(result['runs'][1]['code_resolved'])
                self.assertIsNone(result['runs'][1]['f2p_success'])

    def test_known_zero_test_success_count_is_not_unknown(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = project_experiment(Path(temporary))
            add_results(folder)
            path = folder / 'trials/001-optimized/quality-result.json'
            row = json.loads(path.read_text())
            row['grade'].update(resolved=False, tests_status={
                'FAIL_TO_PASS': {'success': [], 'failure': ['test_a']},
                'PASS_TO_PASS': {'success': ['test_b'], 'failure': []}})
            write_json(path, row)
            write_json(path.parent / 'evaluation/grade.json', row['grade'])
            result = analyze(folder, write=False)
            self.assertEqual(result['runs'][1]['f2p_success'], 0)
            self.assertEqual(result['runs'][1]['p2p_success'], 1)
            self.assertIs(result['runs'][1]['code_resolved'], False)

    def test_launch_attempt_without_result_is_unknown_not_unrun(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = project_experiment(Path(temporary))
            trial = json.loads((folder / 'experiment.json').read_text())['trials'][1]
            write_json(folder / 'trials/001-optimized/agent-started.json', trial)
            result = analyze(folder, write=False)
            self.assertEqual(result['runs'][1]['state'], 'evidence_invalid')
            self.assertEqual(result['runs'][0]['state'], 'not_run')
            self.assertEqual(result['summary']['optimized']['planned'], 1)
            self.assertEqual(result['summary']['optimized']['valid_grades'], 0)

    def test_invalid_exit_code_cannot_count_as_strict_success(self):
        for exit_code in (False, True, '0', 0.0, None):
            with self.subTest(exit_code=exit_code), tempfile.TemporaryDirectory() as temporary:
                folder = project_experiment(Path(temporary))
                add_results(folder)
                path = folder / 'trials/001-optimized/quality-result.json'
                row = json.loads(path.read_text())
                row['agent_exit_code'] = exit_code
                write_json(path, row)
                result = analyze(folder, write=False)
                self.assertIsNone(result['runs'][1]['task_completed_and_valid'])
                self.assertIs(result['runs'][1]['code_resolved'], True)

    def test_receipt_failure_does_not_erase_separate_valid_agent_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = project_experiment(Path(temporary))
            add_results(folder)
            (folder / 'trials/001-optimized/test-receipts' / ('d' * 32 + '.stdout.txt')).write_bytes(b'corrupted')
            result = analyze(folder, write=False)
            self.assertIs(result['runs'][1]['code_resolved'], True)
            self.assertIs(result['runs'][1]['task_completed_and_valid'], True)
            self.assertFalse(result['runs'][1]['test_protocol_satisfied'])
            self.assertFalse(result['pairs'][0]['comparison_evidence_satisfied'])

    def test_missing_stream_does_not_become_observed_zero_tool_errors_or_matches(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = project_experiment(Path(temporary))
            add_results(folder)
            (folder / 'trials/001-optimized/agent.stream.jsonl').unlink()
            result = analyze(folder, write=False)
            for field in ('tool_error_results', 'compressed_messages_in_host_transcript',
                          'native_compressions_in_matching_bash_results'):
                self.assertIsNone(result['runs'][1][field])
            self.assertIsNone(result['runs'][1]['task_completed_and_valid'])

    def test_report_exports_ungraded_observation_without_overwriting_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = project_experiment(Path(temporary))
            add_results(folder)
            path = folder / 'trials/001-optimized/quality-result.json'
            path.write_bytes(b'{invalid result')
            before = {p: p.read_bytes() for p in (folder / 'trials').rglob('*') if p.is_file()}
            result = analyze(folder)
            self.assertEqual(result['model_api_calls_from_analysis'], 0)
            self.assertEqual(result['summary']['optimized']['evidence_invalid'], 1)
            self.assertIn('evidence_invalid', (folder / 'quality-analysis.csv').read_text())
            self.assertIn('Limitations', (folder / 'quality-analysis.md').read_text())
            self.assertEqual(json.loads((folder / 'quality-analysis.json').read_text()), result)
            for path, original in before.items():
                self.assertEqual(path.read_bytes(), original)

    def test_malformed_stopped_marker_preserves_outcomes_but_reports_unknown_evidence(self):
        for raw in ('{broken', '[]', 'null'):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as temporary:
                folder = project_experiment(Path(temporary))
                add_results(folder)
                (folder / 'execution-stopped.json').write_text(raw)
                result = analyze(folder, write=False)
                self.assertEqual(result['summary']['optimized']['code_resolved'], 1)
                self.assertIsNone(result['execution_stopped'])
                self.assertFalse(result['execution_stopped_available'])
                self.assertTrue(result['report_evidence_errors'])
                self.assertFalse(result['pairs'][0]['comparison_evidence_satisfied'])

    def test_partial_known_zero_compression_total_cannot_claim_not_exercised(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = project_experiment(Path(temporary), rounds=2)
            add_results(folder)
            for trial_id in ('001-optimized', '002-optimized'):
                root = folder / 'trials' / trial_id
                path = root / 'quality-result.json'
                row = json.loads(path.read_text())
                row['compression']['compressions'] = 0
                row['compression']['saved_chars'] = 0
                if trial_id == '002-optimized':
                    row['compression'].pop('compressions')
                write_json(path, row)
                (root / 'tg-data/audit-v04.jsonl').write_bytes(b'')
            result = analyze(folder, write=False)
            self.assertEqual(result['summary']['optimized']['compressions'], 0)
            self.assertFalse(result['summary']['optimized']['compression_data_complete'])
            self.assertNotEqual(result['interpretation'], 'compression_not_exercised')

    def test_observed_compression_cannot_be_denied_by_cached_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = project_experiment(Path(temporary))
            add_results(folder)
            path = folder / 'trials/001-optimized/quality-result.json'
            row = json.loads(path.read_text())
            row['compression']['compressions'] = 0
            write_json(path, row)
            result = analyze(folder, write=False)
            self.assertEqual(result['runs'][1]['native_compressions_in_matching_bash_results'], 1)
            self.assertNotEqual(result['interpretation'], 'compression_not_exercised')

    def test_unreadable_current_audit_cannot_confirm_cached_zero_compressions(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = project_experiment(Path(temporary))
            add_results(folder)
            path = folder / 'trials/001-optimized/quality-result.json'
            row = json.loads(path.read_text())
            row['compression']['compressions'] = 0
            write_json(path, row)
            (path.parent / 'tg-data/audit-v04.jsonl').write_bytes(b'{broken')
            result = analyze(folder, write=False)
            self.assertTrue(result['runs'][1]['analysis_evidence_errors'])
            self.assertNotEqual(result['interpretation'], 'compression_not_exercised')
