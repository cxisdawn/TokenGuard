import os
import tempfile
import unittest
import json
import sys
from pathlib import Path
from unittest.mock import patch
from tokenguard.core import compress
from tokenguard.hook import handle, _handle
from tokenguard.storage import save_original, artifact_path, read_lines, grep_lines
from tokenguard.audit import summary, events
from installer import install


def pytest_log(end='===== 400 passed in 0.4s =====\n'):
    return ''.join(f'test_x.py::TestC::test_x[{i} with spaces] PASSED [ 40%]\n' for i in range(400))+end


def event(text=None, session='s1'):
    return {'hook_event_name': 'PostToolUse', 'session_id': session, 'tool_name': 'Bash',
            'tool_input': {'command': 'pytest -v'}, 'tool_response': {'stdout': text or pytest_log(),
            'stderr': 'ERROR must remain', 'interrupted': False, 'isImage': False, 'extra': 42}}


class TestV04(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'TOKENGUARD_DATA_DIR': self.folder.name, 'TOKENGUARD_SOURCE': 'manual', 'TOKENGUARD_ENABLED':'1'})
        self.env.start()
    def tearDown(self):
        self.env.stop()
        self.folder.cleanup()
    def test_ansi_and_space_parameters(self):
        raw = pytest_log().replace('PASSED', '\x1b[32mPASSED\x1b[0m')
        self.assertLess(len(compress(raw, command='pytest')[0]), 1000)
    def test_unknown_and_important_statuses_preserved(self):
        ending = 'x SKIPPED\ny ERROR\nz XFAIL\nw XPASS\ncustom diagnostic\nE assert false\n===== 1 failed, 400 passed in 1.0s =====\n'
        self.assertIn(ending, compress(pytest_log(ending), command='pytest')[0])
    def test_pytest_details_never_classified_as_progress(self):
        ending = '=== FAILURES ===\ntest_x.py::test_fake PASSED\n===== 1 failed, 400 passed in 1.0s =====\n'
        self.assertIn(ending, compress(pytest_log(ending), command='pytest')[0])
    def test_incomplete_pytest_unchanged(self):
        raw = pytest_log('')
        self.assertEqual(compress(raw, command='pytest')[0], raw)
    def test_unittest(self):
        raw = ''.join(f'test_{i} (test_mod.C.test_{i}) ... ok\n' for i in range(400))
        raw += 'test_bug (test_mod.C.test_bug) ... FAIL\nFAIL: test_bug\nAssertionError: 3 != 5\nRan 401 tests in 0.01s\nFAILED (failures=1)\n'
        new, strategy = compress(raw, command='python -m unittest -v')
        self.assertEqual(strategy, 'unittest_pass_summary')
        self.assertIn('AssertionError: 3 != 5', new)
        self.assertIn('... FAIL', new)
    def test_pip(self):
        raw = 'Collecting package==1.0\n'+'  ━━━━━━━━━━━━━━━━━━━━ 1/2 MB 5 MB/s\n'*500+'Successfully installed package-1.0\nWARNING: resolver message\n'
        new, strategy = compress(raw, command='python -m pip install package')
        self.assertEqual(strategy, 'pip_progress')
        self.assertIn('package-1.0', new)
        self.assertIn('resolver message', new)
    def test_search(self):
        raw = ''.join(f'project/module_{i}.py\n' for i in range(1000))
        self.assertEqual(compress(raw, command='rg --files')[1], 'file_search')
        self.assertEqual(compress(raw+'unknown ???\n', command='rg --files')[0], raw+'unknown ???\n')
        self.assertEqual(compress(raw, command='rg needle')[0], raw)
    def test_patch_formats(self):
        for header in ['--- a/x\n+++ b/x\n', '@@@ -1 +1 @@@\n', 'GIT binary patch\n', 'ExceptionGroup Traceback\n']:
            raw = header+'same\n'*2000
            self.assertEqual(compress(raw)[0], raw)
        self.assertEqual(compress('same\n'*2000, command='git diff --stat')[0], 'same\n'*2000)
    def test_hook_preserves_shape(self):
        e = event()
        out = handle(e)['hookSpecificOutput']['updatedToolOutput']
        self.assertEqual({k:v for k,v in out.items() if k!='stdout'}, {k:v for k,v in e['tool_response'].items() if k!='stdout'})
        self.assertEqual(e['tool_response']['stdout'], pytest_log())
    def test_malformed_events(self):
        for e in [None, [], 'x', {'tool_name':'Bash','tool_input':None,'tool_response':{}},
                  {**event(), 'tool_response': 'x'}, {**event(), 'hook_event_name':'PostToolUseFailure'}]:
            self.assertIsNone(handle(e))
    def test_disabled(self):
        with patch.dict(os.environ, {'TOKENGUARD_ENABLED':'0'}):
            self.assertIsNone(handle(event()))
    def test_fail_open_storage_and_compressor(self):
        for target in ['tokenguard.hook.save_original', 'tokenguard.hook.compress', 'tokenguard.hook.append']:
            with patch(target, side_effect=OSError('secret must not print')):
                self.assertIsNone(handle(event()))
    def test_v03_net_negative_regression(self):
        raw = 'pulse\n'*87 + 'tail:' + 'x'*(3000-6*87-6) + '\n'
        self.assertEqual(len(raw),3000)
        e=event(raw)
        e['tool_input']['command']='python noisy.py'
        self.assertEqual(_handle(e)[1], 'insufficient_saving_after_metadata')
    def test_net_savings_gate(self):
        with patch('tokenguard.hook.compress', return_value=('a'*2500, 'mock')):
            self.assertIsNone(handle(event('x'*3000)))
    def test_recovery_stats_and_experiment(self):
        with patch.dict(os.environ, {'TOKENGUARD_EXPERIMENT_ID':'exp1'}):
            handle(event())
        row = summary('s1', 'exp1')['records'][0]
        self.assertEqual(row['source'], 'manual')
        grep_lines(row['artifact_id'], 'test_x', limit=1, compression_id=row['compression_id'])
        report = summary('s1', 'exp1')
        self.assertEqual(report['records'][0]['recovery_count'], 1)
        self.assertGreater(report['returned_chars'], 0)
        self.assertEqual(summary('other')['compressions'], 0)
        self.assertEqual(report['actual_cost'], 'unavailable')
    def test_shared_artifact_no_false_attribution(self):
        handle(event(session='s1')); handle(event(session='s2'))
        row = summary('s1')['records'][0]
        read_lines(row['artifact_id'], 1, 1)
        self.assertEqual(summary('s1')['recovery_count'], 0)
        self.assertEqual(summary()['recovery_count'], 1)
        read_lines(row['artifact_id'], 1, 1, compression_id=row['compression_id'])
        self.assertEqual(summary('s1')['recovery_count'], 1)
    def test_bounded_huge_line(self):
        identifier = save_original('x'*100000+'\n')
        self.assertLessEqual(len(read_lines(identifier, 1, 1, max_chars=100)), 100)
        self.assertLessEqual(len(grep_lines(identifier, 'xxx', max_chars=100)), 100)
    def test_grep_context_overlap(self):
        identifier = save_original('zero\nneedle\nneedle\nthree\nfour\n')
        result = grep_lines(identifier,'needle',context=1)
        self.assertEqual(result.count('2: needle'), 1)
        self.assertIn('4: three', result)
    def test_crlf_unicode_artifact_exact(self):
        raw = '中文\r\n第二行\n'
        identifier = save_original(raw)
        self.assertEqual(artifact_path(identifier).read_bytes(), raw.encode('utf-8'))
        self.assertEqual(read_lines(identifier,1,2), raw)
    def test_validation(self):
        for bad in ['../x', 'A'*64, 'x'*64, 'a'*64+'\n']:
            with self.assertRaises(ValueError): artifact_path(bad)
        identifier = save_original('x\n')
        for kwargs in [{'context':6}, {'limit':0}, {'max_chars':12001}]:
            with self.assertRaises(ValueError): grep_lines(identifier, 'x', **kwargs)
        with self.assertRaises(ValueError): read_lines(identifier,1,101)
        with self.assertRaises(ValueError): read_lines(identifier,1,1,compression_id='missing')
    def test_corrupt_artifact_fail_open(self):
        raw = pytest_log()
        identifier = save_original(raw)
        artifact_path(identifier).write_text('corrupted')
        self.assertIsNone(handle(event(raw)))
    def test_malformed_audit_tolerated(self):
        Path(self.folder.name, 'audit-v04.jsonl').write_text('{broken\n[]\n')
        self.assertEqual(summary()['malformed_rows'], 2)
    def test_diagnostics_no_raw_command(self):
        with patch.dict(os.environ, {'TOKENGUARD_DIAGNOSTICS':'1'}):
            handle(event('short'))
        rows,_ = events('diagnostics-v04.jsonl')
        self.assertEqual(rows[0]['status'], 'below_threshold')
        self.assertNotIn('command', rows[0])
    def test_installer_idempotent_preserves_permissions_and_other_hooks(self):
        project = Path(self.folder.name)/'project with spaces'
        target = project/'.claude/settings.local.json'
        target.parent.mkdir(parents=True)
        old = {'permissions': {'allow': ['Read']}, 'hooks': {'PostToolUse': [{'matcher':'Bash', 'hooks':[{'type':'command','command':'echo keep'}]}]}}
        target.write_text(json.dumps(old))
        install(project, Path(sys.executable)); install(project, Path(sys.executable))
        data = json.loads(target.read_text())
        self.assertEqual(data['permissions'], old['permissions'])
        self.assertEqual(len(data['hooks']['PostToolUse']), 2)
        install(project,Path(sys.executable),True)
        self.assertEqual(json.loads(target.read_text())['hooks'], old['hooks'])
        self.assertEqual(len(list(target.parent.glob('*.backup-*'))), 3)

if __name__ == '__main__': unittest.main()
