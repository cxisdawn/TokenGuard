"""Task-level contracts: fresh snapshots, no paid defaults, independent quality gates."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tokenguard import experiment as exp
from tokenguard.experiment_report import report
from tokenguard.experiment_snapshot import changes,read_spec,relative_file,snapshot
from tokenguard.experiment_validation import run_process,validate


class TaskExperiments(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.project=self.root/'project';self.project.mkdir()
        (self.project/'arithmetic.py').write_text('def divide(a,b):\n    return a // b\n',encoding='utf-8')
        (self.project/'test_arithmetic.py').write_text('from arithmetic import divide\ndef test_fraction():\n    assert divide(3,2)==1.5\n',encoding='utf-8')
        self.raw={'schema_version':1,'task_id':'division','provider':'deepseek','model':'deepseek-flash',
                  'prompt':'Fix only arithmetic.py and run pytest.',
                  'files':['arithmetic.py','test_arithmetic.py'],'editable_files':['arithmetic.py'],
                  'test_files':['test_arithmetic.py'],'expected_tests':1,'timeout_seconds':30,
                  'mock_edits':[{'path':'arithmetic.py','old':'a // b','new':'a / b'}]}
        self.spec=self.root/'task.json'
        self.save_spec()

    def save_spec(self):
        self.spec.write_text(json.dumps(self.raw),encoding='utf-8')

    def prepare(self,rounds=1):
        return exp.prepare(self.project,self.spec,self.root/'experiments',rounds)

    def test_private_noncanonical_and_windows_paths_rejected(self):
        for name in ('../a','/a','a\\b','a//b','a/./b','.env','.env.local','.claude/settings.json',
                     '.claude./settings.json','secrets.json','x.key','x.jsonl','CON.txt','a:stream','a.','a '):
            with self.subTest(name=name),self.assertRaises(ValueError):relative_file(name)

    def test_spec_disjoint_files_case_collisions_and_bounds(self):
        for update in ({'editable_files':['test_arithmetic.py']},{'files':['x.py','X.py']},
                       {'timeout_seconds':True},{'max_turns':101},{'expected_tests':0},
                       {'mock_edits':[{'path':'test_arithmetic.py','old':'a','new':'b'}]}):
            with self.subTest(update=update),self.assertRaises(ValueError):read_spec({**self.raw,**update})

    def test_snapshot_is_allowlisted_and_source_remains_unchanged(self):
        (self.project/'.env').write_text('do-not-copy',encoding='utf-8')
        payload,hashes=snapshot(self.project,self.raw['files'])
        self.assertEqual(set(payload),set(self.raw['files']))
        folder=self.prepare()
        self.assertFalse((folder/'snapshot'/'.env').exists())
        self.assertEqual(hashes,exp.load_experiment(folder)['files_sha256'])
        self.assertIn('a // b',(self.project/'arithmetic.py').read_text())

    def test_missing_binary_and_oversized_snapshot_rejected(self):
        for data in (b'\0',b'x'*(1024*1024+1)):
            (self.project/'arithmetic.py').write_bytes(data)
            with self.assertRaises(ValueError):self.prepare()
        (self.project/'arithmetic.py').unlink()
        with self.assertRaises(ValueError):self.prepare()

    def test_prepare_three_rounds_alternates_order_and_sessions(self):
        folder=self.prepare(3);record=exp.load_experiment(folder)
        self.assertEqual([t['arm'] for t in record['trials']],['baseline','optimized','optimized','baseline','baseline','optimized'])
        self.assertEqual(len({t['session_id'] for t in record['trials']}),6)
        for trial in record['trials']:
            _,hashes=snapshot(folder/'trials'/trial['trial_id']/'worktree',self.raw['files'])
            self.assertEqual(hashes,record['files_sha256'])
        settings=json.loads((folder/'settings.json').read_text())
        self.assertEqual(set(settings),{'hooks'})

    def test_pytest_mode_is_pinned_in_settings_config_and_mock_process(self):
        for mode in ('indexed','off'):
            with self.subTest(mode=mode):
                folder=exp.prepare(self.project,self.spec,self.root/'output',baseline='no_hook',pytest_mode=mode)
                record=exp.load_experiment(folder)
                self.assertEqual(record['config']['pytest_mode'],mode)
                settings=json.loads((folder/'settings.json').read_text(encoding='utf-8'))
                self.assertIn('--pytest-mode '+mode,settings['hooks']['PostToolUse'][0]['hooks'][0]['command'])
                self.assertEqual(exp.execute(folder)['pytest_mode'],mode)
                result=exp.execute(folder,driver='mock',execute_run=True)
                self.assertEqual(result['completed_trials'],2)
                for trial in record['trials']:
                    metrics=json.loads((folder/'trials'/trial['trial_id']/'driver/worker-metrics.json').read_text())
                    self.assertEqual(metrics['pytest_mode'],mode)
        with self.assertRaises(ValueError):
            exp.prepare(self.project,self.spec,self.root/'output',pytest_mode='invalid')

    def test_output_inside_original_project_rejected(self):
        with self.assertRaises(ValueError):exp.prepare(self.project,self.spec,self.project/'output')

    def test_snapshot_or_config_mutation_rejected(self):
        for target in ('snapshot/arithmetic.py','settings.json','task.json'):
            folder=self.prepare()
            (folder/target).write_text('{}',encoding='utf-8')
            with self.subTest(target=target),self.assertRaises(ValueError):exp.load_experiment(folder)

    def test_duplicate_session_rejected(self):
        folder=self.prepare();record=exp.load_experiment(folder)
        record['trials'][1]['session_id']=record['trials'][0]['session_id']
        exp.write_json(folder/'experiment.json',record)
        with self.assertRaises(ValueError):exp.load_experiment(folder)

    def test_dry_run_never_launches_process_or_marks_started(self):
        folder=self.prepare()
        with patch('subprocess.Popen',side_effect=AssertionError('No process allowed')):
            plan=exp.execute(folder,'claude')
        self.assertTrue(plan['dry_run'])
        self.assertEqual(plan['paid_sessions'],2)
        self.assertFalse((folder/'started.json').exists())
        for cmd in plan['commands']:
            self.assertNotIn('--resume',cmd);self.assertNotIn('--continue',cmd)
            self.assertNotIn('--permission-mode',cmd);self.assertNotIn('--dangerously-skip-permissions',cmd)
            self.assertIn('--strict-mcp-config',cmd)
            self.assertEqual(cmd[cmd.index('--setting-sources')+1],'user')

    def test_paid_gates_reject_before_any_process(self):
        folder=self.prepare(2)
        with patch('subprocess.Popen',side_effect=AssertionError('No paid process allowed')):
            for allow,limit in ((False,4),(True,2),(True,True)):
                with self.subTest(allow=allow,limit=limit),self.assertRaises(ValueError):
                    exp.execute(folder,'claude',True,allow,limit)
        self.assertFalse((folder/'started.json').exists())

    def test_preedited_trial_rejected_before_execution(self):
        folder=self.prepare()
        (folder/'trials'/'001-baseline'/'worktree'/'arithmetic.py').write_text('changed',encoding='utf-8')
        with self.assertRaises(ValueError):exp.execute(folder,execute_run=True)
        self.assertFalse((folder/'started.json').exists())

    def test_runtime_drift_rejected(self):
        folder=self.prepare()
        with patch('tokenguard.experiment.sys.version','different runtime'),self.assertRaises(ValueError):
            exp.execute(folder,execute_run=True)

    def test_protected_test_changes_detected_and_cannot_replace_pristine_tests(self):
        folder=self.prepare();record=exp.load_experiment(folder)
        candidate=folder/'trials'/'001-baseline'/'worktree'
        (candidate/'test_arithmetic.py').write_text('def test_fake():\n    assert True\n',encoding='utf-8')
        check=changes(candidate,record['files_sha256'],self.raw['editable_files'])
        self.assertFalse(check['only_allowed_changes'])
        output=self.root/'validation';output.mkdir()
        actual=validate(folder/'snapshot',candidate,read_spec(self.raw),output)
        self.assertFalse(actual['passed']);self.assertEqual(actual['failures'],1)
        self.assertTrue(actual['uses_pristine_protected_files'])

    def test_unexpected_files_and_missing_editable_rejected(self):
        _,hashes=snapshot(self.project,self.raw['files'])
        (self.project/'unexpected.py').write_text('extra',encoding='utf-8')
        (self.project/'arithmetic.py').unlink()
        check=changes(self.project,hashes,self.raw['editable_files'])
        self.assertEqual(check['missing_files'],['arithmetic.py'])
        self.assertEqual(check['unexpected_files'],['unexpected.py'])
        self.assertFalse(check['only_allowed_changes'])

    def test_expected_test_count_is_enforced(self):
        folder=self.prepare();candidate=folder/'trials'/'001-baseline'/'worktree'
        path=candidate/'arithmetic.py';path.write_text(path.read_text().replace('//','/'),encoding='utf-8')
        spec=read_spec({**self.raw,'expected_tests':2})
        output=self.root/'validation';output.mkdir()
        result=validate(folder/'snapshot',candidate,spec,output)
        self.assertEqual(result['exit_code'],0);self.assertFalse(result['passed'])

    def test_process_timeout_and_output_memory_limit(self):
        timed=run_process([sys.executable,'-c','import time; time.sleep(10)'],self.root,dict(os.environ),1)
        self.assertTrue(timed['timed_out'])
        bounded=run_process([sys.executable,'-c','print("x"*1000)'],self.root,dict(os.environ),5,output_limit=100)
        self.assertEqual(len(bounded['stdout']),100);self.assertTrue(bounded['outputs_truncated'])

    def test_session_mismatch_is_failed_not_a_success(self):
        folder=self.prepare()
        fake={'exit_code':0,'timed_out':False,'elapsed_ms':1,'outputs_truncated':False,
              'stdout':json.dumps({'session_id':'wrong','is_error':False}),'stderr':''}
        with patch('tokenguard.experiment.run_process',return_value=fake):
            exp.execute(folder,execute_run=True)
        result=report(folder)
        self.assertEqual(result['successful_trials'],0)
        self.assertTrue(all(r['execution_error']=='ValueError' for r in result['runs']))

    def test_claude_adapter_contract_with_fake_process_only(self):
        folder=self.prepare();seen=[]
        def fake(argv,cwd,env,timeout,stdin=None):
            result={'exit_code':0,'timed_out':False,'elapsed_ms':1,'outputs_truncated':False,'stderr':''}
            if '--version' in argv:
                return {**result,'stdout':'fixture-cli 1.0'}
            seen.append((argv,stdin,env['TOKENGUARD_ENABLED'],env['TOKENGUARD_DATA_DIR']))
            source=cwd/'arithmetic.py'
            source.write_text(source.read_text().replace('//','/'),encoding='utf-8')
            session=argv[argv.index('--session-id')+1]
            return {**result,'stdout':json.dumps({'session_id':session,'is_error':False,
                                                'total_cost_usd':999,'usage':{'input_tokens':999}})}
        with patch('tokenguard.claude_runtime.shutil.which',return_value=sys.executable),patch('tokenguard.experiment.run_process',side_effect=fake):
            exp.execute(folder,'claude',True,True)
        result=report(folder)
        self.assertEqual(result['successful_trials'],2)
        self.assertEqual([item[2] for item in seen],['0','1'])
        self.assertEqual(len({item[3] for item in seen}),2)
        self.assertTrue(all(item[1]==self.raw['prompt'] for item in seen))
        for row in result['runs']:
            self.assertEqual(row['total_task_tokens'],'unavailable')
            self.assertEqual(row['actual_billed_cost'],'unavailable')
            self.assertIsNone(row['tool_calls'])

    def test_snapshot_links_rejected_before_file_read(self):
        with patch('pathlib.Path.is_symlink',return_value=True),self.assertRaises(ValueError):
            snapshot(self.project,self.raw['files'])

    def test_not_run_report_keeps_usage_unavailable(self):
        folder=self.prepare();result=report(folder)
        self.assertEqual(result['completed_trials'],0)
        self.assertEqual(result['usage_comparison'],'unavailable')
        for name in ('report.json','report.csv','report.md'):self.assertTrue((folder/name).exists())
        self.assertTrue(all(r['total_task_tokens']=='unavailable' for r in result['runs']))

    def test_complete_mock_bugfix_compression_usage_and_no_session_reuse(self):
        fixture=exp.BASE/'benchmark'/'fixture'
        for name in self.raw['files']:
            (self.project/name).write_bytes((fixture/name).read_bytes())
        self.raw['expected_tests']=504;self.raw['timeout_seconds']=60;self.save_spec()
        original=(self.project/'arithmetic.py').read_bytes()
        folder=self.prepare();executed=exp.execute(folder,execute_run=True)
        self.assertEqual(executed['completed_trials'],2)
        result=report(folder)
        self.assertEqual(result['successful_trials'],2)
        base,opt=result['runs']
        for row in result['runs']:
            self.assertEqual(row['initial_validation']['failures'],3)
            self.assertEqual(row['final_validation']['tests'],504)
            self.assertEqual(row['final_validation']['failures'],0)
            self.assertEqual(row['bash_hook_invocations'],2)
        self.assertEqual(base['compression']['compressions'],0)
        self.assertEqual(base['hook_process_count'],2)
        self.assertEqual(base['hook_errors'],0)
        self.assertEqual(base['original_stdout_chars'],base['visible_stdout_chars'])
        self.assertGreaterEqual(base['hook_process_elapsed_ms'],0)
        self.assertEqual(opt['hook_process_count'],2)
        self.assertGreater(opt['compression']['compressions'],0)
        self.assertEqual((self.project/'arithmetic.py').read_bytes(),original)
        visible=(folder/'trials'/'001-optimized'/'driver'/'before.visible.txt').read_text(encoding='utf-8')
        self.assertIn('FAILED',visible)
        self.assertIn('test_fraction',visible)
        self.assertIn('TokenGuard artifact=',visible)
        with self.assertRaises(ValueError):exp.execute(folder,execute_run=True)
        with self.assertRaises(ValueError):exp.attach_usage(folder,'001-baseline',self.spec,'deepseek','provider_export','complete_task',1)
        for row in result['runs']:
            source=self.root/(row['trial_id']+'-usage.json')
            source.write_text(json.dumps({'object':'chat.completion','id':'fixture-'+row['trial_id'],
                                         'model':'deepseek-flash','usage':{'prompt_tokens':100,'completion_tokens':10}}),encoding='utf-8')
            exp.attach_usage(folder,row['trial_id'],source,'deepseek','synthetic','complete_task',1)
        first_bundle=folder/'trials'/'001-baseline'/'measurement.json'
        forged=json.loads(first_bundle.read_text())
        forged['actual_billed_cost']={'amount':'999','currency':'USD'}
        forged['cache_hit_ratio']=1
        exp.write_json(first_bundle,forged)
        compared=report(folder)
        self.assertEqual(compared['usage_comparison']['eligible_pairs'],1)
        self.assertEqual(compared['runs'][0]['usage_evidence_kind'],'synthetic')
        self.assertEqual(compared['runs'][0]['total_task_tokens'],110)
        self.assertEqual(compared['runs'][0]['actual_billed_cost'],'unavailable')
        self.assertEqual(compared['runs'][0]['cache_hit_ratio'],'unavailable')
        with self.assertRaises(ValueError):exp.attach_usage(folder,'001-baseline',source,'deepseek','synthetic','complete_task',1)
        bundle=folder/'trials'/'001-baseline'/'measurement.json'
        value=json.loads(bundle.read_text());value['manifest']['session_id']='unrelated-session'
        exp.write_json(bundle,value)
        with self.assertRaises(ValueError):report(folder)


if __name__=='__main__':
    unittest.main()
