import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from organizer.storage import Store
from organizer.filesystem import FileRow, signature, identity, scan, plan_moves, execute, undo, HOLD
from organizer.ai import Gemini, AnalysisError, analyze, validate, cache_key

class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.base=Path(self.temp.name)
        self.src,self.dst=self.base/'입력',self.base/'정리'
        self.src.mkdir(); self.dst.mkdir()
        self.store=Store(self.base/'state.db')
        self.cancel=threading.Event()
        self.progress=lambda _:None
        self.cats=[{'name':'수업자료','path':str(self.dst)}]
    def tearDown(self): self.temp.cleanup()
    def row(self,name='체육_평가.txt'):
        p=self.src/name
        p.write_text('원본',encoding='utf-8')
        sig=signature(p)
        return FileRow(p,sig,identity(p,sig),True,final='수업자료',manual=True)
    def move(self,rows): return execute(plan_moves(rows,self.cats),self.store,self.cancel,self.progress)
    def test_move_undo_unicode_restart(self):
        row=self.row(); result=self.move([row])
        self.assertEqual(result[0][1],'이동 완료')
        self.assertFalse(row.path.exists())
        self.assertEqual((self.dst/row.path.name).read_text(),'원본')
        undo(Store(self.store.path),self.cancel,self.progress)
        self.assertEqual(row.path.read_text(),'원본')
        self.assertIsNone(self.store.last_job())
    def test_collision_never_overwrites(self):
        row=self.row(); (self.dst/row.path.name).write_text('기존')
        self.assertTrue(plan_moves([row],self.cats)[0].renamed)
        self.move([row])
        self.assertEqual((self.dst/row.path.name).read_text(),'기존')
        self.assertEqual((self.dst/'체육_평가 (1).txt').read_text(),'원본')
    def test_undo_conflict(self):
        row=self.row(); self.move([row]); row.path.write_text('새 파일')
        messages=undo(self.store,self.cancel,self.progress)
        self.assertEqual(row.path.read_text(),'새 파일')
        self.assertEqual((self.src/'체육_평가 (1).txt').read_text(),'원본')
        self.assertIn('번호 추가',messages[0])
    def test_modified_destination_not_restored(self):
        row=self.row(); self.move([row])
        (self.dst/row.path.name).write_text('수정됨 변경된 내용')
        self.assertIn('복원 보류',undo(self.store,self.cancel,self.progress)[0])
        self.assertFalse(row.path.exists())
    def test_unchecked_hold_failed_permanent_not_moved(self):
        rows=[self.row(f'{i}.txt') for i in range(4)]
        rows[0].selected=False; rows[1].final=HOLD
        rows[2].status='분석 실패'; rows[3].permanent=True
        self.assertEqual(plan_moves(rows,self.cats),[])
    def test_changed_source_rejected(self):
        row=self.row(); row.path.write_text('스캔 이후 변경')
        with self.assertRaises(ValueError): plan_moves([row],self.cats)
    def test_change_after_approval_rejected(self):
        row=self.row(); plans=plan_moves([row],self.cats)
        row.path.write_text('승인 이후 변경')
        result=execute(plans,self.store,self.cancel,self.progress)
        self.assertEqual(result[0][1],'이동 실패'); self.assertTrue(row.path.exists())
    def test_race_destination_created(self):
        row=self.row(); plans=plan_moves([row],self.cats)
        plans[0].target.write_text('경쟁 파일')
        result=execute(plans,self.store,self.cancel,self.progress)
        self.assertEqual(result[0][1],'이동 실패')
        self.assertEqual(plans[0].target.read_text(),'경쟁 파일'); self.assertTrue(row.path.exists())
    def test_cancel_preserves_original(self):
        row=self.row(); plans=plan_moves([row],self.cats); self.cancel.set()
        self.assertEqual(execute(plans,self.store,self.cancel,self.progress),[])
        self.assertTrue(row.path.exists())
    def test_journal_precedes_mutation(self):
        row=self.row()
        def failure(src,dst):
            self.assertEqual(self.store.records()[0]['state'],'pending')
            raise PermissionError('잠김')
        with patch('organizer.filesystem.move_no_replace',side_effect=failure): result=self.move([row])
        self.assertEqual(result[0][1],'이동 실패'); self.assertTrue(row.path.exists())
        self.assertEqual(self.store.records()[0]['state'],'failed')
    def test_partial_success_undo(self):
        first,second=self.row('하나.txt'),self.row('둘.txt')
        from organizer.filesystem import move_no_replace
        def partial(src,dst):
            if src.name=='둘.txt': raise PermissionError('잠김')
            move_no_replace(src,dst)
        with patch('organizer.filesystem.move_no_replace',side_effect=partial): self.move([first,second])
        undo(self.store,self.cancel,self.progress)
        self.assertTrue(first.path.exists()); self.assertTrue(second.path.exists())
    def test_scan_exclusions_hidden_destinations(self):
        row=self.row(); self.row('.숨김.txt')
        nested=self.src/'destination'; nested.mkdir(); (nested/'내부.txt').write_text('내용')
        self.store.exclude(row.key,row.path)
        rows,_=scan(self.src,True,False,True,[nested],self.store,self.cancel,self.progress)
        self.assertEqual(len(rows),1); self.assertTrue(rows[0].permanent)
    def test_same_name_in_subfolders(self):
        row=self.row(); nested=self.src/'하위'; nested.mkdir(); p=nested/row.path.name; p.write_text('둘')
        sig=signature(p); other=FileRow(p,sig,identity(p,sig),True,final='수업자료',manual=True)
        plans=plan_moves([row,other],self.cats)
        self.assertNotEqual(plans[0].target,plans[1].target)
    def test_symlink_skipped(self):
        row=self.row()
        try: (self.src/'링크.txt').symlink_to(row.path)
        except OSError: self.skipTest('링크 생성 권한 없음')
        rows,_=scan(self.src,True,True,True,[],self.store,self.cancel,self.progress)
        self.assertEqual(len(rows),1)
    def test_ambiguous_pending_preserved(self):
        row=self.row(); dst=self.dst/row.path.name; dst.write_text('다른 파일')
        self.store.journal(self.store.new_job(),row.path,dst,row.sig)
        self.assertIn('복원 보류',undo(self.store,self.cancel,self.progress)[0])
        self.assertEqual(dst.read_text(),'다른 파일'); self.assertTrue(row.path.exists())
    def test_validation_rejects_unknown_duplicate_nan(self):
        good={'id':0,'category':'수업자료','confidence':.9,'reason':'파일명'}
        for bad in [dict(good,category='새 분류'),dict(good,confidence=float('nan')),dict(good,id=True)]:
            with self.assertRaises(AnalysisError): validate([bad],{0},['수업자료'])
        with self.assertRaises(AnalysisError): validate([good,good],{0,1},['수업자료'])
    def test_payload_filename_only_cache_manual_skip(self):
        requests=[]
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self):
                return json.dumps({'candidates':[{'content':{'parts':[{'text':json.dumps([
                    {'id':0,'category':'수업자료','confidence':.9,'reason':'평가 파일명'}])}]}}]}).encode()
        def opener(request,timeout): requests.append(json.loads(request.data)); return Response()
        row=self.row(); row.manual=False; client=Gemini('secret-key','test-model',opener)
        result=analyze([row],self.cats,self.store,client,100,self.cancel,self.progress)
        self.assertEqual(result[row.key]['category'],'수업자료')
        payload=json.loads(requests[0]['contents'][0]['parts'][0]['text'])
        self.assertEqual(set(payload['files'][0]),{'id','filename','extension'})
        self.assertNotIn(str(self.src),json.dumps(requests)); self.assertNotIn(str(self.dst),json.dumps(requests))
        analyze([row],self.cats,self.store,client,100,self.cancel,self.progress)
        self.assertEqual(len(requests),1)
        row.manual=True
        analyze([row],self.cats,self.store,client,100,self.cancel,self.progress,True)
        self.assertEqual(len(requests),1)
    def test_malformed_retry(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self): return b'not json'
        class Cancel:
            def is_set(self): return False
            def wait(self,seconds): return False
        calls=[]; client=Gemini('key','model',lambda *a,**k:(calls.append(1) or Response()))
        with self.assertRaises(AnalysisError): client.classify([{'id':0,'filename':'x','extension':''}],['수업자료'],Cancel())
        self.assertEqual(len(calls),3)
    def test_cache_invalidation(self):
        row=self.row(); base=cache_key(row,['a'],'model')
        self.assertNotEqual(base,cache_key(row,['b'],'model'))
        self.assertNotEqual(base,cache_key(row,['a'],'model2'))
        row.sig[0]+=1; self.assertNotEqual(base,cache_key(row,['a'],'model'))
if __name__=='__main__': unittest.main()
