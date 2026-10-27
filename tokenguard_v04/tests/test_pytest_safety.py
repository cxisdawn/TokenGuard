"""Semantic preservation checks against adversarial and real pytest output."""
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

from tokenguard.core import compress
from tokenguard.pytest_index import PassedIndex, base_node


def progress(count: int = 100, newline: str = '\n') -> str:
    return ''.join(f'tests/test_sample.py::test_case[{i}] PASSED [100%]{newline}'
                   for i in range(count))


class TestPytestSemanticSafety(unittest.TestCase):
    def test_warning_payload_is_preserved_and_never_indexed(self):
        warning = '  /tmp/test_sample.py:5: UserWarning: critical::contract PASSED\n'
        tail = ('================ warnings summary ================\n' + warning
                + '    warnings.warn(message)\n'
                + '================ 100 passed, 1 warning in 0.5s ================\n')
        raw = progress() + tail
        for mode in ('compact', 'indexed'):
            with self.subTest(mode=mode):
                output, strategy = compress(raw, command='pytest -v', pytest_mode=mode)
                self.assertTrue(strategy.startswith('pytest_pass_summary'))
                self.assertTrue(output.endswith(tail))
                self.assertNotIn('contract": 1 passed', output)

    def test_all_report_sections_preserve_pass_shaped_payloads(self):
        for section in ('=== warnings summary ===', '=== PASSES ===',
                        '=== slowest 10 durations ===', '=== custom plugin report ===',
                        '--- Captured stdout call ---', '--- Captured stderr setup ---',
                        '--- Captured log teardown ---', 'Captured stdout call:',
                        '=== FAILURES ===', '___ failure details ___'):
            for mode in ('compact', 'indexed'):
                with self.subTest(section=section, mode=mode):
                    tail = (section + '\n'
                            'tests/test_sample.py::test_diagnostic[do not hide] PASSED\n'
                            '100 passed in 0.5s\n')
                    output, _ = compress(progress() + tail, command='pytest -v', pytest_mode=mode)
                    self.assertTrue(output.endswith(tail))
                    self.assertNotIn('"tests/test_sample.py::test_diagnostic"', output)

    def test_footer_prevents_following_commands_from_becoming_test_progress(self):
        tail = ('100 passed in 0.5s\n'
                'tests/test_sample.py::test_following_command PASSED\n')
        for mode in ('compact', 'indexed'):
            output, _ = compress(progress() + tail, command='pytest -v; python report.py',
                                 pytest_mode=mode)
            self.assertTrue(output.endswith(tail))

    def test_unknown_nodes_and_diagnostic_prefixes_stay_visible(self):
        unknown = ('CRITICAL::contract PASSED\n'
                   '  tests/test_sample.py::test_indented PASSED\n'
                   '/tmp/test_sample.py:5: UserWarning: tests/test_sample.py::test_x PASSED\n'
                   'custom.yaml::test_plugin PASSED\n'
                   'tests/test_sample.py::Class[custom]::test_method[value] PASSED\n'
                   'note\ftests/test_sample.py::test_control PASSED\n')
        for mode in ('compact', 'indexed'):
            output, _ = compress(progress() + unknown + '100 passed in 0.5s\n',
                                 command='pytest -v', pytest_mode=mode)
            self.assertIn(unknown, output)

    def test_start_header_allows_progress_and_later_header_starts_new_section(self):
        start = '================ test session starts ================\n'
        tail = start + 'tests/test_sample.py::test_following PASSED\n100 passed in 0.5s\n'
        raw = start + 'collected 100 items\n' + progress() + tail
        for mode in ('compact', 'indexed'):
            output, strategy = compress(raw, command='pytest -v', pytest_mode=mode)
            self.assertTrue(strategy.startswith('pytest_pass_summary'))
            self.assertIn(start + 'collected 100 items\n', output)
            self.assertTrue(output.endswith(tail))

    def test_newline_variants_preserve_warning_sections_and_diff_guard(self):
        for newline in ('\n', '\r\n', '\r'):
            tail = newline.join(('=== warnings summary ===',
                                 'tests/test_sample.py::test_payload PASSED',
                                 '100 passed in 0.5s', ''))
            raw = progress(newline=newline) + tail
            for mode in ('compact', 'indexed'):
                with self.subTest(newline=repr(newline), mode=mode):
                    output, _ = compress(raw, command='pytest -v', pytest_mode=mode)
                    self.assertTrue(output.endswith(tail))
                    diff = 'build output' + newline + '@@ -1,1000 +1,1000 @@' + newline
                    diff += (' unchanged' + newline) * 1000
                    self.assertEqual(compress(diff, command='make', pytest_mode=mode),
                                     (diff, 'protected_diff_or_traceback'))

    def test_parameter_delimiters_are_not_node_delimiters(self):
        raw = ''.join(f'tests/test_sample.py::TestClass::test_case[value::{i}] PASSED\n'
                      for i in range(100)) + '100 passed in 0.5s\n'
        output, strategy = compress(raw, command='pytest -v', pytest_mode='indexed')
        self.assertEqual(strategy, 'pytest_pass_summary_indexed')
        self.assertIn('"tests/test_sample.py::TestClass::test_case": 100 passed; raw lines 1-100',
                      output)
        self.assertNotIn('[value::', output)

    def test_parameter_brackets_paths_and_uncertain_custom_nodes(self):
        index = PassedIndex()
        for number, parameter in enumerate(('first::value', 'second::value', 'unbalanced['), 1):
            index.add(f'tests[parent]/test_sample.py::TestClass::test_case[{parameter}]', number)
        self.assertIn('"tests[parent]/test_sample.py::TestClass::test_case": 3 passed; raw lines 1-3',
                      index.render())
        self.assertEqual(base_node(r'C:\project dir\test_sample.py::test_case[x::y]'),
                         r'C:\project dir\test_sample.py::test_case')
        for node in ('test.yaml::test_case', 'test.py::Class[plugin]::test_case[x]',
                     'test.py::test_case[unterminated', 'test.py:5: warning::test_case'):
            self.assertIsNone(base_node(node))
        custom = 'test.py::Class[plugin]::test_case[x]'
        index.add(custom, 4)
        self.assertIn('"' + custom + '"', index.render())

    def test_real_pytest_warning_and_captured_pass_report(self):
        # This is the actual pytest formatter, including warning locations and
        # captured stdout from successful tests; no model or network is involved.
        fixture = textwrap.dedent('''\
            import warnings
            import pytest

            @pytest.mark.parametrize('value', range(100))
            def test_case(value):
                if value == 0:
                    warnings.warn('critical::contract PASSED', UserWarning)
                    print('tests/test_sample.py::test_captured_payload PASSED')
        ''')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'test_safety_fixture.py'
            path.write_text(fixture, encoding='utf-8')
            configuration = Path(folder) / 'pytest.ini'
            configuration.write_text('[pytest]\n', encoding='utf-8')
            env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTHONDONTWRITEBYTECODE='1')
            result = subprocess.run([sys.executable, '-B', '-m', 'pytest', '-v', '-rP',
                                     '-c', str(configuration), '-p', 'no:cacheprovider', '--color=no',
                                     str(path)], capture_output=True, text=True,
                                    encoding='utf-8', env=env, timeout=30, cwd=folder)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('UserWarning: critical::contract PASSED', result.stdout)
        captured = 'tests/test_sample.py::test_captured_payload PASSED'
        self.assertIn(captured, result.stdout)
        self.assertIn('100 passed, 1 warning', result.stdout)
        for mode in ('compact', 'indexed'):
            output, strategy = compress(result.stdout, command='pytest -v -rP', pytest_mode=mode)
            self.assertTrue(strategy.startswith('pytest_pass_summary'), (mode, strategy, result.stdout))
            self.assertIn('UserWarning: critical::contract PASSED', output)
            self.assertIn(captured, output)
            self.assertIn('100 passed, 1 warning', output)

    def test_real_live_output_cannot_masquerade_as_passed_progress(self):
        # A single fake node replaces the real node interrupted by print(), so
        # checking the final PASSED count alone cannot detect this corruption.
        fixture = textwrap.dedent(r'''
            import pytest

            @pytest.mark.parametrize('value', range(100))
            def test_case(value):
                if value == 0:
                    print('\ntests/test_sample.py::test_critical_payload PASSED')
        ''')
        variants = (
            (['-s'], '', 'pytest -v -s'),
            (['--capture=no'], '', 'pytest -v --capture=no'),
            (['--capture=tee-sys'], '', 'pytest -v --capture=tee-sys'),
            ([], 'addopts = -s\n', 'pytest -v'),
            ([], 'addopts = --capture=tee-sys\n', 'pytest -v'),
        )
        payload = 'tests/test_sample.py::test_critical_payload PASSED'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'test_safety_fixture.py'
            path.write_text(fixture, encoding='utf-8')
            configuration = Path(folder) / 'pytest.ini'
            env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
                       PYTHONDONTWRITEBYTECODE='1', PYTEST_ADDOPTS='')
            for flags, options, command in variants:
                configuration.write_text('[pytest]\n' + options, encoding='utf-8')
                result = subprocess.run(
                    [sys.executable, '-B', '-m', 'pytest', '-v', *flags,
                     '-c', str(configuration), '-p', 'no:cacheprovider',
                     '--color=no', str(path)], capture_output=True, text=True,
                    encoding='utf-8', env=env, timeout=30, cwd=folder)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(payload, result.stdout)
                self.assertIn('100 passed', result.stdout)
                apparent_passes = re.findall(
                    r'^\S+\.py::.+ PASSED(?:[ \t]+\[[ \d]+%\])?$', result.stdout, re.M)
                self.assertEqual(len(apparent_passes), 100, repr(result.stdout.splitlines()[6:14]))
                for mode in ('compact', 'indexed'):
                    with self.subTest(command=command, options=options, mode=mode):
                        output, strategy = compress(result.stdout, command=command,
                                                    pytest_mode=mode)
                        self.assertEqual(output, result.stdout)
                        self.assertEqual(strategy, 'ambiguous_pytest_live_output')

    def test_explicit_live_capture_flags_always_preserve_output(self):
        raw = progress() + '100 passed in 0.5s\n'
        for flags in ('-s', '-vs', '-sv', '--capture=no', '--capture no',
                      '--capture=tee-sys', '--capture "tee-sys"',
                      '--capture=\'no\'', '"-s"'):
            for mode in ('compact', 'indexed'):
                with self.subTest(flags=flags, mode=mode):
                    self.assertEqual(compress(raw, command='pytest ' + flags, pytest_mode=mode),
                                     (raw, 'ambiguous_pytest_live_output'))

    def test_detached_status_or_unfinished_node_preserves_progress(self):
        for line in ('PASSED', 'PASSED [ 50%]', 'FAILED', 'ERROR',
                     'SKIPPED (reason)', 'XFAIL', 'XPASS',
                     'tests/test_sample.py::test_case[0]'):
            for mode in ('compact', 'indexed'):
                with self.subTest(line=line, mode=mode):
                    raw = progress() + line + '\n100 passed in 0.5s\n'
                    self.assertEqual(compress(raw, command='pytest -v', pytest_mode=mode),
                                     (raw, 'ambiguous_pytest_live_output'))

    def test_detached_status_after_report_section_is_preserved_without_disabling(self):
        tail = ('=== PASSES ===\n---------------- Captured stdout call ----------------\n'
                'PASSED\ntests/test_sample.py::test_case\n100 passed in 0.5s\n')
        for mode in ('compact', 'indexed'):
            output, strategy = compress(progress() + tail, command='pytest -v -rP',
                                        pytest_mode=mode)
            self.assertTrue(strategy.startswith('pytest_pass_summary'))
            self.assertTrue(output.endswith(tail))

    def test_later_session_header_after_nonpass_status_starts_preserved_section(self):
        raw = ('tests/test_sample.py::test_failed FAILED\n'
               '================ test session starts ================\n'
               + progress() + '100 passed, 1 failed in 0.5s\n')
        for mode in ('compact', 'indexed'):
            self.assertEqual(compress(raw, command='pytest -v', pytest_mode=mode)[0], raw)


if __name__ == '__main__':
    unittest.main()
