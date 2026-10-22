"""CLI byte/encoding contract, beyond in-process storage recovery."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tokenguard.storage import save_original


class TestRecoveryCLI(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='tg-cli-')
        self.env = patch.dict(os.environ, {'TOKENGUARD_DATA_DIR': self.folder.name,
                                         'PYTHONIOENCODING': 'ascii'})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.folder.cleanup()

    def invoke(self, mode, identifier, *args):
        result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'tg.py'),
                                 mode, identifier, *args], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b'')
        audit = Path(self.folder.name) / 'audit-v04.jsonl'
        last = json.loads(audit.read_text(encoding='utf-8').splitlines()[-1])
        self.assertEqual(last['returned_chars'], len(result.stdout.decode('utf-8')))
        return result.stdout

    def test_show_preserves_utf8_and_each_line_ending(self):
        for ending in ('\n', '\r\n', '\r', ''):
            with self.subTest(ending=repr(ending)):
                expected = '原文 café 😀' + ending
                identifier = save_original('first\r\n' + expected)
                self.assertEqual(self.invoke('show', identifier, '--start', '2', '--count', '1'),
                                 expected.encode('utf-8'))

    def test_grep_preserves_utf8_and_each_line_ending(self):
        for ending in ('\n', '\r\n', '\r', ''):
            with self.subTest(ending=repr(ending)):
                text = '原文 café 😀' + ending
                identifier = save_original('first\r\n' + text)
                self.assertEqual(self.invoke('grep', identifier, 'café', '--context', '0', '--limit', '1'),
                                 ('2: ' + text).encode('utf-8'))

    def test_show_preserves_mixed_endings_without_final_newline(self):
        text = 'alpha\r\nβeta\ngamma\rlast'
        identifier = save_original(text)
        self.assertEqual(self.invoke('show', identifier, '--start', '1', '--count', '4'), text.encode('utf-8'))

    def test_show_ascii_line_endings_match_audited_characters(self):
        text = 'alpha\r\nbeta\ngamma\rlast'
        identifier = save_original(text)
        self.assertEqual(self.invoke('show', identifier, '--start', '1', '--count', '4'), text.encode('ascii'))
