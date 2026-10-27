"""Windows aliases must not be mistaken for actual filesystem links."""
from contextlib import nullcontext
import ctypes
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tokenguard.experiment_snapshot import changes
from tokenguard.swebench_quality import candidate_patch
import test_swebench_quality


class TestSnapshotPaths(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Actual Windows 8.3 API contract')
    def test_real_short_alias_retains_patch_and_independent_grade(self):
        with tempfile.TemporaryDirectory(prefix='TokenGuard path alias evidence ') as temporary:
            get_short = ctypes.WinDLL('kernel32', use_last_error=True).GetShortPathNameW
            get_short.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
            get_short.restype = ctypes.c_uint32
            buffer = ctypes.create_unicode_buffer(32768)
            length = get_short(temporary, buffer, len(buffer))
            self.assertGreater(length, 0)
            self.assertLess(length, len(buffer))
            alias = Path(buffer.value)
            if str(alias).casefold() == str(Path(temporary).resolve()).casefold():
                self.skipTest('Filesystem does not expose an 8.3 alias for this directory')
            # Exercise the exact producer -> archive -> analysis chain that
            # failed under Windows hosted-runner TEMP aliases, using real paths.
            case = test_swebench_quality.TestQualityPilot(
                'test_analysis_separates_valid_patch_from_turn_limit_exit')
            with patch('test_swebench_quality.tempfile.TemporaryDirectory',
                       return_value=nullcontext(str(alias))):
                case.test_analysis_separates_valid_patch_from_turn_limit_exit()
            record = json.loads((alias / 'experiment.json').read_text())
            candidate = alias / 'trials/001-optimized/worktree'
            short_patch, short_check = candidate_patch(alias, record, candidate)
            long_patch, long_check = candidate_patch(alias.resolve(), record, candidate.resolve())
            self.assertTrue(short_check['only_allowed_changes'])
            self.assertEqual(short_check, long_check)
            self.assertEqual(short_patch, long_patch)
            self.assertTrue(short_patch)

    def test_linked_root_is_rejected_before_reading_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(Path, 'is_symlink', return_value=True), \
                 patch.object(Path, 'read_bytes', side_effect=AssertionError('Unsafe source was read')):
                with self.assertRaisesRegex(ValueError, 'links'):
                    changes(Path(temporary), {'code.py': '0' * 64}, ['code.py'])

    def test_reparse_root_is_rejected_before_reading_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(Path, 'is_symlink', return_value=False), \
                 patch.object(Path, 'lstat', return_value=SimpleNamespace(st_file_attributes=0x400)), \
                 patch.object(Path, 'read_bytes', side_effect=AssertionError('Unsafe source was read')):
                with self.assertRaisesRegex(ValueError, 'reparse'):
                    changes(Path(temporary), {'code.py': '0' * 64}, ['code.py'])

    def test_linked_descendant_remains_disallowed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / 'code.py').write_bytes(b'VALUE = 1\n')
            digest = hashlib.sha256(b'VALUE = 1\n').hexdigest()
            with patch.object(Path, 'is_symlink', lambda path: path.name == 'code.py'):
                check = changes(root, {'code.py': digest}, ['code.py'])
            self.assertFalse(check['only_allowed_changes'])
            self.assertEqual(check['missing_files'], ['code.py'])
            self.assertEqual(check['unexpected_files'], ['code.py'])
