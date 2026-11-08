import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'experiments/generalization_v1'))
import resume_paired_repair as resume


class ResumeTests(unittest.TestCase):
    def test_order_keeps_alternation(self):
        self.assertEqual(resume.order(dict(selected_ids=['one','two'],arms=['A3','A4'])),
                         [('one','A3'),('one','A4'),('two','A4'),('two','A3')])

    def test_invalid_progress_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            resume.dump(p/'run_config.json',dict(selected_ids=['one'],arms=['A3','A4']))
            resume.dump(p/'progress.json',dict(completed=1,planned=2,records=[dict(ticket_id='one',arm='A4')]))
            with self.assertRaises(ValueError):resume.inventory(p)

    def test_journal_replays_exact_reply_without_inference(self):
        with tempfile.TemporaryDirectory() as d:
            backend=resume.JournalBackend();backend.journal=Path(d);backend.cursor=0
            resume.dump(Path(d)/'001.json',dict(request=dict(prefix='p',suffix='s',kwargs={'seed':42}),reply={'text':'body'}))
            with patch.object(resume.OllamaFIM,'infill',side_effect=AssertionError('must not generate')):
                self.assertEqual(backend.infill('p','s',seed=42),{'text':'body'})

    def test_unknown_attempt_never_repeated(self):
        with tempfile.TemporaryDirectory() as d:
            backend=resume.JournalBackend();backend.journal=Path(d);backend.cursor=0
            resume.dump(Path(d)/'001.json',dict(request=dict(prefix='p',suffix='s',kwargs={}),status='started'))
            with patch.object(resume.OllamaFIM,'infill',side_effect=AssertionError('must not generate')):
                with self.assertRaisesRegex(ValueError,'unknown consumed'):backend.infill('p','s')

    def test_collect_is_get_only_and_verifies_identity(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);request=root/'original/request.json';raw=b'saved diff\n'
            meta=dict(repository='owner/repo',run_id='repair-id',workflow_run_id=1,head_sha='head',
                instance_id='one',patch_sha256=resume.sha(raw),dataset='dataset',split='test')
            resume.dump(request,meta);(request.parent/'candidate.diff').write_bytes(raw)
            verdict={k:meta[k] for k in ('run_id','instance_id','patch_sha256','dataset','split')}
            verdict.update(status='failed',official_resolved=False,feedback='AssertionError: failure')
            buffer=io.BytesIO()
            with zipfile.ZipFile(buffer,'w') as z:z.writestr('verdict.json',json.dumps(verdict))
            calls=[]
            class API:
                def __init__(self,*args):pass
                def request(self,path,payload=None,binary=False):
                    calls.append((path,payload))
                    if payload is not None:raise AssertionError('must not POST')
                    if path=='actions/runs/1':return dict(display_title='repair-id',head_sha='head',status='completed',conclusion='success')
                    if path.endswith('/artifacts'):return dict(artifacts=[dict(name='swebench-repair-id',id=2,size_in_bytes=100)])
                    return buffer.getvalue()
            with patch.object(resume,'GitHubAPI',API):
                result=resume.collect(request,root/'recovery')
            self.assertIs(result['official_resolved'],False)
            self.assertTrue(all(payload is None for _,payload in calls))
            self.assertFalse((request.parent/'validation.json').exists())
            bad=dict(meta,patch_sha256='wrong');resume.dump(request,bad)
            with patch.object(resume,'GitHubAPI',side_effect=AssertionError('no network for bad SHA')):
                with self.assertRaisesRegex(ValueError,'identity'):resume.collect(request,root/'bad')


if __name__=='__main__':unittest.main()
