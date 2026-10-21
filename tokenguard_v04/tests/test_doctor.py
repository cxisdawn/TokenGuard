import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from installer import install
from tokenguard.audit import append, diagnostics
from tokenguard.doctor import check

ROOT = Path(__file__).resolve().parent.parent

class TestDoctor(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.project = self.folder/'project with spaces'
        self.env = patch.dict(os.environ, {'TOKENGUARD_DATA_DIR':str(self.folder/'live-data')})
        self.env.start()
    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()
    def test_installed_probe_is_isolated_and_no_host_claim(self):
        settings=install(self.project,Path(sys.executable))
        original=settings.read_bytes()
        append({'event':'hook_invocation','status':'below_threshold','session_id':'actual-session'},'diagnostics-v04.jsonl')
        log=self.folder/'live-data/diagnostics-v04.jsonl'
        original_log=log.read_bytes()
        report=check(self.project)
        for key in ('configuration','configured_process','compression','artifact_integrity'):
            self.assertEqual(report[key],'passed',report)
        self.assertEqual(report['original_chars'],9629)
        self.assertEqual(report['probe_source'],'manual')
        self.assertEqual(report['host_acceptance'],'unverified')
        self.assertEqual(report['model_api_calls'],0)
        self.assertEqual(settings.read_bytes(),original)
        self.assertEqual(log.read_bytes(),original_log)
        self.assertFalse((self.folder/'live-data/audit-v04.jsonl').exists())
    def test_missing_config_does_not_start_process(self):
        with patch('tokenguard.doctor.subprocess.run') as run:
            self.assertEqual(check(self.project)['configuration'],'missing_project_settings')
            run.assert_not_called()
    def test_shell_expression_refused(self):
        settings=install(self.project,Path(sys.executable))
        data=json.loads(settings.read_text())
        data['hooks']['PostToolUse'][0]['hooks'][0]['command'] += ' && echo private'
        settings.write_text(json.dumps(data))
        with patch('tokenguard.doctor.subprocess.run') as run:
            report=check(self.project)
            self.assertEqual(report['configuration'],'unsupported_command_shape')
            self.assertNotIn('private',json.dumps(report))
            run.assert_not_called()
    def test_duplicate_and_async_hooks_refused(self):
        settings=install(self.project,Path(sys.executable))
        data=json.loads(settings.read_text())
        entry=data['hooks']['PostToolUse'][0]
        entry['hooks'][0]['async']=True
        settings.write_text(json.dumps(data))
        self.assertEqual(check(self.project)['configuration'],'not_synchronous_command')
        data['hooks']['PostToolUse'].append(entry)
        settings.write_text(json.dumps(data))
        self.assertEqual(check(self.project)['configuration'],'multiple_v04_hooks')
    def test_unrecognized_interpreter_refused(self):
        settings=install(self.project,Path(sys.executable))
        data=json.loads(settings.read_text())
        data['hooks']['PostToolUse'][0]['hooks'][0]['command']='"C:/other/python.exe" "'+(ROOT/'tokenguard/hook.py').as_posix()+'" --tokenguard-v04 --source claude_hook'
        settings.write_text(json.dumps(data))
        with patch('tokenguard.doctor.subprocess.run') as run:
            self.assertEqual(check(self.project)['configuration'],'interpreter_differs_from_running_python')
            run.assert_not_called()
    def test_bad_json_no_sensitive_echo(self):
        path=self.project/'.claude/settings.local.json'
        path.parent.mkdir(parents=True)
        path.write_text('{private broken json')
        report=check(self.project)
        self.assertEqual(report['error_type'],'JSONDecodeError')
        self.assertNotIn('private',json.dumps(report))
    def test_diagnostics_filters_legacy_rows_and_caps_output(self):
        append({'event':'hook_invocation','session_id':'old'},'diagnostics-v04.jsonl')
        for n in range(8):
            append({'event':'hook_invocation','session_id':'s1','experiment_id':'e1','stdout_chars':n},'diagnostics-v04.jsonl')
        append({'event':'hook_invocation','session_id':'s2','experiment_id':'e1'},'diagnostics-v04.jsonl')
        report=diagnostics('s1','e1',limit=3)
        self.assertEqual(report['matching_invocations'],8)
        self.assertEqual(report['omitted_invocations'],5)
        self.assertEqual([r['stdout_chars'] for r in report['invocations']],[5,6,7])
        self.assertEqual(len(diagnostics(experiment_id='e1')['invocations']),9)
        self.assertEqual(len(diagnostics(session_id='old')['invocations']),1)
        for limit in (0,101):
            with self.assertRaises(ValueError): diagnostics(limit=limit)
    def test_diagnostics_cli_filter_and_doctor_exit(self):
        append({'event':'hook_invocation','session_id':'one','experiment_id':'trial'},'diagnostics-v04.jsonl')
        append({'event':'hook_invocation','session_id':'two'},'diagnostics-v04.jsonl')
        result=subprocess.run([sys.executable,str(ROOT/'tg.py'),'diagnostics','--session','one','--experiment','trial','--limit','1'],capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(result.stdout)['matching_invocations'],1)
        failed=subprocess.run([sys.executable,str(ROOT/'tg.py'),'doctor','--project',str(self.project)],capture_output=True,text=True)
        self.assertEqual(failed.returncode,1)
        self.assertEqual(json.loads(failed.stdout)['configuration'],'missing_project_settings')
        install(self.project,Path(sys.executable))
        passed=subprocess.run([sys.executable,str(ROOT/'tg.py'),'doctor','--project',str(self.project)],capture_output=True,text=True)
        self.assertEqual(passed.returncode,0,passed.stderr)
        self.assertEqual(json.loads(passed.stdout)['compression'],'passed')

if __name__=='__main__': unittest.main()
