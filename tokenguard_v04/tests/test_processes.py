import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from tokenguard.storage import artifact_path, save_original, read_lines, grep_lines, MAX_SCAN
from tokenguard.audit import events, summary
from tokenguard.hook import handle

ROOT = Path(__file__).resolve().parent.parent

class TestProcesses(unittest.TestCase):
    def test_hook_invalid_json_no_stdout(self):
        p = subprocess.run([sys.executable, str(ROOT/'tokenguard/hook.py')], input='{bad', text=True, capture_output=True, encoding='utf-8')
        self.assertEqual(p.returncode,0)
        self.assertEqual(p.stdout,'')
        self.assertNotIn('{bad', p.stderr)
    def test_manual_verification(self):
        p = subprocess.run([sys.executable,str(ROOT/'verify_hook.py')],capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertTrue(json.loads(p.stdout)['artifact_byte_identical'])
    def test_scan_cap_and_early_stop(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ,{'TOKENGUARD_DATA_DIR':d}):
            identifier = save_original('first\n'+'x'*(MAX_SCAN+10)+'needle\n')
            self.assertEqual(read_lines(identifier,1,1),'first\n')
            self.assertLess(events()[0][-1]['scanned_chars'],100)
            result = grep_lines(identifier,'needle')
            self.assertIn('limit',result)
            self.assertLessEqual(events()[0][-1]['scanned_chars'],MAX_SCAN+1)
    def test_symlink_rejected_if_supported(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ,{'TOKENGUARD_DATA_DIR':d}):
            identifier = 'a'*64
            folder = Path(d)/'artifacts'
            folder.mkdir()
            outside = Path(d)/'elsewhere'
            outside.write_text('must not read')
            try:
                (folder/(identifier+'.txt')).symlink_to(outside)
            except OSError:
                self.skipTest('Windows symlink privilege unavailable')
            with self.assertRaises(ValueError): artifact_path(identifier)
    @unittest.skipUnless(os.name=='nt','Windows PowerShell smoke test')
    def test_powershell_install_uninstall(self):
        with tempfile.TemporaryDirectory(prefix='tg install ') as d:
            project = Path(d)/'project with spaces'
            for extra in [[],[],['-Uninstall']]:
                p = subprocess.run(['powershell','-NoProfile','-File',str(ROOT/'install.ps1'),'-Project',str(project),'-Python',sys.executable,*extra],capture_output=True,text=True,encoding='utf-8',errors='replace')
                self.assertEqual(p.returncode,0,p.stderr)
            data=json.loads((project/'.claude/settings.local.json').read_text())
            self.assertEqual(data['hooks']['PostToolUse'],[])
    def test_benchmark_contract(self):
        with tempfile.TemporaryDirectory() as d:
            p=subprocess.run([sys.executable,str(ROOT/'benchmark/run.py'),'--rounds','1','--output',d],capture_output=True,text=True,encoding='utf-8',timeout=120)
            self.assertEqual(p.returncode,0,p.stderr)
            folder=Path(p.stdout.strip())
            rows=json.loads((folder/'report.json').read_text())['rows']
            self.assertEqual(len(rows),10)
            self.assertEqual(len({r['session_id'] for r in rows}),10)
            self.assertTrue(all(r['success'] for r in rows))
            self.assertTrue(all(r['actual_billed_cost']=='unavailable' for r in rows))
            for case in {r['case'] for r in rows}:
                pair=[r for r in rows if r['case']==case]
                self.assertEqual(pair[0]['initial_snapshot_sha256'],pair[1]['initial_snapshot_sha256'])
                self.assertEqual(pair[0]['prompt_sha256'],pair[1]['prompt_sha256'])
            recoveries=[r['recovery_count'] for r in rows if r['variant']=='optimized' and r['case'].startswith('pytest')]
            self.assertEqual(recoveries,[1,1])
            indexed=next(r for r in rows if r['case']=='large_artifact_recovery' and r['variant']=='optimized')
            self.assertTrue(indexed['failure_recovery_verified'])
            self.assertEqual(indexed['indexed_recoveries'],1)
            self.assertGreater(indexed['index_build_bytes'],8*1024*1024)
            self.assertLess(indexed['recovery_scanned_chars'],20000)
            self.assertTrue((folder/'report.csv').is_file())
            self.assertTrue((folder/'report.md').is_file())

if __name__=='__main__': unittest.main()
