"""Full runner contracts with an actual offline child CLI; no paid calls."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import patch

from tokenguard.experiment import BASE, command, prepare, write_json
from tokenguard.experiment_validation import run_process, validate
from tokenguard.quality_protocol import file_sha
from tokenguard.quality_seed import build_seeds
from tokenguard.swebench_quality import execute_quality
from tokenguard.visible_tests import fingerprint

CLI = Path(__file__).with_name('fixtures') / 'quality_cli.py'


@contextmanager
def experiment(protocol='agent_driven', *, pytest_mode='compact'):
    with tempfile.TemporaryDirectory(prefix='tg-quality-execution-') as temporary:
        root = Path(temporary).resolve()
        project = root / 'project'
        (project / 'pylint').mkdir(parents=True)
        (project / 'tests').mkdir()
        (project / 'pylint/__init__.py').write_bytes(b'')
        (project / 'pylint/arithmetic.py').write_bytes((BASE / 'benchmark/fixture/arithmetic.py').read_bytes())
        tests = (BASE / 'benchmark/fixture/test_arithmetic.py').read_text(encoding='utf-8')
        (project / 'test_arithmetic.py').write_text(tests.replace('from arithmetic', 'from pylint.arithmetic'), encoding='utf-8')
        helper = ('import sys\nfrom pathlib import Path\n'
                  f'sys.path.insert(0, {str(BASE)!r})\n'
                  'from tokenguard.visible_tests import run_recorded\n'
                  'root = Path(__file__).resolve().parents[1]\n'
                  "raise SystemExit(run_recorded(root, 'offline-fixture', ['pylint/arithmetic.py'], [], "
                  "[sys.executable, '-m', 'pytest', '-v', '--color=no', '-p', 'no:cacheprovider', 'test_arithmetic.py']))\n")
        (project / 'tests/pytest.py').write_text(helper, encoding='utf-8')
        task = {'schema_version': 1, 'task_id': 'offline-runner-contract', 'provider': 'mock', 'model': 'mock',
                'prompt': 'Deterministic mock fixture, not a coding ability experiment.',
                'files': ['pylint/__init__.py', 'pylint/arithmetic.py', 'tests/pytest.py', 'test_arithmetic.py'],
                'editable_files': ['pylint/arithmetic.py'], 'test_files': ['test_arithmetic.py'],
                'max_turns': 2, 'timeout_seconds': 60, 'expected_tests': 504}
        write_json(root / 'task.json', task)
        folder = prepare(project, root / 'task.json', root / 'output', 1, baseline='no_hook', pytest_mode=pytest_mode)
        record = json.loads((folder / 'experiment.json').read_text(encoding='utf-8'))
        plan = {'protocol': protocol, 'instance_id': 'offline-fixture', 'pytest_mode': pytest_mode,
                'scope': 'offline mock CLI/fixture; NOT official scoring or live Claude',
                'image_id': 'offline-fixture', 'visible_expected_tests': 504,
                'initial_source_sha256': fingerprint(folder / 'snapshot', task['editable_files']),
                'required_command': '"' + sys.executable.replace('\\', '/') + '" tests/pytest.py'}
        write_json(folder / 'quality-plan.json', plan)
        yield folder, record, plan


def cli_command(folder, record, trial, driver, executable):
    return [sys.executable, str(CLI), *command(folder, record, trial, driver, executable)[1:]]


def fixture_grade(folder, plan, row, patch_text, output):
    output.mkdir()
    check = validate(folder / 'snapshot', output.parent / 'worktree', record_for(folder)['task'], output)
    return {'resolved': bool(patch_text) and check['passed'],
            'evaluation_valid': not check['timed_out'] and not check['outputs_truncated'],
            'tests_status': {}, 'source': 'actual local pristine pytest fixture, NOT official grader'}


def record_for(folder):
    return json.loads((folder / 'experiment.json').read_text(encoding='utf-8'))


@contextmanager
def adapters(folder, record, plan, *, mode='quick', grade=None, preflight=None):
    # No installed agent is resolved or invoked. Only this owned Python child is launched.
    with patch.dict(os.environ, {'TG_FIXTURE_BASE': str(BASE), 'TG_FIXTURE_MODE': mode}), \
         patch('tokenguard.swebench_quality.load', return_value=(record, plan, {})), \
         patch('tokenguard.swebench_quality.checked_preflight', return_value=preflight or {}) as preflight_check, \
         patch('tokenguard.swebench_quality.resolve_claude', return_value={'executable': sys.executable}), \
         patch('tokenguard.swebench_quality.checked', return_value='offline-cli/1'), \
         patch('tokenguard.swebench_quality.command', side_effect=cli_command), \
         patch('tokenguard.swebench_quality.evaluate', side_effect=grade or fixture_grade):
        yield preflight_check


def analyze(folder):
    return runpy.run_path(str(BASE / 'benchmark/quality_analysis.py'))['analyze'](folder)


class TestQualityExecution(unittest.TestCase):
    def test_grader_failure_preserves_consumed_trial_and_stops_more_sessions(self):
        with experiment() as (folder, record, plan), adapters(folder, record, plan, grade=ValueError('private detail')):
            report = execute_quality(folder, execute=True, allow_paid=True)
            self.assertEqual(report['arms']['baseline']['completed'], 1)
            self.assertEqual(report['arms']['optimized']['completed'], 0)
            first = folder / 'trials/001-baseline'
            self.assertTrue((first / 'agent.stream.jsonl').exists())
            row = json.loads((first / 'quality-result.json').read_text(encoding='utf-8'))
            self.assertFalse(row['task_success'])
            self.assertFalse(row['grade']['evaluation_valid'])
            self.assertEqual(row['stage_errors'][0], {'stage': 'evaluation', 'type': 'ValueError'})
            self.assertNotIn('private detail', (first / 'quality-result.json').read_text())
            self.assertTrue((folder / 'execution-stopped.json').exists())
            self.assertFalse((folder / 'trials/001-optimized/agent-started.json').exists())

    def test_evidence_failure_is_saved_instead_of_discarding_valid_grade(self):
        grade = lambda *args: {'resolved': False, 'evaluation_valid': True, 'tests_status': {}}
        with experiment() as (folder, record, plan), adapters(folder, record, plan, grade=grade), \
             patch('tokenguard.swebench_quality.evidence', side_effect=ValueError('unsafe receipt')):
            report = execute_quality(folder, execute=True, allow_paid=True)
            first = report['runs'][0]
            self.assertTrue(first['grade']['evaluation_valid'])
            self.assertFalse(first['protocol_evidence']['agent_test_protocol_satisfied'])
            self.assertEqual(first['stage_errors'][0]['stage'], 'evidence')
            self.assertEqual(report['arms']['optimized']['completed'], 0)

    def test_optional_malformed_usage_does_not_erase_finished_session(self):
        grade = lambda *args: {'resolved': False, 'evaluation_valid': True, 'tests_status': {}}
        with experiment() as (folder, record, plan), adapters(folder, record, plan, mode='bad_usage', grade=grade):
            report = execute_quality(folder, execute=True, allow_paid=True)
            self.assertEqual(sum(a['completed'] for a in report['arms'].values()), 2)
            for row in report['runs']:
                self.assertEqual(row['cli_aggregate_usage_unverified'], {})
                self.assertEqual(row['actual_billed_cost'], 'unavailable')
                self.assertIsNone(row['permission_denial_count'])
                self.assertEqual(set(row['cli_metadata_warnings']), {'usage', 'modelUsage', 'permission_denials'})

    def test_changed_preflight_blocks_next_launch_and_remains_in_report(self):
        grade = lambda *args: {'resolved': False, 'evaluation_valid': True, 'tests_status': {}}
        with experiment() as (folder, record, plan), adapters(folder, record, plan, grade=grade) as preflight:
            preflight.side_effect = [{}, {}, ValueError('changed input')]
            report = execute_quality(folder, execute=True, allow_paid=True)
            self.assertEqual(report['arms']['baseline']['completed'], 1)
            self.assertEqual(report['arms']['optimized']['blocked_before_launch'], 1)
            self.assertFalse((folder / 'trials/001-optimized/agent-started.json').exists())
            read = analyze(folder)
            self.assertEqual(read['runs'][1]['state'], 'blocked_before_launch')
            self.assertEqual(read['summary']['optimized']['planned'], 1)
            self.assertEqual(read['summary']['optimized']['completed'], 0)
            self.assertFalse(read['pairs'][0]['comparison_evidence_satisfied'])

    def test_audit_failure_is_explicitly_unavailable_and_stops_next_launch(self):
        grade = lambda *args: {'resolved': False, 'evaluation_valid': True, 'tests_status': {}}
        with experiment() as (folder, record, plan), adapters(folder, record, plan, grade=grade), \
             patch('tokenguard.swebench_quality.audit.summary', side_effect=OSError('private path')):
            report = execute_quality(folder, execute=True, allow_paid=True)
            first = report['runs'][0]
            self.assertFalse(first['compression']['data_available'])
            self.assertIsNone(first['compression']['compressions'])
            self.assertEqual(first['stage_errors'][0]['stage'], 'audit')
            self.assertFalse(report['arms']['baseline']['compression_data_complete'])
            self.assertIn('baseline_compression_audit_unavailable', report['pairs'][0]['reasons'])
            read = analyze(folder)
            self.assertEqual(read['summary']['baseline']['infrastructure_failed'], 1)
            self.assertFalse(read['summary']['baseline']['compression_data_complete'])

    def test_source_validation_error_never_gets_graded_as_a_candidate(self):
        with experiment() as (folder, record, plan), adapters(folder, record, plan), \
             patch('tokenguard.swebench_quality.candidate_patch', side_effect=ValueError('invalid source')), \
             patch('tokenguard.swebench_quality.evaluate') as grader:
            report = execute_quality(folder, execute=True, allow_paid=True)
            self.assertEqual(report['runs'][0]['stage_errors'][0]['stage'], 'source_validation')
            grader.assert_not_called()

    def test_launch_error_is_a_recorded_attempt_not_a_successful_session(self):
        with experiment() as (folder, record, plan), adapters(folder, record, plan), \
             patch('tokenguard.swebench_quality.run_process', side_effect=OSError('launch unavailable')) as runner:
            report = execute_quality(folder, execute=True, allow_paid=True)
            self.assertEqual(report['runs'][0]['stage_errors'][0]['stage'], 'agent_process')
            self.assertIsNone(report['runs'][0]['agent_exit_code'])
            self.assertFalse(report['runs'][0]['task_success'])
            runner.assert_called_once()
            self.assertFalse((folder / 'trials/001-optimized/agent-started.json').exists())

    def test_invalid_stream_still_independently_grades_the_actual_patch(self):
        with experiment() as (folder, record, plan), adapters(folder, record, plan, mode='bad_json'):
            report = execute_quality(folder, execute=True, allow_paid=True)
            first = report['runs'][0]
            self.assertTrue(first['grade']['resolved'])
            self.assertFalse(first['task_success'])
            self.assertEqual(first['stage_errors'][0]['stage'], 'agent_stream')
            self.assertEqual(report['arms']['optimized']['completed'], 0)
            read = analyze(folder)
            self.assertTrue(read['runs'][0]['code_resolved'])
            self.assertIn('agent.stream.jsonl:invalid_row', read['runs'][0]['analysis_evidence_errors'])
            self.assertFalse(read['pairs'][0]['comparison_evidence_satisfied'])

    def test_real_local_tests_and_hook_through_runner_native_path(self):
        with experiment() as (folder, record, plan), adapters(folder, record, plan, mode='native'):
            report = execute_quality(folder, execute=True, allow_paid=True)
            self.assertTrue(all(r['task_success'] for r in report['runs']))
            self.assertTrue(report['pairs'][0]['comparison_evidence_satisfied'])
            optimized = next(r for r in report['runs'] if r['arm'] == 'optimized')
            self.assertEqual(optimized['protocol_evidence']['native_compressions_in_matching_bash_results'], 2)
            self.assertEqual(optimized['seed_compression']['compressions'], 0)
            self.assertEqual(report['arms']['baseline']['compressions'], 0)
            for row in report['runs']:
                self.assertTrue(row['protocol_evidence']['agent_test_protocol_satisfied'])
                self.assertEqual(row['actual_provider_usage'], 'unavailable')
            # A manually seeded guide echoed by a later tool must not become a
            # third native Hook match in the read-only analysis.
            root = folder / 'trials' / optimized['trial_id']
            manual = {'event': 'compression', 'source': 'manual', 'session_id': optimized['session_id'],
                      'experiment_id': record['experiment_id'], 'artifact_id': 'f' * 64, 'compression_id': 'e' * 32}
            with (root / 'tg-data/audit-v04.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(manual) + '\n')
            echoed = {'type': 'user', 'session_id': optimized['session_id'], 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': 'echo',
                 'content': '[TokenGuard artifact=' + 'f' * 64 + '; compression_id=' + 'e' * 32 + ']'}]}}
            with (root / 'agent.stream.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(echoed) + '\n')
            read = analyze(folder)
            matched = next(r for r in read['runs'] if r['arm'] == 'optimized')
            self.assertEqual(matched['compressed_messages_in_host_transcript'], 2)
            self.assertEqual(matched['manual_guides_echoed_in_tool_results'], 1)
            self.assertEqual(matched['unattributed_host_metadata_pairs'], 0)

    def test_controlled_prompts_and_selective_recovery_data_reach_runner(self):
        with experiment('controlled_initial_log') as (folder, record, plan):
            env = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1',
                   'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1', 'TOKENGUARD_TEST_RECEIPTS_DIR': str(folder / 'receipts'),
                   'TOKENGUARD_TEST_SESSION_ID': 'controller-preflight', 'TOKENGUARD_TEST_ORIGIN': 'controller',
                   'TOKENGUARD_EXPERIMENT_ID': record['experiment_id']}
            initial = run_process([sys.executable, str(folder / 'snapshot/tests/pytest.py')], folder / 'snapshot', env, 60)
            seed = build_seeds(folder, record, folder, initial, next((folder / 'receipts').glob('*.json')))
            with adapters(folder, record, plan, mode='controlled', preflight={'seed_inputs': seed}):
                report = execute_quality(folder, execute=True, allow_paid=True)
            self.assertTrue(report['pairs'][0]['comparison_evidence_satisfied'])
            for row in report['runs']:
                root = folder / 'trials' / row['trial_id']
                self.assertEqual(file_sha(root / 'received-prompt.txt'), seed['deliveries'][row['trial_id']]['prompt_sha256'])
                self.assertTrue(row['task_success'])
            optimized = next(r for r in report['runs'] if r['arm'] == 'optimized')
            self.assertEqual(optimized['seed_compression']['compressions'], 1)
            self.assertEqual(optimized['compression']['compressions'], 1)
            baseline = next(r for r in report['runs'] if r['arm'] == 'baseline')
            self.assertEqual(baseline['seed_compression']['compressions'], 0)

    def test_controller_seed_uses_prepared_indexed_mode(self):
        with experiment('controlled_initial_log', pytest_mode='indexed') as (folder, record, plan):
            env = {**os.environ, 'PYTHONUTF8': '1', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1',
                   'TOKENGUARD_TEST_RECEIPTS_DIR': str(folder/'receipts'),
                   'TOKENGUARD_TEST_SESSION_ID': 'controller-preflight', 'TOKENGUARD_TEST_ORIGIN': 'controller',
                   'TOKENGUARD_EXPERIMENT_ID': record['experiment_id']}
            initial = run_process([sys.executable, str(folder/'snapshot/tests/pytest.py')], folder/'snapshot', env, 60)
            seeds = build_seeds(folder, record, folder, initial, next((folder/'receipts').glob('*.json')))
            for arm in ('baseline','optimized'):
                delivery=seeds['deliveries']['001-'+arm]
                self.assertEqual(delivery['pytest_mode'],'indexed')
                text=(folder/delivery['prompt_path']).read_text(encoding='utf-8')
                self.assertEqual('pytest PASSED index' in text,arm=='optimized')
            data=folder/seeds['deliveries']['001-optimized']['recovery_data_dir']
            row=json.loads((data/'audit-v04.jsonl').read_text(encoding='utf-8'))
            self.assertEqual(row['strategy'],'pytest_pass_summary_indexed')
            self.assertEqual(row['pytest_mode'],'indexed')
