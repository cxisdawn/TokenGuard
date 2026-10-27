"""Descriptive scorecard, grouping and source-preservation contracts; zero API calls."""
from copy import deepcopy
import csv
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tokenguard.experiment import BASE, prepare, write_json
from tokenguard.visible_tests import fingerprint
from tokenguard.swebench_quality import candidate_patch
from test_quality_protocol import fixture_receipt, tool_trace

SCRIPT = BASE / 'benchmark/quality_compare.py'
API = runpy.run_path(str(SCRIPT))


def synthetic_item(outcomes=(True, False), *, eligible=True):
    record = {'experiment_id': 'synthetic', 'task': {'task_id': 'fixture', 'provider': 'mock', 'model': 'mock',
               'max_turns': 20, 'timeout_seconds': 600},
              'config': {'baseline_mode': 'no_hook', 'tools_sha256': 'tools'},
              'prompt_sha256': 'prompt', 'initial_snapshot_sha256': 'snapshot', 'config_sha256': 'config'}
    plan = {'instance_id': 'fixture', 'protocol': 'agent_driven', 'image_id': 'image'}
    runs = []
    for arm, resolved in zip(('baseline', 'optimized'), outcomes):
        runs.append({'arm': arm, 'pair_id': 'round-001', 'state': 'completed', 'evaluation_valid': True,
                     'only_allowed_changes': True, 'code_resolved': resolved, 'task_completed_and_valid': resolved,
                     'stage_errors': [], 'agent_end_reason': 'success', 'agent_elapsed_ms': 100 if arm == 'baseline' else 80,
                     'compression_data_available': True, 'compressions': int(arm == 'optimized'),
                     'recovery_count': 0, 'seed_compressions': 0, 'seed_recoveries': 0, 'tool_calls': 2,
                     'native_compressions_in_matching_bash_results': int(arm == 'optimized'),
                     'cli_aggregate_usage_unverified': {'input_tokens': 100}})
    pairs = [{'pair_id': 'round-001', 'comparison_evidence_satisfied': eligible,
              'compression_exposed': eligible, 'reasons': [] if eligible else ['compression_not_observed']}]
    return {'record': record, 'plan': plan, 'analysis': {'runs': runs, 'pairs': pairs}}


def project_experiment(root: Path, *, max_turns=20, rounds=1) -> Path:
    project = root / 'project'
    project.mkdir(parents=True)
    (project / 'pylint').mkdir()
    (project / 'pylint/value.py').write_text('VALUE = 1\n', encoding='utf-8')
    (project / 'test_value.py').write_text('def test_value():\n    assert True\n', encoding='utf-8')
    spec = {'schema_version': 1, 'task_id': 'offline-scorecard', 'provider': 'mock', 'model': 'mock',
            'prompt': 'SYNTHETIC fixture, no model invocation.', 'files': ['pylint/value.py', 'test_value.py'],
            'editable_files': ['pylint/value.py'], 'test_files': ['test_value.py'], 'max_turns': max_turns}
    write_json(root / 'task.json', spec)
    folder = prepare(project, root / 'task.json', root / 'output', rounds, baseline='no_hook')
    write_json(folder / 'quality-plan.json', {'instance_id': 'synthetic-fixture', 'protocol': 'agent_driven',
               'image_id': 'no-real-image', 'visible_expected_tests': 1,
               'required_command': 'python pytest.py',
               'initial_source_sha256': fingerprint(folder / 'snapshot', ['pylint/value.py'])})
    # Existing derived results must not be rewritten by the new overview.
    for name in ('quality-analysis.json', 'quality-analysis.md', 'quality-report.csv'):
        (folder / name).write_bytes(b'KEEP ORIGINAL DERIVED REPORT\r\n')
    return folder


def add_results(folder: Path, *, broken=False) -> None:
    record = json.loads((folder / 'experiment.json').read_text(encoding='utf-8'))
    for trial in record['trials']:
        root = folder / 'trials' / trial['trial_id']
        compressed = trial['arm'] == 'optimized'
        (root / 'worktree/pylint/value.py').write_text('VALUE = 2\n', encoding='utf-8')
        row = {**trial, 'task_success': True, 'grade': {'resolved': True, 'evaluation_valid': True, 'tests_status': {}},
               'source_changes': {'only_allowed_changes': True, 'changed_files': ['pylint/value.py']},
               'agent_exit_code': 0, 'agent_elapsed_ms': 100, 'tool_calls': 1,
               'timed_out': False, 'outputs_truncated': False, 'stage_errors': [],
               'compression': {'compressions': int(compressed), 'recovery_count': 0, 'saved_chars': 10 if compressed else 0},
               'protocol_evidence': {'agent_test_protocol_satisfied': True,
                                     'native_compressions_in_matching_bash_results': int(compressed)},
               'model_cited_matching_metadata_count': 0, 'cli_aggregate_usage_unverified': {'input_tokens': 10}}
        write_json(root / 'quality-result.json', row)
        (root / 'evaluation').mkdir()
        (root / 'evaluation/candidate.patch').write_bytes(candidate_patch(folder, record, root / 'worktree')[0].encode('utf-8'))
        write_json(root / 'evaluation/grade.json', row['grade'])
        guide = '[TokenGuard artifact=' + 'a' * 64 + '; compression_id=' + 'b' * 32 + ']'
        events = []
        for run_id, key in (('c' * 32, 't'), ('d' * 32, 'last')):
            _, text = fixture_receipt(root / 'test-receipts', run_id=run_id,
                source=fingerprint(folder / 'snapshot' if key == 't' else root / 'worktree',
                                   record['task']['editable_files']),
                session_id=trial['session_id'], experiment_id=record['experiment_id'], image_id='no-real-image')
            events += tool_trace(run_id, key, text=text + (guide if compressed else 'NO_RAW_EXPORT'))
        for event in events:
            event['session_id'] = trial['session_id']
        events.append({'type': 'result', 'session_id': trial['session_id'], 'subtype': 'success', 'is_error': False})
        text = '\n'.join(json.dumps(e) for e in events) + '\n'
        (root / 'agent.stream.jsonl').write_text(('{broken\n' if broken else '') + text, encoding='utf-8')
        if compressed:
            (root / 'tg-data').mkdir()
            (root / 'tg-data/audit-v04.jsonl').write_text(json.dumps({
                'event': 'compression', 'source': 'claude_hook', 'session_id': trial['session_id'],
                'experiment_id': record['experiment_id'], 'tool_use_id': 't',
                'original_chars': 100, 'optimized_chars': 90,
                'artifact_id': 'a' * 64, 'compression_id': 'b' * 32}) + '\n', encoding='utf-8')


class TestQualityScorecard(unittest.TestCase):
    def test_all_planned_outcomes_and_evidence_subset_are_separate(self):
        items = []
        for n, results in enumerate(((True, False), (False, True), (True, True), (False, False))):
            item = synthetic_item(results, eligible=n < 2)
            item['record']['experiment_id'] = str(n)
            items.append(item)
        group = API['scorecard'](items)['cohorts'][0]
        self.assertEqual(group['planned_pairs'], 4)
        self.assertEqual(list(group['all_planned_code_outcomes'].values()), [1, 1, 1, 1, 0])
        self.assertEqual(group['compression_evidence_subset']['pairs'], 2)
        self.assertEqual(group['ineligible_pairs'], 2)
        self.assertEqual(group['missing_evidence_counts']['compression_not_observed'], 2)
        self.assertEqual(group['general_non_inferiority'], 'not_established')

    def test_protocol_budget_model_and_pinned_inputs_never_pool(self):
        changes = [('task', 'max_turns', 40), ('task', 'timeout_seconds', 300), ('task', 'model', 'another'),
                   ('task', 'provider', 'another'), ('task', 'task_id', 'another'),
                   ('record', 'prompt_sha256', 'changed'), ('record', 'initial_snapshot_sha256', 'changed'),
                   ('record', 'config_sha256', 'changed'), ('plan', 'image_id', 'changed'),
                   ('plan', 'protocol', 'controlled_initial_log'), ('plan', 'harness_commit', 'another')]
        for target, key, value in changes:
            with self.subTest(target=target, key=key):
                one = synthetic_item()
                two = deepcopy(one)
                owner = two['record']['task'] if target == 'task' else two[target]
                owner[key] = value
                self.assertEqual(len(API['scorecard']([one, two])['cohorts']), 2)

        one=synthetic_item()
        two=deepcopy(one)
        two['record']['config']['pytest_mode']='indexed'
        self.assertEqual(len(API['scorecard']([one,two])['cohorts']),2)

    def test_incomplete_pair_stays_in_denominator_without_false_failure_measurement(self):
        item = synthetic_item((False, False))
        item['analysis']['runs'][1] = {'arm': 'optimized', 'pair_id': 'round-001', 'state': 'not_run'}
        item['analysis']['pairs'][0].update(comparison_evidence_satisfied=False, reasons=['pair_incomplete'])
        group = API['scorecard']([item])['cohorts'][0]
        self.assertEqual(group['all_planned_code_outcomes']['ungraded_or_incomplete'], 1)
        self.assertEqual(group['all_planned_code_outcomes']['neither_resolved'], 0)
        self.assertEqual(group['arms']['optimized']['planned'], 1)
        self.assertEqual(group['arms']['optimized']['valid_grades'], 0)
        self.assertEqual(group['arms']['optimized']['agent_elapsed_ms']['sum'], None)

    def test_max_turn_exit_does_not_erase_valid_patch_or_become_strict_success(self):
        item = synthetic_item((True, True))
        for run in item['analysis']['runs']:
            run.update(agent_end_reason='error_max_turns', task_completed_and_valid=False)
        group = API['scorecard']([item])['cohorts'][0]
        self.assertEqual(group['all_planned_code_outcomes']['both_resolved'], 1)
        self.assertEqual(group['arms']['baseline']['strict_task_success'], 0)
        self.assertEqual(group['arms']['baseline']['max_turns_exhausted'], 1)

    def test_measurement_coverage_and_unverified_usage_do_not_imply_billing(self):
        measured = API['measurement']([100, None, True, -1, float('nan'), float('inf')], 6)
        self.assertEqual(measured['sum'], 100)
        self.assertEqual(measured['known'], 1)
        self.assertFalse(measured['complete'])
        result = API['scorecard']([synthetic_item()])
        self.assertEqual(result['actual_billed_cost'], 'unavailable')
        self.assertEqual(result['actual_provider_usage'], 'unavailable')
        self.assertEqual(result['model_api_calls'], 0)

    def test_controlled_origins_and_preflight_must_be_verified(self):
        one = synthetic_item()
        one['plan']['protocol'] = 'controlled_initial_log'
        for run in one['analysis']['runs']:
            run['seed_original_stdout_sha256'] = 'a' * 64
        group = API['scorecard']([one])['cohorts'][0]
        self.assertEqual(group['compression_evidence_subset']['pairs'], 0)
        one['seed_inputs_verified'] = True
        self.assertEqual(API['scorecard']([one])['cohorts'][0]['compression_evidence_subset']['pairs'], 1)
        two = deepcopy(one)
        for run in two['analysis']['runs']:
            run['seed_original_stdout_sha256'] = 'b' * 64
        self.assertEqual(len(API['scorecard']([one, two])['cohorts']), 2)

    def test_read_only_exports_keep_old_reports_and_all_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = project_experiment(root / 'one')
            second = project_experiment(root / 'two', max_turns=40)
            before = {p: p.read_bytes() for folder in (first, second) for p in folder.rglob('*') if p.is_file()}
            output = API['compare']([first, second], root / 'scores')
            result = json.loads((output / 'scorecard.json').read_text())
            self.assertEqual(len(result['cohorts']), 2)
            self.assertTrue(all(g['all_planned_code_outcomes']['ungraded_or_incomplete'] == 1 for g in result['cohorts']))
            with (output / 'pairs.csv').open(newline='') as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 2)
            for path, original in before.items():
                self.assertEqual(path.read_bytes(), original)
            self.assertNotEqual(output, API['compare']([first], root / 'scores'))

    def test_duplicate_experiment_and_reused_session_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            one = project_experiment(root / 'one')
            duplicate = root / 'copy'
            shutil.copytree(one, duplicate)
            with self.assertRaisesRegex(ValueError, 'Duplicate experiment'):
                API['collect']([one, duplicate])
            two = project_experiment(root / 'two')
            first_record = json.loads((one / 'experiment.json').read_text())
            second_record = json.loads((two / 'experiment.json').read_text())
            second_record['trials'][0]['session_id'] = first_record['trials'][0]['session_id']
            write_json(two / 'experiment.json', second_record)
            with self.assertRaisesRegex(ValueError, 'Reused session'):
                API['collect']([one, two])

    def test_damaged_stream_keeps_observation_but_invalid_grade_is_not_a_measurement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = project_experiment(root / 'experiment')
            add_results(folder, broken=True)
            output = API['compare']([folder], root / 'scores')
            group = json.loads((output / 'scorecard.json').read_text())['cohorts'][0]
            self.assertEqual(group['all_planned_code_outcomes']['both_resolved'], 1)
            self.assertEqual(group['compression_evidence_subset']['pairs'], 0)
            path = folder / 'trials/001-optimized/quality-result.json'
            row = json.loads(path.read_text())
            row['grade']['resolved'] = 'false'
            write_json(path, row)
            output = API['compare']([folder], root / 'scores')
            group = json.loads((output / 'scorecard.json').read_text())['cohorts'][0]
            self.assertEqual(group['all_planned_code_outcomes']['ungraded_or_incomplete'], 1)

    def test_current_bash_audit_match_is_required_and_raw_logs_are_not_exported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = project_experiment(root / 'experiment')
            add_results(folder)
            output = API['compare']([folder], root / 'scores')
            text = (output / 'scorecard.json').read_text()
            group = json.loads(text)['cohorts'][0]
            self.assertEqual(group['compression_evidence_subset']['pairs'], 1)
            self.assertNotIn('NO_RAW_EXPORT', text)
            # The stored execution verdict alone cannot replace missing current audit evidence.
            (folder / 'trials/001-optimized/tg-data/audit-v04.jsonl').write_bytes(b'')
            output = API['compare']([folder], root / 'scores')
            group = json.loads((output / 'scorecard.json').read_text())['cohorts'][0]
            self.assertEqual(group['compression_evidence_subset']['pairs'], 0)

    def test_cached_native_protocol_success_cannot_replace_current_receipts(self):
        for damage in ('missing', 'tampered', 'wrong_source'):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                folder = project_experiment(root / 'experiment')
                add_results(folder)
                trial = folder / 'trials/001-optimized'
                receipt = trial / 'test-receipts' / ('d' * 32 + '.json')
                if damage == 'missing':
                    receipt.unlink()
                elif damage == 'tampered':
                    receipt.with_suffix('.stdout.txt').write_bytes(b'changed evidence')
                else:
                    (trial / 'worktree/pylint/value.py').write_text('VALUE = 3\n')
                items, _ = API['collect']([folder])
                group = API['scorecard'](items)['cohorts'][0]
                outcome = 'ungraded_or_incomplete' if damage == 'wrong_source' else 'both_resolved'
                self.assertEqual(group['all_planned_code_outcomes'][outcome], 1)
                self.assertEqual(group['compression_evidence_subset']['pairs'], 0)
                self.assertFalse(items[0]['analysis']['runs'][1]['test_protocol_satisfied'])

    def test_cached_healthy_audit_does_not_hide_current_invalid_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = project_experiment(Path(temporary))
            add_results(folder)
            items, _ = API['collect']([folder])
            self.assertEqual(API['scorecard'](items)['cohorts'][0]['compression_evidence_subset']['pairs'], 1)
            audit_path = folder / 'trials/001-optimized/tg-data/audit-v04.jsonl'
            with audit_path.open('ab') as stream:
                stream.write(b'{"event":"compression","original_chars":true,"optimized_chars":0}\n')
            items, _ = API['collect']([folder])
            run = items[0]['analysis']['runs'][1]
            self.assertFalse(run['compression_data_available'])
            self.assertTrue(run['code_resolved'])
            group = API['scorecard'](items)['cohorts'][0]
            self.assertEqual(group['all_planned_code_outcomes']['both_resolved'], 1)
            self.assertEqual(group['compression_evidence_subset']['pairs'], 0)

    def test_current_baseline_compression_cannot_be_hidden_by_cached_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = project_experiment(Path(temporary))
            add_results(folder)
            record = json.loads((folder / 'experiment.json').read_text())
            trial = record['trials'][0]
            root = folder / 'trials' / trial['trial_id']
            guide = '[TokenGuard artifact=' + 'a' * 64 + '; compression_id=' + 'b' * 32 + ']'
            stream = root / 'agent.stream.jsonl'
            stream.write_text(stream.read_text().replace('NO_RAW_EXPORT', guide))
            (root / 'tg-data').mkdir()
            (root / 'tg-data/audit-v04.jsonl').write_text(json.dumps({
                'event': 'compression', 'source': 'claude_hook', 'session_id': trial['session_id'],
                'experiment_id': record['experiment_id'], 'tool_use_id': 't',
                'original_chars': 100, 'optimized_chars': 90,
                'artifact_id': 'a' * 64, 'compression_id': 'b' * 32}) + '\n')
            items, _ = API['collect']([folder])
            self.assertIsNone(items[0]['analysis']['runs'][0]['compressions'])
            self.assertFalse(items[0]['analysis']['runs'][0]['compression_data_available'])
            self.assertIn('audit-v04.jsonl:cached_counts_differ',
                          items[0]['analysis']['runs'][0]['analysis_evidence_errors'])
            self.assertEqual(items[0]['analysis']['runs'][0]['native_compressions_in_matching_bash_results'], 1)
            group = API['scorecard'](items)['cohorts'][0]
            self.assertEqual(group['compression_evidence_subset']['pairs'], 0)
            self.assertIn('baseline_compression_contamination', group['pairs'][0]['missing_evidence'])

    def test_configuration_and_all_snapshot_inputs_cannot_change_during_collection(self):
        for name in ('task.json', 'settings.json', 'no-hook-settings.json', 'mcp.json',
                     'official-instance.json', 'snapshot/test_value.py'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                folder = project_experiment(Path(temporary))
                add_results(folder)
                globals_ = API['collect'].__globals__
                original = globals_['analyze']
                def mutate(path, **kwargs):
                    result = original(path, **kwargs)
                    (path / name).write_bytes(b'{"changed":true}')
                    return result
                with patch.dict(globals_, {'analyze': mutate}):
                    with self.assertRaisesRegex(ValueError, 'changed while reading'):
                        API['collect']([folder])

    def test_change_after_initial_validation_before_hash_snapshot_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = project_experiment(Path(temporary))
            add_results(folder)
            globals_ = API['collect'].__globals__
            original = globals_['load_experiment']
            def mutate(path):
                result = original(path)
                (path / 'no-hook-settings.json').write_bytes(b'{"changed":true}')
                return result
            with patch.dict(globals_, {'load_experiment': mutate}):
                with self.assertRaisesRegex(ValueError, 'changed while reading'):
                    API['collect']([folder])

    def test_receipt_and_source_changes_during_analysis_abort_collection(self):
        for relative in ('test-receipts/' + 'd' * 32 + '.json',
                         'test-receipts/' + 'd' * 32 + '.stdout.txt',
                         'test-receipts/' + 'd' * 32 + '.stderr.txt',
                         'worktree/pylint/value.py', 'test-receipts/' + 'e' * 32 + '.json',
                         'tg-data/diagnostics-v04.jsonl'):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                folder = project_experiment(root / 'experiment')
                add_results(folder)
                globals_ = API['collect'].__globals__
                original = globals_['analyze']
                def mutate(path, **kwargs):
                    result = original(path, **kwargs)
                    (path / 'trials/001-optimized' / relative).write_bytes(b'changed evidence')
                    return result
                with patch.dict(globals_, {'analyze': mutate}):
                    with self.assertRaisesRegex(ValueError, 'changed while reading'):
                        API['collect']([folder])

    def test_output_cannot_be_written_inside_input_and_mid_read_changes_abort(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = project_experiment(root / 'experiment')
            with self.assertRaisesRegex(ValueError, 'outside'):
                API['compare']([folder], folder / 'scores')
            globals_ = API['collect'].__globals__
            original = globals_['analyze']
            def mutate(path, **kwargs):
                result = original(path, **kwargs)
                (path / 'trials/001-baseline/agent.stream.jsonl').write_bytes(b'new evidence')
                return result
            with patch.dict(globals_, {'analyze': mutate}):
                with self.assertRaisesRegex(ValueError, 'changed while reading'):
                    API['compare']([folder], root / 'scores')

    def test_oversized_stream_keeps_sample_without_reading_unbounded_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = project_experiment(root / 'experiment')
            add_results(folder)
            name = 'trials/001-optimized/agent.stream.jsonl'
            (folder / name).write_bytes(b'x' * (16 * 1024 * 1024 + 1))
            output = API['compare']([folder], root / 'scores')
            result = json.loads((output / 'scorecard.json').read_text())
            self.assertEqual(result['cohorts'][0]['all_planned_code_outcomes']['both_resolved'], 1)
            self.assertEqual(result['cohorts'][0]['compression_evidence_subset']['pairs'], 0)
            self.assertIsNone(result['sources'][0]['sha256'][name]['sha256'])

    def test_cli_is_local_and_rejects_tampered_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = project_experiment(root / 'experiment')
            run = subprocess.run([sys.executable, '-X', 'utf8', str(SCRIPT), str(folder), '--output', str(root / 'scores')],
                                 capture_output=True, text=True, encoding='utf-8', timeout=30)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertTrue(Path(run.stdout.strip()).is_file())
            (folder / 'no-hook-settings.json').write_text('{"altered":true}')
            with self.assertRaisesRegex(ValueError, 'changed'):
                API['collect']([folder])
