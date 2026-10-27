"""Reports recheck stored evidence; cached success and counts are not certificates."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tokenguard.experiment import load_experiment, write_json
from tokenguard.experiment_report import report
from tokenguard.experiment_validation import validate
import test_experiment
from test_quality_compare import API, add_results, project_experiment


class TestReportIntegrity(unittest.TestCase):
    def test_mock_validation_is_bound_to_current_candidate_and_legacy_is_unknown(self):
        fixture = test_experiment.TaskExperiments()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        folder = fixture.prepare()
        test_experiment.exp.execute(folder, execute_run=True)
        root = folder / 'trials/001-optimized'
        path = root / 'result.json'
        original_result = json.loads(path.read_text())
        original_source = (root / 'worktree/arithmetic.py').read_bytes()
        original_test = (root / 'worktree/test_arithmetic.py').read_bytes()
        for damage in ('source', 'protected', 'unexpected', 'legacy_missing_binding'):
            with self.subTest(damage=damage):
                if damage == 'source':
                    (root / 'worktree/arithmetic.py').write_bytes(b'BROKEN NOW\n')
                elif damage == 'protected':
                    (root / 'worktree/test_arithmetic.py').write_bytes(b'def test_fake(): pass\n')
                elif damage == 'unexpected':
                    (root / 'worktree/new_file.py').write_bytes(b'UNEXPECTED\n')
                else:
                    old = json.loads(json.dumps(original_result))
                    old['final_validation'].pop('validated_files_sha256')
                    write_json(path, old)
                checked = report(folder)
                row = checked['runs'][1]
                self.assertIsNone(row['task_success'])
                self.assertIs(row['reported_task_success'], True)
                self.assertFalse(row['current_source_binding_verified'])
                self.assertFalse(row['quality_evidence_valid'])
                self.assertEqual(checked['prepared_trials'], 2)
                (root / 'worktree/arithmetic.py').write_bytes(original_source)
                (root / 'worktree/test_arithmetic.py').write_bytes(original_test)
                (root / 'worktree/new_file.py').unlink(missing_ok=True)
                write_json(path, original_result)
        self.assertEqual(report(folder)['successful_trials'], 2)

    def test_validation_detects_source_modified_inside_test_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ('snapshot', 'candidate', 'validation'):
                (root / name).mkdir()
            for name in ('snapshot', 'candidate'):
                (root / name / 'code.py').write_text('VALUE = 1\n')
                (root / name / 'test_code.py').write_text(
                    'from pathlib import Path\ndef test_mutate():\n'
                    '    Path("code.py").write_text("VALUE = 2\\n")\n    assert True\n')
            spec = {'files': ['code.py', 'test_code.py'], 'editable_files': ['code.py'],
                    'test_files': ['test_code.py'], 'timeout_seconds': 30, 'expected_tests': 1}
            result = validate(root / 'snapshot', root / 'candidate', spec, root / 'validation')
            self.assertEqual(result['exit_code'], 0)
            self.assertFalse(result['validated_source_unchanged'])
            self.assertFalse(result['passed'])
            self.assertEqual((root / 'candidate/code.py').read_text(), 'VALUE = 1\n')

    def test_valid_mock_results_pass_and_mutated_quality_fields_do_not(self):
        fixture = test_experiment.TaskExperiments()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        folder = fixture.prepare()
        test_experiment.exp.execute(folder, execute_run=True)
        self.assertEqual(report(folder)['successful_trials'], 2)
        path = folder / 'trials/001-optimized/result.json'
        original = json.loads(path.read_text())
        for field, value in (('tests_passed', 1), ('tests_unchanged', None), ('timed_out', None),
                             ('agent_exit_code', False), ('elapsed_ms', True), ('elapsed_ms', float('nan'))):
            with self.subTest(field=field, value=value):
                write_json(path, {**original, field: value})
                checked = report(folder)
                self.assertEqual(checked['successful_trials'], 1)
                self.assertIsNone(checked['runs'][1]['task_success'])
                self.assertEqual(checked['prepared_trials'], 2)
                self.assertEqual(checked['local_wall_clock_pairs'][0]['status'], 'incomplete_or_failed')
        write_json(path, original)
        self.assertEqual(report(folder)['successful_trials'], 2)

    def test_contradictory_task_result_cannot_pass_or_certify_missing_audit(self):
        fixture = test_experiment.TaskExperiments()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        folder = fixture.prepare()
        for trial in load_experiment(folder)['trials']:
            write_json(folder / 'trials' / trial['trial_id'] / 'result.json', {
                **trial, 'task_success': True, 'tests_passed': False, 'tests_unchanged': False,
                'agent_exit_code': 9, 'elapsed_ms': -5, 'stage_errors': [{'stage': 'audit'}]})
        result = report(folder)
        self.assertEqual(result['successful_trials'], 0)
        self.assertEqual(result['local_wall_clock_pairs'][0]['status'], 'incomplete_or_failed')
        for run in result['runs']:
            self.assertIsNot(run['task_success'], True)
            self.assertFalse(run['compression_data_available'])
            self.assertIsNone(run['elapsed_ms'])
            self.assertTrue(run['report_evidence_errors'])

    def test_cached_saved_characters_cannot_override_audit(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = project_experiment(Path(temporary))
            add_results(folder)
            path = folder / 'trials/001-optimized/quality-result.json'
            row = json.loads(path.read_text())
            row['compression']['saved_chars'] = 999999999
            write_json(path, row)
            items, _ = API['collect']([folder])
            run = items[0]['analysis']['runs'][1]
            self.assertFalse(run['compression_data_available'])
            self.assertIsNone(run['saved_stdout_chars'])
            self.assertIsNone(run['task_completed_and_valid'])
            self.assertTrue(run['code_resolved'])
            self.assertIn('audit-v04.jsonl:cached_metrics_differ', run['analysis_evidence_errors'])

    def test_changed_source_or_missing_grade_binding_is_unknown(self):
        for damage in ('source', 'missing_patch', 'missing_grade', 'grade'):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as temporary:
                folder = project_experiment(Path(temporary))
                add_results(folder)
                root = folder / 'trials/001-optimized'
                if damage == 'source':
                    (root / 'worktree/pylint/value.py').write_text('BROKEN NOW\n')
                elif damage.startswith('missing'):
                    (root / 'evaluation' / ('candidate.patch' if damage == 'missing_patch' else 'grade.json')).unlink(missing_ok=True)
                else:
                    write_json(root / 'evaluation/grade.json', {'resolved': False})
                items, _ = API['collect']([folder])
                run = items[0]['analysis']['runs'][1]
                self.assertIsNone(run['code_resolved'])
                self.assertIsNone(run['task_completed_and_valid'])
                self.assertFalse(run['grade_source_binding_verified'])
                group = API['scorecard'](items)['cohorts'][0]
                self.assertEqual(group['planned_pairs'], 1)
                self.assertEqual(group['all_planned_code_outcomes']['ungraded_or_incomplete'], 1)

    def test_incomplete_execution_flags_do_not_certify_strict_completion(self):
        for field in ('timed_out', 'outputs_truncated'):
            for value in ('missing', 1, 'true', None):
                with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as temporary:
                    folder = project_experiment(Path(temporary))
                    add_results(folder)
                    path = folder / 'trials/001-optimized/quality-result.json'
                    row = json.loads(path.read_text())
                    row.pop(field, None)
                    if value != 'missing':
                        row[field] = value
                    write_json(path, row)
                    items, _ = API['collect']([folder])
                    run = items[0]['analysis']['runs'][1]
                    self.assertIsNone(run['task_completed_and_valid'])
                    self.assertTrue(run['code_resolved'])

    def test_invalid_cached_counts_stay_unavailable(self):
        for key in ('compressions', 'recovery_count'):
            for value in (True, -1, None, '0'):
                with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as temporary:
                    folder = project_experiment(Path(temporary))
                    add_results(folder)
                    path = folder / 'trials/001-optimized/quality-result.json'
                    row = json.loads(path.read_text())
                    row['compression'][key] = value
                    write_json(path, row)
                    items, _ = API['collect']([folder])
                    self.assertFalse(items[0]['analysis']['runs'][1]['compression_data_available'])

    def test_valid_failed_grade_is_retained_and_unbound_reported_grade_is_visible(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = project_experiment(Path(temporary))
            add_results(folder)
            root = folder / 'trials/001-optimized'
            path = root / 'quality-result.json'
            row = json.loads(path.read_text())
            row['grade']['resolved'] = False
            row['task_success'] = False
            write_json(path, row)
            write_json(root / 'evaluation/grade.json', row['grade'])
            items, _ = API['collect']([folder])
            run = items[0]['analysis']['runs'][1]
            self.assertIs(run['code_resolved'], False)
            self.assertIs(run['task_completed_and_valid'], False)
            self.assertEqual(API['scorecard'](items)['cohorts'][0]['all_planned_code_outcomes']['baseline_only_resolved'], 1)
            (root / 'evaluation/grade.json').unlink()
            items, _ = API['collect']([folder])
            run = items[0]['analysis']['runs'][1]
            self.assertIsNone(run['code_resolved'])
            self.assertIs(run['reported_code_resolved'], False)

    def test_grade_and_protected_candidate_changes_during_collection_abort(self):
        for relative in ('evaluation/grade.json', 'evaluation/candidate.patch',
                         'worktree/test_value.py', 'worktree/new_file.py'):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temporary:
                folder = project_experiment(Path(temporary))
                add_results(folder)
                namespace = API['collect'].__globals__
                original = namespace['analyze']
                def mutate(path, **kwargs):
                    result = original(path, **kwargs)
                    (path / 'trials/001-optimized' / relative).write_bytes(b'changed')
                    return result
                with patch.dict(namespace, {'analyze': mutate}):
                    with self.assertRaisesRegex(ValueError, 'changed while reading'):
                        API['collect']([folder])

    def test_archived_grade_equality_is_type_sensitive(self):
        for field, value in (('resolved', 1), ('evaluation_valid', 1), ('parsed_tests', True)):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                folder = project_experiment(Path(temporary))
                add_results(folder)
                root = folder / 'trials/001-optimized'
                result = json.loads((root / 'quality-result.json').read_text())
                if field == 'parsed_tests':
                    result['grade']['parsed_tests'] = 1
                    write_json(root / 'quality-result.json', result)
                archived = {**result['grade'], field: value}
                write_json(root / 'evaluation/grade.json', archived)
                items, _ = API['collect']([folder])
                run = items[0]['analysis']['runs'][1]
                self.assertFalse(run['grade_source_binding_verified'])
                self.assertIsNone(run['code_resolved'])
