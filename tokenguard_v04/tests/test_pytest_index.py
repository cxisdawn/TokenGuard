import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from installer import install
from tokenguard.audit import events, summary
from tokenguard.core import compress
from tokenguard.doctor import _installed_argv
from tokenguard.hook import handle
from tokenguard.pytest_index import MAX_INDEX_CHARS, MAX_NODES, PassedIndex
from tokenguard.storage import artifact_path, grep_lines, read_lines

ROOT = Path(__file__).resolve().parents[1]


def log(nodes=2, parameters=180, newline='\n'):
    rows = ['platform test', 'collected tests']
    for n in range(nodes):
        rows.extend(f'tests/test_paths.py::TestPaths::test_ignore_{n}[case {i}] PASSED [ 50%]'
                    for i in range(parameters))
    rows.append(f'===== {nodes * parameters} passed in 0.2s =====')
    return newline.join(rows) + newline


def event(raw):
    return {'hook_event_name': 'PostToolUse', 'session_id': 'index-session',
            'tool_use_id': 'test-call', 'tool_name': 'Bash',
            'tool_input': {'command': 'python -m pytest -v'},
            'tool_response': {'stdout': raw, 'stderr': 'ERROR stderr must remain',
                              'exit_code': 1, 'interrupted': False, 'extra': {'retained': True}}}


class TestPassedIndex(unittest.TestCase):
    def test_default_compact_output_remains_compatible(self):
        raw = log()
        new, strategy = compress(raw, command='pytest')
        self.assertEqual(strategy, 'pytest_pass_summary')
        self.assertEqual(new, 'platform test\ncollected tests\n'
                         '[TokenGuard: 360 pytest PASSED lines hidden; original retrievable]\n'
                         '===== 360 passed in 0.2s =====\n')
        self.assertEqual(compress(raw, command='pytest', pytest_mode='compact'), (new, strategy))

    def test_index_keeps_all_base_nodes_and_parameter_counts(self):
        raw = log()
        new, strategy = compress(raw, command='pytest', pytest_mode='indexed')
        self.assertEqual(strategy, 'pytest_pass_summary_indexed')
        for n, start, end in ((0, 3, 182), (1, 183, 362)):
            self.assertIn(f'"tests/test_paths.py::TestPaths::test_ignore_{n}": 180 passed; '
                          f'raw lines {start}-{end}', new)
        self.assertNotIn('[case 179]', new)
        self.assertIn('===== 360 passed in 0.2s =====', new)
        self.assertLess(len(new), len(raw) * .85)

    def test_interleaved_statuses_and_details_remain_byte_exact(self):
        raw = log(nodes=1)
        tail = ('x::test_skip SKIPPED\ny::test_error ERROR\nz::test_fail FAILED\n'
                'u::test_x XFAIL\nv::test_y XPASS\nUNKNOWN critical custom output\n'
                '=== FAILURES ===\nx::test_fake PASSED\nAssertionError: expected=9 actual=2\n'
                '===== 1 failed, 180 passed in 1.2s =====\n')
        new, _ = compress(raw + tail, command='pytest', pytest_mode='indexed')
        self.assertTrue(new.endswith(tail))
        self.assertNotIn('"x::test_fake"', new)
        first = raw.index('tests/test_paths.py')
        split = raw.index('tests/test_paths.py', first + 1)
        interleaved = raw[:split] + 'warning: keep exact\nx::test_x SKIPPED\n' + raw[split:]
        result, _ = compress(interleaved, command='pytest', pytest_mode='indexed')
        self.assertIn('180 passed; raw lines 3-184', result)
        self.assertIn('warning: keep exact\nx::test_x SKIPPED\n', result)

    def test_newlines_ansi_unicode_and_brackets_in_parent(self):
        for newline in ('\n', '\r\n', '\r'):
            with self.subTest(newline=newline):
                raw = log(nodes=1, newline=newline).replace('platform test', 'header\fnot a new line')
                raw = raw.replace('tests/test_paths.py', '测试[目录]\\test_paths.py')
                raw = raw.replace('PASSED', '\x1b[32mPASSED\x1b[0m')
                result, _ = compress(raw, command='pytest', pytest_mode='indexed')
                self.assertIn('"测试[目录]\\\\test_paths.py::TestPaths::test_ignore_0": 180 passed; raw lines 3-182', result)
                self.assertIn('header\fnot a new line' + newline, result)
                self.assertNotIn('\x1b', result)

    def test_node_and_render_limits_fail_open_with_all_names(self):
        for raw in (log(nodes=MAX_NODES + 1, parameters=3),
                    log(nodes=1).replace('test_ignore_0', 'x' * MAX_INDEX_CHARS),
                    log(nodes=MAX_NODES, parameters=3)):
            with self.subTest(length=len(raw)):
                self.assertEqual(compress(raw, command='pytest', pytest_mode='indexed'),
                                 (raw, 'pytest_index_budget_exceeded'))

    def test_inventory_memory_is_bounded_and_escapes_controls(self):
        index = PassedIndex()
        index.add('test.py::test_"quoted\t[one]', 5)
        rendered = index.render()
        self.assertIn('\\"quoted\\t', rendered)
        self.assertNotIn('\t', rendered)
        for n in range(10000):
            index.add(f'test.py::test_{n}', n + 6)
        self.assertLessEqual(len(index.nodes), MAX_NODES)
        self.assertIsNone(index.render())

    def test_off_incomplete_and_protected_outputs(self):
        raw = log()
        self.assertEqual(compress(raw, command='pytest', pytest_mode='off'), (raw, 'pytest_disabled'))
        for mode in ('compact', 'indexed', 'off'):
            for text in (raw.replace('===== 360 passed in 0.2s =====', ''),
                         'Traceback (most recent call last):\n' + raw, 'diff --git a/x b/x\n' + raw):
                self.assertEqual(compress(text, command='pytest', pytest_mode=mode)[0], text)
        self.assertNotEqual(compress('same\n' * 1000, command='make', pytest_mode='off')[0], 'same\n' * 1000)
        with self.assertRaises(ValueError):
            compress(raw, command='pytest', pytest_mode='unknown')
        self.assertEqual(compress('test.py::test_one PASSED\n1 passed in 0.1s\n',
                                 min_chars=0, command='pytest', pytest_mode='indexed')[1], 'insufficient_saving')


class TestIndexIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'TOKENGUARD_DATA_DIR': str(self.folder / 'data'),
            'TOKENGUARD_ENABLED': '1', 'TOKENGUARD_SOURCE': 'manual', 'TOKENGUARD_DIAGNOSTICS': '1'})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_hook_shape_line_recovery_and_accounting(self):
        raw = log(newline='\r\n')
        source = event(raw)
        out = handle(source, pytest_mode='indexed')['hookSpecificOutput']['updatedToolOutput']
        self.assertEqual({k:v for k,v in out.items() if k != 'stdout'},
                         {k:v for k,v in source['tool_response'].items() if k != 'stdout'})
        self.assertEqual(source['tool_response']['stdout'], raw)
        row = summary('index-session')['records'][0]
        self.assertEqual(row['pytest_mode'], 'indexed')
        self.assertEqual(row['strategy'], 'pytest_pass_summary_indexed')
        self.assertEqual(artifact_path(row['artifact_id']).read_bytes(), raw.encode('utf-8'))
        match = re.search(r'test_ignore_1": 180 passed; raw lines (\d+)-', out['stdout'])
        line = int(match.group(1))
        recovered = read_lines(row['artifact_id'], line, 1, compression_id=row['compression_id'])
        self.assertEqual(recovered, raw.splitlines(keepends=True)[line - 1])
        found = grep_lines(row['artifact_id'], 'test_ignore_1[case 179]', context=0,
                           limit=1, compression_id=row['compression_id'])
        self.assertIn('test_ignore_1[case 179] PASSED', found)
        stats = summary('index-session')
        self.assertEqual(stats['recovery_count'], 2)
        self.assertLess(stats['returned_chars'], len(raw) / 10)
        self.assertGreater(stats['index_build_bytes'], 0)

    def test_manual_hook_subprocess_modes_and_invalid_argument(self):
        for mode in ('compact', 'indexed', 'off'):
            p = subprocess.run([sys.executable, str(ROOT/'tokenguard/hook.py'), '--source', 'manual',
                                '--pytest-mode', mode], input=json.dumps(event(log())),
                               capture_output=True, text=True, encoding='utf-8', timeout=15)
            self.assertEqual(p.returncode, 0, p.stderr)
            if mode == 'off':
                self.assertEqual(p.stdout, '')
            else:
                out = json.loads(p.stdout)['hookSpecificOutput']['updatedToolOutput']['stdout']
                self.assertEqual('pytest PASSED index' in out, mode == 'indexed')
        p = subprocess.run([sys.executable, str(ROOT/'tokenguard/hook.py'), '--pytest-mode', 'bad'],
                           capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(p.returncode, 2)
        self.assertEqual(p.stdout, '')
        self.assertEqual([r['pytest_mode'] for r in summary()['records']], ['compact', 'indexed'])

    def test_fixed_navigation_comparison_contract(self):
        p = subprocess.run([sys.executable, str(ROOT/'benchmark/pytest_navigation.py'), '--output',
                            str(self.folder/'reports')], capture_output=True, text=True,
                           encoding='utf-8', timeout=90)
        self.assertEqual(p.returncode, 0, p.stderr)
        folder = Path(p.stdout.strip())
        report = json.loads((folder/'report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['model_api_calls'], 0)
        self.assertFalse(report['coding_ability_evaluated'])
        self.assertFalse(report['live_claude_integration'])
        self.assertEqual(report['actual_billed_cost'], 'unavailable')
        self.assertEqual(len(report['rows']), 21)
        self.assertTrue(all(r['selective_lookup_verified'] and r['important_output_preserved']
                            and r['native_hook_compressions'] == 0 for r in report['rows']))
        grouped = {r['mode']: r for r in report['rows'] if r['case'] == 'parameterized'}
        self.assertFalse(grouped['compact']['query_visible_before_recovery'])
        self.assertTrue(grouped['indexed']['query_visible_before_recovery'])
        self.assertEqual(grouped['indexed']['recovery_count'], 1)
        self.assertEqual(grouped['compact']['recovery_count'], 1)
        self.assertEqual(grouped['compact']['index_build_bytes'], 0)
        self.assertGreater(grouped['indexed']['index_build_bytes'], 0)
        self.assertGreater(grouped['indexed']['delivered_chars'], grouped['compact']['delivered_chars'])
        for row in report['rows']:
            if row['case'] in ('node_budget', 'name_budget') and row['mode'] == 'indexed':
                self.assertEqual(row['status'], 'pytest_index_budget_exceeded')
                self.assertEqual(row['original_chars'], row['delivered_chars'])
        self.assertTrue((folder/'report.csv').is_file() and (folder/'report.md').is_file())

    def test_invalid_mode_and_index_exception_do_not_emit_replacement(self):
        self.assertIsNone(handle(event(log()), pytest_mode='wrong'))
        with patch('tokenguard.pytest_index.PassedIndex.render', side_effect=RuntimeError('private message')):
            self.assertIsNone(handle(event(log()), pytest_mode='indexed'))
        self.assertEqual(summary()['compressions'], 0)
        self.assertEqual(events('diagnostics-v04.jsonl')[0][-1]['status'], 'exception_RuntimeError')

    def test_installer_modes_preserve_permissions_and_doctor_whitelist(self):
        project = self.folder/'project'
        for mode in ('indexed', 'off', 'compact'):
            path = install(project, Path(sys.executable), pytest_mode=mode)
            data = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(len(data['hooks']['PostToolUse']), 1)
            argv, status = _installed_argv(project)
            self.assertEqual(status, 'passed')
            self.assertEqual(argv[-2:],
                             ['--pytest-mode', mode] if mode != 'compact' else ['--source', 'claude_hook'])
            data['permissions'] = {'allow': ['Read']}
            path.write_text(json.dumps(data), encoding='utf-8')
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['permissions'], {'allow': ['Read']})
        data['hooks']['PostToolUse'][0]['hooks'][0]['command'] += ' --pytest-mode indexed --other'
        path.write_text(json.dumps(data), encoding='utf-8')
        self.assertEqual(_installed_argv(project)[1], 'unsupported_command_shape')
        with self.assertRaises(ValueError):
            install(project, Path(sys.executable), pytest_mode='invalid')
        install(project, Path(sys.executable), True)
        # Unknown arguments are outside the installer's managed command shape.
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), data)
        data['hooks']['PostToolUse'][0]['hooks'][0]['command'] = data['hooks']['PostToolUse'][0]['hooks'][0]['command'].removesuffix(' --pytest-mode indexed --other')
        path.write_text(json.dumps(data), encoding='utf-8')
        install(project, Path(sys.executable), True)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['hooks']['PostToolUse'], [])

    @unittest.skipUnless(os.name == 'nt', 'Windows PowerShell installation')
    def test_powershell_indexed_install_and_return_to_compact(self):
        project = self.folder/'powershell project'
        for mode in ('indexed', 'off', 'compact'):
            p = subprocess.run(['powershell', '-NoProfile', '-File', str(ROOT/'install.ps1'),
                                '-Project', str(project), '-Python', sys.executable, '-PytestMode', mode],
                               capture_output=True, text=True, encoding='utf-8', timeout=30)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(_installed_argv(project)[1], 'passed')


if __name__ == '__main__':
    unittest.main()
