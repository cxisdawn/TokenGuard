"""No paid calls: resolution and process metadata are fixtures in these tests."""
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tokenguard import experiment as exp
from tokenguard import experiment_preflight as probe
from tokenguard.claude_runtime import ClaudeExecutableError,resolve_claude


class NativeResolution(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()/'path with spaces'
        self.root.mkdir()

    def test_direct_native_executable(self):
        exe=self.root/'claude.exe';exe.write_bytes(b'fixture')
        with patch('tokenguard.claude_runtime.shutil.which',return_value=str(exe)):
            result=resolve_claude(windows=True)
        self.assertEqual(result['resolution'],'direct_executable')
        self.assertEqual(result['executable'],str(exe))

    def test_npm_native_resolved_without_reading_or_executing_wrapper(self):
        native=self.root/'node_modules'/'@anthropic-ai'/'claude-code'/'bin'/'claude.exe'
        native.parent.mkdir(parents=True);native.write_bytes(b'fixture')
        for name in ('claude.cmd','claude.ps1','claude'):
            with self.subTest(name=name),patch('tokenguard.claude_runtime.shutil.which',return_value=str(self.root/name)),patch('pathlib.Path.read_text',side_effect=AssertionError('Do not read launchers')),patch('subprocess.Popen',side_effect=AssertionError('No process during resolution')):
                self.assertEqual(resolve_claude(windows=True)['executable'],str(native))

    def test_missing_executable_or_legacy_js_only_rejected(self):
        with patch('tokenguard.claude_runtime.shutil.which',return_value=None),self.assertRaises(ClaudeExecutableError) as caught:
            resolve_claude(windows=True)
        self.assertEqual(caught.exception.code,'executable_not_found')
        package=self.root/'node_modules'/'@anthropic-ai'/'claude-code'
        package.mkdir(parents=True);(package/'cli.js').write_text('fixture',encoding='utf-8')
        with patch('tokenguard.claude_runtime.shutil.which',return_value=str(self.root/'claude.cmd')),self.assertRaises(ClaudeExecutableError) as caught:
            resolve_claude(windows=True)
        self.assertEqual(caught.exception.code,'npm_native_binary_missing_or_linked')

    def test_arbitrary_shell_launcher_cannot_use_npm_fallback(self):
        with patch('tokenguard.claude_runtime.shutil.which',return_value=str(self.root/'custom.cmd')),self.assertRaises(ClaudeExecutableError):
            resolve_claude(windows=True)

    def test_posix_executable_has_no_windows_layout_requirement(self):
        with patch('tokenguard.claude_runtime.shutil.which',return_value=str(self.root/'claude')):
            self.assertEqual(resolve_claude(windows=False)['resolution'],'direct_executable')


class TaskPreflight(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.folder=exp.prepare(exp.BASE/'benchmark'/'fixture',exp.BASE/'benchmark'/'task_fixture.json',self.root,1)
        self.commands=[]
        self.hook={'configuration':'passed','configured_process':'passed','compression':'passed',
                   'artifact_integrity':'passed','host_acceptance':'unverified'}
        self.validation={'passed':False,'exit_code':1,'tests':504,'failures':3,'errors':0,
                         'timed_out':False,'outputs_truncated':False}
        self.addCleanup(patch.stopall)
        patch('tokenguard.experiment_preflight.resolve_claude',return_value={'executable':'fixture-native','resolution':'fixture'}).start()
        patch('tokenguard.experiment_preflight.run_process',side_effect=self.process).start()
        self.hook_mock=patch('tokenguard.experiment_preflight.doctor.check',return_value=self.hook).start()
        self.validation_mock=patch('tokenguard.experiment_preflight.validate',return_value=self.validation).start()
        patch.dict(os.environ,{'CLAUDE_CODE_SIMPLE':'0','CLAUDE_CODE_SAFE_MODE':'0'}).start()

    def process(self,argv,cwd,env,timeout):
        self.commands.append(argv)
        text='2.1.181 (Claude Code)' if argv[1:]==['--version'] else '\n'.join(probe.REQUIRED_FLAGS)
        return {'exit_code':0,'timed_out':False,'outputs_truncated':False,'stdout':text,'stderr':''}

    def test_preflight_only_help_and_version_does_not_consume_sessions(self):
        result=probe.check(self.folder)
        self.assertTrue(result['local_checks_passed'])
        self.assertFalse((self.folder/'started.json').exists())
        self.assertFalse((self.folder/'runtime.json').exists())
        self.assertEqual(result['model_api_calls_started_by_runner'],0)
        self.assertEqual(result['actual_task_tokens'],'unavailable')
        self.assertIn('unverified',result['authentication'])
        self.assertEqual(self.commands,[['fixture-native','--version'],['fixture-native','--help'],
                                        ['fixture-native','--max-turns','1','--help']])
        self.hook_mock.assert_called_once_with(self.folder/'control')
        self.assertEqual(self.validation_mock.call_args.args[:2],(self.folder/'snapshot',self.folder/'snapshot'))
        self.assertTrue(Path(result['report_path']).exists())

    def test_unresolved_cli_reports_failure_without_raw_error_content(self):
        with patch('tokenguard.experiment_preflight.resolve_claude',side_effect=ClaudeExecutableError('executable_not_found')):
            result=probe.check(self.folder)
        self.assertFalse(result['local_checks_passed'])
        self.assertEqual(result['cli']['error_code'],'executable_not_found')
        self.assertEqual(self.commands,[])

    def test_missing_flag_or_timed_out_help_rejected(self):
        for output in ({'exit_code':0,'timed_out':False,'outputs_truncated':False,'stdout':'unrelated help'},
                       {'exit_code':0,'timed_out':True,'outputs_truncated':False,'stdout':' '.join(probe.REQUIRED_FLAGS)},
                       {'exit_code':0,'timed_out':False,'outputs_truncated':True,'stdout':' '.join(probe.REQUIRED_FLAGS)}):
            with self.subTest(output=output),patch('tokenguard.experiment_preflight.run_process',return_value=output):
                self.assertFalse(probe.check(self.folder)['checks']['cli_flags'])

    def test_already_started_or_altered_trial_is_not_ready(self):
        (self.folder/'started.json').write_text('{}',encoding='utf-8')
        target=self.folder/'trials'/'001-baseline'/'worktree'/'arithmetic.py'
        target.write_text('changed',encoding='utf-8')
        result=probe.check(self.folder)
        self.assertFalse(result['checks']['fresh_experiment'])
        self.assertFalse(result['checks']['initial_worktrees_unchanged'])
        self.assertFalse(result['local_checks_passed'])

    def test_hook_failure_and_disabled_hook_environment_are_not_ready(self):
        self.hook['compression']='no_replacement'
        with patch.dict(os.environ,{'CLAUDE_CODE_SAFE_MODE':'1'}):
            result=probe.check(self.folder)
        self.assertFalse(result['checks']['configured_hook'])
        self.assertFalse(result['checks']['hook_environment_enabled'])

    def test_wrong_test_count_or_collection_error_rejected(self):
        for update in ({'tests':503},{'errors':1},{'timed_out':True},{'outputs_truncated':True}):
            with self.subTest(update=update),patch('tokenguard.experiment_preflight.validate',return_value={**self.validation,**update}):
                self.assertFalse(probe.check(self.folder)['checks']['initial_task'])

    def test_failed_cli_resolution_does_not_burn_fresh_sessions(self):
        with patch('tokenguard.experiment.resolve_claude',side_effect=ClaudeExecutableError('executable_not_found')),self.assertRaises(ClaudeExecutableError):
            exp.execute(self.folder,'claude',True,True)
        self.assertFalse((self.folder/'started.json').exists())
        self.assertFalse((self.folder/'execution.lock').exists())
        failed_version={'exit_code':1,'timed_out':False,'outputs_truncated':False,'stdout':'','stderr':''}
        with patch('tokenguard.experiment.resolve_claude',return_value={'executable':'fixture-native'}),patch('tokenguard.experiment.run_process',return_value=failed_version),self.assertRaises(ValueError):
            exp.execute(self.folder,'claude',True,True)
        self.assertFalse((self.folder/'started.json').exists())
        self.assertFalse((self.folder/'execution.lock').exists())


if __name__=='__main__':
    unittest.main()
