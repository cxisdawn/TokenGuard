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
            with self.assertRaisesRegex(ValueError, 'scan limit'):
                read_lines(identifier,2,1)
        self.assertEqual(events()[0], [])
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
    def test_path_handle_ctime_disagreement_preserves_index_reuse(self):
        from types import SimpleNamespace
        identifier=save_original('line\n'*400+'tail\n')
        source=artifact_path(identifier)
        original_stat=Path.stat
        def stat_with_other_ctime(path,*args,**kwargs):
            result=original_stat(path,*args,**kwargs)
            if path != source:
                return result
            fields={name:getattr(result,name) for name in dir(result) if name.startswith('st_')}
            fields['st_ctime_ns']+=100000000
            return SimpleNamespace(**fields)
        with patch.object(Path,'stat',stat_with_other_ctime):
            self.assertEqual(ensure_index(identifier,source).status,'built')
            self.assertEqual(read_lines(identifier,401,1),'tail\n')
            self.assertEqual(events()[0][-1]['index_status'],'reused')
    def test_handle_ctime_change_still_rejects_inflight_mutation(self):
        from types import SimpleNamespace
        identifier=save_original('line\n'*400)
        source=artifact_path(identifier)
        source_inode=source.stat().st_ino
        real_fstat=os.fstat
        source_calls=0
        def changing_fstat(fd):
            nonlocal source_calls
            result=real_fstat(fd)
            if result.st_ino!=source_inode:
                return result
            source_calls+=1
            if source_calls==1:
                return result
            fields={name:getattr(result,name) for name in dir(result) if name.startswith('st_')}
            fields['st_ctime_ns']+=1
            return SimpleNamespace(**fields)
        with patch('tokenguard.line_index.os.fstat',side_effect=changing_fstat):
            with self.assertRaisesRegex(ValueError,'Artifact changed during index build'):
                ensure_index(identifier,source,force=True)
    def test_replaced_path_identity_still_rejected(self):
        from types import SimpleNamespace
        identifier=save_original('line\n'*400)
        source=artifact_path(identifier)
        original_stat=Path.stat
        def replaced_stat(path,*args,**kwargs):
            result=original_stat(path,*args,**kwargs)
            if path!=source:
                return result
            fields={name:getattr(result,name) for name in dir(result) if name.startswith('st_')}
            fields['st_ino']+=1
            return SimpleNamespace(**fields)
        with patch.object(Path,'stat',replaced_stat):
            with self.assertRaisesRegex(ValueError,'Artifact changed during index build'):
                ensure_index(identifier,source,force=True)
    def test_cli_index_and_range(self):
        identifier=save_original('one\ntwo needle\nthree\n')
        built=subprocess.run([sys.executable,str(ROOT/'tg.py'),'index',identifier],capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(built.stdout)['total_lines'],3)
        recovered=subprocess.run([sys.executable,str(ROOT/'tg.py'),'grep',identifier,'needle','--start','2','--end','2'],capture_output=True,text=True,check=True)
        self.assertEqual(recovered.stdout,'2: two needle\n')

    def test_character_checkpoints_reach_beyond_scan_limit_in_single_line(self):
        raw = '😀' * (MAX_SCAN+10) + 'TAIL'
        identifier = save_original(raw)
        self.assertEqual(read_lines(identifier, 1, 1, char_offset=MAX_SCAN+10), 'TAIL')
        row = events()[0][-1]
        self.assertEqual(row['index_status'], 'built')
        self.assertLess(row['scanned_chars'], CHUNK)
        self.assertGreater(row['seek_offset_bytes'], MAX_SCAN)
        self.assertEqual(row['index_build_bytes'], len(raw.encode('utf-8')))
        self.assertEqual(read_lines(identifier, 1, 1, char_offset=MAX_SCAN+10), 'TAIL')
        self.assertEqual(events()[0][-1]['index_status'], 'reused')
        self.assertEqual(events()[0][-1]['index_build_bytes'], 0)

    def test_legacy_two_field_index_is_rebuilt_without_source_changes(self):
        import hashlib
        import struct
        from tokenguard.line_index import HEADER, MAGIC
        identifier = save_original('line\r\n' * 400 + 'tail')
        source = artifact_path(identifier)
        original = source.read_bytes()
        ensure_index(identifier, source)
        sidecar = index_path(identifier)
        fields = list(HEADER.unpack(sidecar.read_bytes()[:HEADER.size]))
        fields[0] = b'TGIDX001'
        fields[-1] = 1
        legacy = HEADER.pack(*fields) + struct.pack('<2Q', 1, 0)
        sidecar.write_bytes(legacy + hashlib.sha256(legacy).digest())
        self.assertEqual(read_lines(identifier, 401, 1), 'tail')
        self.assertEqual(events()[0][-1]['index_status'], 'built')
        self.assertEqual(sidecar.read_bytes()[:8], MAGIC)
        self.assertEqual(source.read_bytes(), original)

    def test_multibyte_and_crlf_boundaries_in_small_index_chunks(self):
        raw = ('😀原\r\nß\rthree\n' * 400) + 'tail'
        identifier = save_original(raw)
        with patch('tokenguard.line_index.CHUNK', 25), patch('tokenguard.line_index.BYTE_STRIDE', 25):
            self.assertEqual(read_lines(identifier, 1198, 4), '😀原\r\nß\rthree\ntail')
            info = ensure_index(identifier, artifact_path(identifier))
            self.assertEqual(info.total_lines, 1201)
            self.assertEqual(read_lines(identifier, 1198, 1, char_offset=1), '原\r\n')

    def test_artifact_change_after_index_lookup_cannot_return_wrong_line(self):
        identifier = save_original('a\n' * 400 + 'TARGET\n')
        source = artifact_path(identifier)
        replacement = ('zz\n' * 400 + 'TARGET\n').encode('utf-8')
        def mutate_after_lookup(*args, **kwargs):
            position = ensure_index(*args, **kwargs)
            source.write_bytes(replacement)
            return position
        with patch('tokenguard.storage.ensure_index', side_effect=mutate_after_lookup):
            with self.assertRaisesRegex(ValueError, 'Artifact changed'):
                read_lines(identifier, 401, 1)
        self.assertEqual(events()[0], [])
        self.assertEqual(source.read_bytes(), replacement)
        self.assertEqual(read_lines(identifier, 401, 1), 'TARGET\n')

    def test_artifact_change_during_open_handle_read_is_rejected_without_audit_success(self):
        identifier = save_original('a\n' * 400 + 'TARGET\n')
        source = artifact_path(identifier)
        replacement = ('zz\n' * 400 + 'TARGET\n').encode('utf-8')
        original_open = Path.open
        class MutatingRead:
            def __init__(self, stream):
                self.stream = stream
                self.mutated = False
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.stream.close()
            def __getattr__(self, name):
                return getattr(self.stream, name)
            def readline(self, *args):
                result = self.stream.readline(*args)
                if not self.mutated:
                    self.mutated = True
                    source.write_bytes(replacement)
                return result
        def changing_open(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            return MutatingRead(stream) if path == source and args and args[0] == 'r' else stream
        with patch.object(Path, 'open', changing_open):
            with self.assertRaisesRegex(ValueError, 'Artifact changed during recovery read'):
                read_lines(identifier, 401, 1)
        self.assertEqual(events()[0], [])
        self.assertEqual(source.read_bytes(), replacement)

if __name__=='__main__': unittest.main()
