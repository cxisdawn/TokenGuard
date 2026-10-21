import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from tokenguard.core import compress
from tokenguard.hook import handle
from tokenguard.storage import save_original, read_lines, grep_lines, artifact_path

class TestV02(unittest.TestCase):
    def test_compress_repeats(self):
        v = 'HEARTBEAT ok\n' * 1500 + 'Diagnostics complete\n'
        new, reason = compress(v)
        self.assertIn('1499 identical', new)
        self.assertIn('Diagnostics complete', new)
        self.assertEqual(reason, 'adjacent_duplicates')

    def test_errors_preserved(self):
        v = 'PASS\n' * 1500 + 'FAIL: AssertionError\n'
        new, _ = compress(v)
        self.assertIn('FAIL: AssertionError', new)

    def test_no_changes_on_short_or_alternating(self):
        self.assertEqual(compress('small\n')[0], 'small\n')
        v = 'A\nB\n' * 1000
        self.assertEqual(compress(v)[0], v)

    def test_hook_recovery_and_audit(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {'TOKENGUARD_DATA_DIR': d}):
            v = 'HEARTBEAT ok\n' * 1500 + 'Done\n'
            result = handle({'tool_name': 'Bash', 'tool_response': {'stdout': v, 'stderr':'', 'interrupted':False}, 'session_id':'s1'})
            out = result['hookSpecificOutput']['updatedToolOutput']['stdout']
            self.assertIn('tg.py', out)
            artifact_id = out.split('artifact=')[1][:64]
            self.assertEqual(read_lines(artifact_id, 1, 1), 'HEARTBEAT ok\n')
            self.assertIn('1501: Done', grep_lines(artifact_id, 'Done'))
            with open(os.path.join(d, 'audit-v04.jsonl'), encoding='utf-8') as f:
                self.assertEqual(sum(json.loads(line).get('event') == 'compression' for line in f), 1)

    def test_fail_open_unmatched(self):
        self.assertIsNone(handle({'tool_name': 'Read', 'tool_response': {'stdout':'x'*4000}}))
        self.assertIsNone(handle({'tool_name': 'Bash', 'tool_response': {'stdout':'short'}}))

    def test_reject_path_traversal(self):
        with self.assertRaises(ValueError):
            artifact_path('../bad')

if __name__ == '__main__':
    unittest.main()
