"""Recovery must reach every original character with bounded, advancing cursors."""
import os
import tempfile
import unittest
from unittest.mock import patch

from tokenguard.audit import events
from tokenguard.storage import artifact_path, grep_lines, read_lines, save_original


class TestLongLineRecovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='tg-long-line-')
        self.env = patch.dict(os.environ, {'TOKENGUARD_DATA_DIR': self.temp.name,
                                          'TOKENGUARD_INDEX_ENABLED': '1'})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def last(self):
        return events()[0][-1]

    def recover_all(self, raw, budget=137):
        identifier = save_original(raw)
        cursor = (1, 0)
        recovered = []
        for _ in range(1000):
            result = read_lines(identifier, cursor[0], 100,
                                char_offset=cursor[1], max_chars=budget)
            row = self.last()
            self.assertLessEqual(len(result), budget)
            self.assertEqual(row['returned_chars'], len(result))
            if not row['truncated']:
                if result != '(no matches)\n':
                    recovered.append(result)
                self.assertEqual(''.join(recovered), raw)
                self.assertEqual(artifact_path(identifier).read_bytes(), raw.encode('utf-8'))
                return
            text, marker = result.rsplit('\n[TokenGuard: retrieval limit reached;', 1)
            recovered.append(text)
            following = row['next_start_line'], row['next_start_char']
            self.assertGreater(following, cursor)
            self.assertIn(f'next_start_line={following[0]}', marker)
            self.assertIn(f'next_start_char={following[1]}', marker)
            cursor = following
        self.fail('Recovery did not terminate')

    def test_long_unicode_line_and_mixed_newlines_reconstruct_exactly(self):
        self.recover_all(('原文😀\u2028\x0b' * 1000) + '\r\nsecond\rthird\nlast')

    def test_every_newline_boundary_is_lossless(self):
        for ending in ('\n', '\r\n', '\r', ''):
            for budget in range(100, 121):
                with self.subTest(ending=repr(ending), budget=budget):
                    raw = 'x' * 20 + ending + 'y' * 240
                    self.recover_all(raw, budget)

    def test_seeded_mixed_line_lengths_reconstruct_without_skips_or_duplicates(self):
        import random
        generator = random.Random(104729)
        for sample in range(20):
            fragments = []
            for _ in range(8):
                fragments.append(generator.choice(('a', '原', '😀', '\u2028')) *
                                 generator.choice((0, 1, 19, 45, 100)))
                fragments.append(generator.choice(('\n', '\r\n', '\r')))
            raw = ''.join(fragments) + generator.choice(('', 'final'))
            with self.subTest(sample=sample):
                self.recover_all(raw, generator.randrange(100, 151))

    def test_tail_keyword_is_visible_with_bounded_window(self):
        raw = 'x' * 15000 + 'IMPORTANT_FAILURE' + 'y' * 80 + '\r\n'
        identifier = save_original(raw)
        result = grep_lines(identifier, 'important_failure', context=0, max_chars=200)
        self.assertIn('IMPORTANT_FAILURE', result)
        self.assertLessEqual(len(result), 200)
        self.assertIn('[char ', result)
        self.assertEqual(self.last()['windowed_matches'], 1)

    def test_long_context_cannot_displace_actual_match(self):
        identifier = save_original('x' * 15000 + '\nIMPORTANT_FAILURE\nafter\n')
        result = grep_lines(identifier, 'IMPORTANT_FAILURE', context=1, max_chars=200)
        self.assertIn('IMPORTANT_FAILURE', result)
        self.assertIn('omitted 1 context line', result)
        self.assertLessEqual(len(result), 200)
        self.assertEqual(self.last()['omitted_context_lines'], 1)

    def test_unreachable_cursor_with_disabled_or_failed_index_is_rejected(self):
        identifier = save_original('x' * 1000 + 'NEEDLE')
        for index_mode in ('disabled', 'failed'):
            for offset in (256, 300, 900):
                with self.subTest(index_mode=index_mode, offset=offset):
                    with patch('tokenguard.storage.MAX_SCAN', 256), \
                            patch.dict(os.environ, {'TOKENGUARD_INDEX_ENABLED':
                                                   '0' if index_mode == 'disabled' else '1'}), \
                            patch('tokenguard.storage.ensure_index', side_effect=PermissionError):
                        with self.assertRaisesRegex(ValueError, 'scan limit'):
                            read_lines(identifier, 1, 1, char_offset=offset, max_chars=200)
                        with self.assertRaisesRegex(ValueError, 'scan limit'):
                            grep_lines(identifier, 'NEEDLE', char_offset=offset, max_chars=200)

    def test_keyword_larger_than_window_budget_is_rejected_instead_of_partially_returned(self):
        pattern = 'A' * 199 + 'Z'
        identifier = save_original('x' * 15000 + pattern + 'y' * 1000)
        for budget in (100, 150):
            with self.subTest(budget=budget), self.assertRaisesRegex(ValueError, 'max_chars'):
                grep_lines(identifier, pattern, context=0, max_chars=budget)
        result = grep_lines(identifier, pattern, context=0, max_chars=400)
        self.assertIn(pattern, result)

    def test_marker_cannot_split_a_later_matching_record(self):
        identifier = save_original('needle\n' * 100)
        result = grep_lines(identifier, 'needle', context=0, max_chars=100)
        body = result.split('\n[TokenGuard:', 1)[0]
        self.assertTrue(body)
        for line in body.splitlines():
            self.assertTrue(line.endswith(': needle'), line)
        row = self.last()
        following = grep_lines(identifier, 'needle', start=row['next_start_line'],
                               char_offset=row['next_start_char'], context=0, limit=1)
        self.assertTrue(following.startswith(f"{row['next_start_line']}: needle\n"))

    def test_match_limit_reports_cursor_until_remaining_matches_are_retrieved(self):
        identifier = save_original('NEEDLE first\nNEEDLE second\nNEEDLE third\n')
        first = grep_lines(identifier, 'NEEDLE', context=0, limit=1)
        self.assertTrue(first.startswith('1: NEEDLE first\n'))
        self.assertTrue(self.last()['truncated'])
        self.assertEqual((self.last()['next_start_line'], self.last()['next_start_char']), (2, 0))
        self.assertIn('next_start_line=2', first)
        second = grep_lines(identifier, 'NEEDLE', start=2, context=0, limit=1)
        self.assertTrue(second.startswith('2: NEEDLE second\n'))
        self.assertTrue(self.last()['truncated'])
        self.assertEqual((self.last()['next_start_line'], self.last()['next_start_char']), (3, 0))
        self.assertEqual(grep_lines(identifier, 'NEEDLE', start=3, context=0, limit=1),
                         '3: NEEDLE third\n')
        self.assertFalse(self.last()['truncated'])

    def test_match_limit_lookahead_is_bounded_and_respects_requested_end(self):
        identifier = save_original('NEEDLE\n' + 'x' * 1000000)
        first = grep_lines(identifier, 'NEEDLE', context=0, limit=1)
        self.assertIn('next_start_line=2', first)
        self.assertLessEqual(self.last()['scanned_chars'], len('NEEDLE\n')+1)
        self.assertEqual(grep_lines(identifier, 'NEEDLE', end=1, context=0, limit=1), '1: NEEDLE\n')
        self.assertFalse(self.last()['truncated'])

    def test_match_limit_keeps_requested_tail_context_before_continuation(self):
        identifier = save_original('NEEDLE first\r\ncontext one\r\nNEEDLE second\r\ncontext two\r\n')
        result = grep_lines(identifier, 'NEEDLE', context=1, limit=1)
        self.assertTrue(result.startswith('1: NEEDLE first\r\n2: context one\r\n'))
        self.assertEqual((self.last()['next_start_line'], self.last()['next_start_char']), (3, 0))
        self.assertEqual(grep_lines(identifier, 'NEEDLE', start=3, context=1, limit=1),
                         '3: NEEDLE second\r\n4: context two\r\n')

    def test_keyword_budget_boundary_returns_complete_match_or_explicit_error(self):
        from tokenguard.storage import RecoveryLimitError
        for pattern in ('a', 'needle', 'ß' * 90 + 'END', 'A' * 199 + 'Z'):
            identifier = save_original('x' * 2000 + pattern + 'y' * 1000)
            for budget in (100, 150, 200, 300, 400):
                with self.subTest(pattern_length=len(pattern), budget=budget):
                    try:
                        result = grep_lines(identifier, pattern.upper(), context=0, max_chars=budget)
                    except RecoveryLimitError as exc:
                        self.assertIn('max_chars', str(exc))
                    else:
                        self.assertIn(pattern, result)
                        self.assertLessEqual(len(result), budget)

    def test_unicode_casefold_window_maps_back_to_original_characters(self):
        for phrase, pattern in [('Straße', 'STRASSE'), ('ß', 's'), ('İstanbul', 'i\u0307stanbul')]:
            with self.subTest(phrase=phrase):
                identifier = save_original('😀ß' * 4000 + phrase + 'z' * 200)
                result = grep_lines(identifier, pattern, context=0, max_chars=200)
                self.assertIn(phrase, result)
                self.assertLessEqual(len(result), 200)

    def test_scan_limited_show_end_line_returns_continuation(self):
        identifier = save_original('x' * 400)
        with patch('tokenguard.storage.MAX_SCAN', 256):
            result = read_lines(identifier, 1, 1, max_chars=1000)
        self.assertTrue(self.last()['truncated'])
        self.assertIn('next_start_char=', result)

    def test_keyword_crossing_scan_boundary_is_recoverable(self):
        identifier = save_original('x' * 253 + 'Straße' + 'y' * 300)
        with patch('tokenguard.storage.MAX_SCAN', 256), \
                patch('tokenguard.line_index.CHUNK', 64), \
                patch('tokenguard.line_index.BYTE_STRIDE', 64):
            first = grep_lines(identifier, 'STRASSE', start=1, end=1, context=0, max_chars=200)
            self.assertNotIn('Straße', first)
            self.assertTrue(self.last()['truncated'])
            line, column = self.last()['next_start_line'], self.last()['next_start_char']
            self.assertGreater((line, column), (1, 0))
            result = grep_lines(identifier, 'STRASSE', start=line, end=1,
                                char_offset=column, context=0, max_chars=200)
            self.assertIn('Straße', result)

    def test_scan_boundary_after_cr_preserves_lf_and_next_line(self):
        raw = 'x' * 255 + '\r\nnext\n'
        identifier = save_original(raw)
        with patch('tokenguard.storage.MAX_SCAN', 256), \
                patch('tokenguard.line_index.CHUNK', 64), \
                patch('tokenguard.line_index.BYTE_STRIDE', 64):
            first = read_lines(identifier, 1, 100, max_chars=1000)
            row = self.last()
            self.assertTrue(row['truncated'])
            prefix = first.split('\n[TokenGuard:', 1)[0]
            rest = read_lines(identifier, row['next_start_line'], 100,
                              char_offset=row['next_start_char'], max_chars=1000)
            self.assertEqual(prefix + rest, raw)

    def test_tiny_budget_rejects_nonadvancing_truncated_marker(self):
        identifier = save_original('x' * 1000)
        for budget in (1, 20, 60):
            with self.subTest(budget=budget), self.assertRaisesRegex(ValueError, 'max_chars'):
                read_lines(identifier, 1, 1, max_chars=budget)

    def test_character_cursor_at_end_of_completed_line_normalizes(self):
        identifier = save_original('x' * 256 + '\nnext\n')
        with patch('tokenguard.storage.MAX_SCAN', 257):
            result = grep_lines(identifier, 'absent', context=0, max_chars=200)
        self.assertIn('next_start_line=2', result)
        self.assertEqual((self.last()['next_start_line'], self.last()['next_start_char']), (2, 0))

    def test_char_offset_validation_and_empty_suffix(self):
        identifier = save_original('原文😀\r\nnext')
        self.assertEqual(read_lines(identifier, 1, 1, char_offset=2), '😀\r\n')
        self.assertEqual(read_lines(identifier, 1, 1, char_offset=4), '\n')
        for offset in (-1, 6):
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                read_lines(identifier, 1, 1, char_offset=offset)
