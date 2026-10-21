import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from tokenguard.audit import events
from tokenguard.hook import handle
from live_check import LINE, REPETITIONS, MARKER

ROOT = Path(__file__).resolve().parent.parent

class TestLiveDiagnostics(unittest.TestCase):
    def test_fixed_fixture_subprocess_and_compression(self):
        result = subprocess.run([sys.executable,str(ROOT/'live_check.py')],capture_output=True,text=True,encoding='utf-8',check=True)
        expected = LINE*REPETITIONS+MARKER
        self.assertEqual(result.stdout,expected)
        self.assertEqual(result.stderr,'')
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {
            'TOKENGUARD_DATA_DIR':d,'TOKENGUARD_ENABLED':'1','TOKENGUARD_DIAGNOSTICS':'1',
            'TOKENGUARD_SOURCE':'manual','TOKENGUARD_EXPERIMENT_ID':'test-diagnostics'}):
            output = handle({'hook_event_name':'PostToolUse','session_id':'manual-fixed-fixture',
                             'tool_use_id':'test-tool','tool_name':'Bash','tool_input':{'command':'python live_check.py'},
                             'tool_response':{'stdout':result.stdout,'stderr':'','interrupted':False,'isImage':False}})
            self.assertIn(MARKER,output['hookSpecificOutput']['updatedToolOutput']['stdout'])
            row=events('diagnostics-v04.jsonl')[0][0]
            self.assertEqual(row['status'],'compressed')
            self.assertEqual(row['stdout_chars'],len(expected))
            self.assertEqual(row['stderr_chars'],0)
            self.assertEqual(row['experiment_id'],'test-diagnostics')
            self.assertEqual(row['tool_use_id'],'test-tool')
    def test_short_stdout_large_stderr_explained_without_log_content(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {
            'TOKENGUARD_DATA_DIR':d,'TOKENGUARD_ENABLED':'1','TOKENGUARD_DIAGNOSTICS':'1'}):
            self.assertIsNone(handle({'tool_name':'Bash','tool_input':{'command':'python secret-command.py'},
                                     'tool_response':{'stdout':'ok\n','stderr':'private'*2000}}))
            row=events('diagnostics-v04.jsonl')[0][0]
            self.assertEqual(row['status'],'below_threshold')
            self.assertEqual(row['stdout_chars'],3)
            self.assertEqual(row['stderr_chars'],14000)
            self.assertNotIn('private',json.dumps(row))
            self.assertNotIn('secret-command',json.dumps(row))
    def test_malformed_response_still_diagnosed(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {
            'TOKENGUARD_DATA_DIR':d,'TOKENGUARD_DIAGNOSTICS':'1','TOKENGUARD_ENABLED':'1'}):
            self.assertIsNone(handle({'tool_name':'Bash','tool_input':None,'tool_response':[]}))
            row=events('diagnostics-v04.jsonl')[0][0]
            self.assertEqual(row['status'],'invalid_shape')
            self.assertIsNone(row['stdout_chars'])
            self.assertEqual(row['response_type'],'list')

if __name__=='__main__': unittest.main()
