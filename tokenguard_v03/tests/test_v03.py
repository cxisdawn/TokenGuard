import unittest
from tokenguard.core import compress

class TestStructured(unittest.TestCase):
    def test_pytest_progress(self):
        raw = '================ test session starts ================\n' + ''.join(f'test_mod.py::test_case[{i}] PASSED [ 42%]\n' for i in range(400)) + '================ 400 passed in 0.4s ================\n'
        got, kind = compress(raw, command='python -m pytest -v')
        self.assertEqual(kind, 'pytest_pass_summary')
        self.assertIn('400 pytest PASSED lines hidden', got)
        self.assertIn('400 passed in 0.4s', got)
    def test_pytest_fail_detail_preserved(self):
        raw = 'header\n' + ''.join(f'test_mod.py::test_case[{i}] PASSED [ 50%]\n' for i in range(350)) + 'test_mod.py::test_bug FAILED [ 99%]\nE assert 3 == 5\n====== 1 failed, 350 passed in 0.4s ======\n'
        got, _ = compress(raw, command='pytest -v')
        self.assertIn('test_mod.py::test_bug FAILED', got)
        self.assertIn('E assert 3 == 5', got)
    def test_pytest_not_claimed_for_unrelated(self):
        text = 'A\nB\n' * 1800
        self.assertEqual(compress(text, command='pytest -v')[0], text)
    def test_diff_is_untouched(self):
        raw = 'diff --git a/f b/f\n@@ -1,1 +1,1 @@\n' + 'same\n'*1500
        self.assertEqual(compress(raw, command='git diff')[0], raw)
    def test_traceback_untouched(self):
        raw = 'Traceback (most recent call last):\n' + '  File "f", line 1\n'*1000
        self.assertEqual(compress(raw, command='python file.py')[0], raw)
    def test_build_warnings(self):
        raw = 'Warning: deprecated option\n' * 600 + 'BUILD COMPLETE\n'
        got, kind = compress(raw, command='build project')
        self.assertEqual(kind, 'adjacent_duplicates')
        self.assertIn('599 identical', got)
        self.assertIn('Warning: deprecated option', got)
    def test_generic_error_protected(self):
        raw = 'FAIL E42\n'*500 + 'ok\n'*1000
        got, _ = compress(raw)
        self.assertEqual(got.count('FAIL E42'), 500)
    def test_short_passthrough(self):
        self.assertEqual(compress('hello',command='pytest -v')[0],'hello')

if __name__ == '__main__': unittest.main()
