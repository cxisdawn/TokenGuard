import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tokenguard.audit import events
from tokenguard.storage import save_original, artifact_path, read_lines, grep_lines, MAX_SCAN
from tokenguard.line_index import ensure_index, index_path, CHUNK

ROOT=Path(__file__).resolve().parent.parent

class TestLineIndex(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{'TOKENGUARD_DATA_DIR':self.temp.name,'TOKENGUARD_INDEX_ENABLED':'1'})
        self.env.start()
    def tearDown(self):
        self.env.stop(); self.temp.cleanup()
    def test_show_tail_past_scan_budget_builds_then_reuses(self):
        raw=('heartbeat '+'x'*70+'\n')*110000+'FAILED: tail detail\nassert expected == actual\n'
        self.assertGreater(len(raw),MAX_SCAN)
        identifier=save_original(raw)
        result=read_lines(identifier,110001,2)
        self.assertEqual(result,'FAILED: tail detail\nassert expected == actual\n')
        row=events()[0][-1]
        self.assertEqual(row['index_status'],'built')
        self.assertGreater(row['seek_offset_bytes'],MAX_SCAN)
        self.assertEqual(row['index_build_bytes'],len(raw.encode()))
        self.assertLess(row['scanned_chars'],20000)
        self.assertEqual(read_lines(identifier,110001,1),'FAILED: tail detail\n')
        self.assertEqual(events()[0][-1]['index_status'],'reused')
        self.assertEqual(events()[0][-1]['index_build_bytes'],0)
    def test_seek_after_one_oversized_line(self):
        identifier=save_original('x'*(MAX_SCAN+5)+'\nneedle\n')
        self.assertEqual(read_lines(identifier,2,1),'needle\n')
        self.assertLess(events()[0][-1]['scanned_chars'],20)
    def test_unicode_mixed_newlines_exact_offsets(self):
        lines=['中文\r\n','第二行\r','third\n','last']
        # Many rows force actual nonzero checkpoint offsets rather than just line 1.
        raw=''.join(lines[:3])*300+lines[-1]
        identifier=save_original(raw)
        self.assertEqual(read_lines(identifier,899,3),'第二行\rthird\nlast')
        self.assertGreater(events()[0][-1]['seek_offset_bytes'],0)
    def test_crlf_split_across_index_chunk_boundary(self):
        raw='x'*(CHUNK-1)+'\r\n'+'next\r'+'終\n'
        identifier=save_original(raw)
        self.assertEqual(read_lines(identifier,2,2),'next\r終\n')
        info=ensure_index(identifier,artifact_path(identifier))
        self.assertEqual(info.total_lines,3)
    def test_empty_and_unterminated_files(self):
        for raw,total in [('',0),('one',1),('one\n',1),('one\n\n',2),('\r\n',1),('a\rb',2)]:
            identifier=save_original(raw)
            self.assertEqual(ensure_index(identifier,artifact_path(identifier)).total_lines,total)
            self.assertEqual(read_lines(identifier,100,1),'(no matches)\n')
    def test_corrupt_sidecar_rebuilt_without_source_changes(self):
        raw='line\n'*400+'tail\n'
        identifier=save_original(raw)
        path=artifact_path(identifier)
        original=path.read_bytes()
        ensure_index(identifier,path)
        sidecar=index_path(identifier)
        corrupt=bytearray(sidecar.read_bytes()); corrupt[-1]^=1
        sidecar.write_bytes(corrupt)
        self.assertEqual(read_lines(identifier,401,1),'tail\n')
        self.assertEqual(events()[0][-1]['index_status'],'built')
        self.assertEqual(path.read_bytes(),original)
    def test_stale_source_fingerprint_rebuilds_for_legacy_artifact(self):
        identifier=save_original('a\n'*400+'old\n')
        path=artifact_path(identifier)
        ensure_index(identifier,path)
        # Legacy/manual artifacts can differ from the original content-address bytes.
        path.write_bytes(('longer\r\n'*400+'new\r\n').encode())
        self.assertEqual(read_lines(identifier,401,1),'new\r\n')
        self.assertEqual(events()[0][-1]['index_status'],'built')
    def test_index_write_failure_falls_back_sequential(self):
        identifier=save_original('a\nb\nc\n')
        with patch('tokenguard.storage.ensure_index',side_effect=PermissionError()):
            self.assertEqual(read_lines(identifier,2,1),'b\n')
        self.assertEqual(events()[0][-1]['index_status'],'unavailable_sequential_fallback')
    def test_disable_index_preserves_previous_scan_cap(self):
        identifier=save_original('x'*(MAX_SCAN+1)+'\ntail\n')
        with patch.dict(os.environ,{'TOKENGUARD_INDEX_ENABLED':'0'}):
            self.assertIn('limit',read_lines(identifier,2,1))
        self.assertEqual(events()[0][-1]['index_status'],'disabled')
        self.assertFalse(index_path(identifier).exists())
    def test_keyword_range_context_clipping_and_overlap(self):
        identifier=save_original('needle outside\nstart\nneedle\nneedle\nend\nneedle outside\n')
        result=grep_lines(identifier,'needle',start=2,end=5,context=1)
        self.assertEqual(result,'2: start\n3: needle\n4: needle\n5: end\n')
        self.assertNotIn('outside',result)
    def test_keyword_tail_range_uses_index(self):
        identifier=save_original(('normal '+'x'*80+'\n')*100000+'FAILED: final\n')
        self.assertIn('100001: FAILED: final',grep_lines(identifier,'FAILED',start=100000,context=1))
        self.assertLess(events()[0][-1]['scanned_chars'],20000)
    def test_range_validation_and_return_budget(self):
        identifier=save_original('needle\n'*1000)
        for kwargs in ({'start':0},{'start':3,'end':2}):
            with self.assertRaises(ValueError): grep_lines(identifier,'needle',**kwargs)
        result=grep_lines(identifier,'needle',start=900,max_chars=100)
        self.assertLessEqual(len(result),100)
        self.assertIn('next_start_line=',result)
        resume=events()[0][-1]['next_start_line']
        # Space reserved for the marker displaces output before the last scanned line.
        self.assertLess(resume,910)
        self.assertIn(f'{resume}: needle',grep_lines(identifier,'needle',start=resume,limit=1,context=0))
    def test_cli_index_and_range(self):
        identifier=save_original('one\ntwo needle\nthree\n')
        built=subprocess.run([sys.executable,str(ROOT/'tg.py'),'index',identifier],capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(built.stdout)['total_lines'],3)
        recovered=subprocess.run([sys.executable,str(ROOT/'tg.py'),'grep',identifier,'needle','--start','2','--end','2'],capture_output=True,text=True,check=True)
        self.assertEqual(recovered.stdout,'2: two needle\n')

if __name__=='__main__': unittest.main()
