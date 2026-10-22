"""Distinguish omission, disabled adapters and actual compression without APIs."""
import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tokenguard import experiment as exp
from tokenguard.experiment_modes import baseline_mode,hook_mode
from tokenguard.experiment_report import report
from tokenguard.experiment_snapshot import digest
from tokenguard.experiment_validation import validate


class BaselineModes(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.project=self.root/'source';self.project.mkdir()
        (self.project/'arithmetic.py').write_text('def divide(a,b):\n    return a // b\n',encoding='utf-8')
        (self.project/'test_arithmetic.py').write_text('from arithmetic import divide\ndef test_fraction():\n    assert divide(3,2)==1.5\n',encoding='utf-8')
        self.task=json.loads((exp.BASE/'benchmark'/'task_fixture.json').read_text())
        self.task['expected_tests']=1
        self.spec=self.root/'task.json';exp.write_json(self.spec,self.task)

    def prepare(self,mode='no_hook'):
        return exp.prepare(self.project,self.spec,self.root/'experiments',baseline=mode)

    def test_modes_have_same_snapshot_and_distinct_protocol_identity(self):
        disabled=exp.load_experiment(self.prepare('disabled_hook'))
        omitted=exp.load_experiment(self.prepare())
        self.assertEqual(disabled['initial_snapshot_sha256'],omitted['initial_snapshot_sha256'])
        self.assertEqual(disabled['prompt_sha256'],omitted['prompt_sha256'])
        self.assertNotEqual(disabled['config_sha256'],omitted['config_sha256'])
        self.assertEqual(len(omitted['trials']),2)
        for trial in omitted['trials']:
            self.assertEqual(hook_mode(omitted,trial),'no_hook' if trial['arm']=='baseline' else 'enabled_hook')

    def test_dry_run_selects_empty_settings_without_permission_overrides(self):
        folder=self.prepare()
        with patch('subprocess.Popen',side_effect=AssertionError('No process in dry run')):
            plan=exp.execute(folder,'claude')
        self.assertEqual(plan['baseline_mode'],'no_hook')
        self.assertEqual(plan['trial_hook_modes'],{'001-baseline':'no_hook','001-optimized':'enabled_hook'})
        for index,command in enumerate(plan['commands']):
            settings=Path(command[command.index('--settings')+1])
            data=json.loads(settings.read_text())
            self.assertEqual(data,{} if index==0 else json.loads((folder/'settings.json').read_text()))
            self.assertEqual(command[command.index('--setting-sources')+1],'user')
            self.assertNotIn('--permission-mode',command)
            self.assertNotIn('--dangerously-skip-permissions',command)
        self.assertFalse((folder/'started.json').exists())

    def test_unknown_baseline_mode_rejected(self):
        for value in ('third_arm','',None,True):
            with self.subTest(value=value),self.assertRaises(ValueError):self.prepare(value)

    def test_no_hook_settings_mutation_rejected_even_with_recomputed_hash(self):
        folder=self.prepare();record=exp.load_experiment(folder)
        exp.write_json(folder/'no-hook-settings.json',{'hooks':{'PostToolUse':[]}})
        with self.assertRaises(ValueError):exp.load_experiment(folder)
        import hashlib
        record['config']['no_hook_settings_sha256']=hashlib.sha256((folder/'no-hook-settings.json').read_bytes()).hexdigest()
        record['config_sha256']=digest(record['config']);exp.write_json(folder/'experiment.json',record)
        with self.assertRaises(ValueError):exp.load_experiment(folder)

    def test_legacy_experiment_defaults_to_disabled_and_does_not_invent_timing(self):
        folder=self.prepare('disabled_hook');record=exp.load_experiment(folder)
        record['config'].pop('baseline_mode');record['config'].pop('no_hook_settings_sha256')
        record['config_sha256']=digest(record['config']);exp.write_json(folder/'experiment.json',record)
        (folder/'no-hook-settings.json').unlink()
        self.assertEqual(baseline_mode(exp.load_experiment(folder)['config']),'disabled_hook')
        result=report(folder)
        self.assertEqual(result['baseline_mode'],'disabled_hook')
        self.assertEqual(result['local_wall_clock_pairs'][0]['metrics'],'unavailable')
        self.assertNotIn('hook_process_elapsed_ms',result['runs'][0])

    def test_no_hook_full_fixture_has_zero_hook_processes_and_complete_raw_output(self):
        folder=exp.prepare(exp.BASE/'benchmark'/'fixture',exp.BASE/'benchmark'/'task_fixture.json',
                           self.root/'experiments',baseline='no_hook')
        exp.execute(folder,execute_run=True)
        result=report(folder);base,opt=result['runs']
        self.assertEqual(result['successful_trials'],2)
        for row in (base,opt):
            self.assertEqual(row['final_validation']['tests'],504)
            self.assertEqual(row['initial_validation']['failures'],3)
        self.assertEqual(base['hook_process_count'],0)
        self.assertEqual(base['hook_process_elapsed_ms'],0)
        self.assertEqual(base['hook_errors'],0)
        self.assertEqual(base['bash_hook_invocations'],0)
        self.assertEqual(base['compression']['compressions'],0)
        self.assertEqual(base['original_stdout_chars'],base['visible_stdout_chars'])
        self.assertGreater(base['visible_stdout_chars'],30000)
        self.assertFalse((folder/'trials'/'001-baseline'/'tg-data').exists())
        self.assertEqual(opt['hook_process_count'],2)
        self.assertEqual(opt['bash_hook_invocations'],2)
        self.assertEqual(opt['hook_errors'],0)
        self.assertGreater(opt['compression']['compressions'],0)
        self.assertLess(opt['visible_stdout_chars'],opt['original_stdout_chars'])
        local=result['local_wall_clock_pairs'][0]
        self.assertEqual(local['status'],'quality_passed')
        self.assertEqual(local['metrics']['hook_process_count']['optimized_minus_baseline'],2)
        self.assertEqual(result['actual_task_savings_claim'],'not_established')
        with (folder/'report.csv').open(encoding='utf-8',newline='') as stream:
            rows=list(csv.DictReader(stream))
        self.assertEqual(rows[0]['baseline_mode'],'no_hook')
        self.assertEqual(rows[0]['hook_process_count'],'0')
        self.assertIn('Baseline mode: no_hook',(folder/'report.md').read_text(encoding='utf-8'))
        for row in (base,opt):
            source=self.root/(row['arm']+'.json')
            exp.write_json(source,{'object':'chat.completion','id':'synthetic-'+row['arm'],'model':'deepseek-flash',
                                   'usage':{'prompt_tokens':10,'completion_tokens':2}})
            exp.attach_usage(folder,row['trial_id'],source,'deepseek','synthetic','complete_task',1)
        with_usage=report(folder)
        self.assertEqual(with_usage['usage_comparison']['pairs'][0]['baseline_mode'],'no_hook')
        self.assertEqual(with_usage['usage_comparison']['eligible_pairs'],1)

    def test_live_adapter_mode_and_timing_contract_use_only_fake_process(self):
        folder=self.prepare();seen=[]
        def fake(argv,cwd,env,timeout,stdin=None):
            result={'exit_code':0,'timed_out':False,'outputs_truncated':False,'elapsed_ms':1,'stderr':''}
            if '--version' in argv:return {**result,'stdout':'fixture-cli'}
            seen.append(json.loads(Path(argv[argv.index('--settings')+1]).read_text()))
            path=cwd/'arithmetic.py';path.write_text(path.read_text().replace('//','/'),encoding='utf-8')
            session=argv[argv.index('--session-id')+1]
            return {**result,'stdout':json.dumps({'session_id':session,'is_error':False})}
        with patch('tokenguard.experiment.resolve_claude',return_value={'executable':'fixture-cli'}),patch('tokenguard.experiment.run_process',side_effect=fake):
            exp.execute(folder,'claude',True,True)
        result=report(folder)
        self.assertEqual(result['successful_trials'],2)
        self.assertEqual(seen[0],{});self.assertIn('hooks',seen[1])
        self.assertTrue(all(r['hook_process_elapsed_ms']=='unavailable' for r in result['runs']))

    def test_missing_mock_measurements_cannot_claim_success_or_zero_overhead(self):
        folder=self.prepare()
        def fake(argv,cwd,env,timeout,stdin=None):
            path=cwd/'arithmetic.py';path.write_text(path.read_text().replace('//','/'),encoding='utf-8')
            return {'exit_code':0,'timed_out':False,'outputs_truncated':False,'elapsed_ms':1,'stderr':'',
                    'stdout':json.dumps({'session_id':argv[3],'is_error':False})}
        with patch('tokenguard.experiment.run_process',side_effect=fake):
            exp.execute(folder,execute_run=True)
        result=report(folder)
        self.assertEqual(result['successful_trials'],0)
        for row in result['runs']:
            self.assertEqual(row['execution_error'],'invalid_mock_metrics')
            self.assertEqual(row['hook_process_elapsed_ms'],'unavailable')
            self.assertTrue(row['tests_passed'])

    def test_result_baseline_mode_tamper_is_rejected(self):
        folder=self.prepare();trial=exp.load_experiment(folder)['trials'][0]
        exp.write_json(folder/'trials'/trial['trial_id']/'result.json',{**trial,'baseline_mode':'disabled_hook'})
        with self.assertRaises(ValueError):report(folder)

    def test_nested_windows_report_path_does_not_lengthen_validation_worktree(self):
        folder=self.prepare()
        output=self.root/'nested-reports'
        while len(str(output))<212:
            output=output/'nested-report-segment'
        output.mkdir(parents=True)
        original=(self.project/'arithmetic.py').read_bytes()
        result=validate(folder/'snapshot',self.project,exp.load_experiment(folder)['task'],output)
        self.assertEqual(result['tests'],1)
        self.assertEqual(result['failures'],1)
        self.assertFalse(result['passed'])
        self.assertTrue((output/'validation.stdout.txt').is_file())
        self.assertEqual((self.project/'arithmetic.py').read_bytes(),original)


if __name__=='__main__':
    unittest.main()
