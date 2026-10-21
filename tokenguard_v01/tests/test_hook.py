import unittest
from tokenguard.hook import compress

class TestCompression(unittest.TestCase):
    def test_short_untouched(self):
        self.assertEqual(compress("PASS\n"*20), "PASS\n"*20)
    def test_repeated_compressed(self):
        s="PASS\n"*2000
        result=compress(s)
        self.assertLess(len(result), len(s))
        self.assertIn("identical adjacent", result)
    def test_failure_line_preserved(self):
        s="PASS\n"*1000 + "FAIL test_xyz AssertionError\n"
        self.assertIn("FAIL test_xyz AssertionError", compress(s))
    def test_nonadjacent_no_compression(self):
        s=("a\nb\n"*1200)
        self.assertEqual(compress(s), s)

if __name__ == "__main__":
    unittest.main()
