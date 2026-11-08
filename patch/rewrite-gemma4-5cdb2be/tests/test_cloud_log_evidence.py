import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).resolve().parents[1])]
from fim_patch.cloud_log_evidence import capture_file,collect_logs,select_feedback,read_archive_feedback,VERSION
from fim_patch.iterative import test_failure_excerpt,feedback_block
from fim_patch.failure_identity import failure_evidence
from fim_patch.cloud_official import CloudOfficialRepairValidator
from tests.test_cloud_official import API,PATCH


def verdict():
    return dict(run_id='r',instance_id='case',patch_sha256='p',dataset='d',split='test',status='failed',official_resolved=False,feedback='old summary')


def archive(log='E assert 1 == 2\nFAILED tests/a.py::test_a\n',stage='outer log',tamper=False,wrong_id=False):
    v=verdict();data=log.encode();outer=stage.encode()
    manifest=dict(version=VERSION,identity=dict(v),files={name:dict(present=True,source=name,captured_bytes=len(raw),captured_sha256=hashlib.sha256(raw).hexdigest())
                  for name,raw in [('test_output.txt',data),('run_instance.log',outer)]})
    if tamper:manifest['files']['test_output.txt']['captured_sha256']='bad'
    if wrong_id:manifest['identity']['run_id']='other'
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as z:
        z.writestr('logs_manifest.json',json.dumps(manifest));z.writestr('test_output.txt',data);z.writestr('run_instance.log',outer)
        z.writestr('report.json',json.dumps({'case':dict(tests_status={'FAIL_TO_PASS':{'failure':['tests/a.py::test_a']}})}))
    return zipfile.ZipFile(io.BytesIO(stream.getvalue()))


class EvidenceTests(unittest.TestCase):
    def test_separate_capture_cannot_overwrite_assertion_with_long_runner(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'logs';out=Path(temp)/'out';root.mkdir();out.mkdir()
            real=b'E assert 3 == 4\nFAILED tests/a.py::test_a\n'
            (root/'test_output.txt').write_bytes(real)
            (root/'run_instance.log').write_text('outer harness only\n'*10000)
            logs,manifest=collect_logs(root,out,verdict())
            self.assertEqual((out/'test_output.txt').read_bytes(),real)
            self.assertNotIn('outer harness',logs['test_output.txt'])
            self.assertEqual(manifest['files']['test_output.txt']['original_sha256'],hashlib.sha256(real).hexdigest())
            r=select_feedback(verdict(),logs,source_verified=True)
            self.assertEqual(r['feedback_source'],'artifact_test_output');self.assertTrue(r['failure_details_verified'])
            self.assertIn('assert 3 == 4',r['feedback'])

    def test_head_tail_ranges_and_hashes(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'large';data=b'HEAD'+b'x'*1000+b'TAIL';path.write_bytes(data)
            raw,meta=capture_file(path,200)
            self.assertEqual(len(raw),200);self.assertTrue(meta['truncated'])
            self.assertTrue(raw.startswith(b'HEAD') and raw.endswith(b'TAIL'))
            self.assertEqual(meta['ranges'][0]['offset'],0)
            self.assertEqual(meta['original_sha256'],hashlib.sha256(data).hexdigest())
            self.assertEqual(meta['captured_sha256'],hashlib.sha256(raw).hexdigest())

    def test_missing_test_details_no_bulk_log_fallback(self):
        r=select_feedback(verdict(),{'run_instance.log':'runner wrote test_output.txt\nouter metadata\n'},
            {'case':{'tests_status':{'FAIL_TO_PASS':{'failure':['tests/a.py::test_a']}}}},True)
        self.assertEqual(r['feedback_quality'],'missing_test_details')
        self.assertEqual(r['feedback_source'],'structured_report');self.assertFalse(r['failure_details_verified'])
        self.assertIn('unavailable',r['feedback']);self.assertNotIn('outer metadata',r['feedback'])

    def test_stage_failure_has_distinct_source(self):
        r=select_feedback(verdict(),{'run_instance.log':'TypeError: setup failed\n'},source_verified=True)
        self.assertEqual(r['feedback_source'],'artifact_run_instance');self.assertFalse(r['failure_details_verified'])

    def test_assertion_survives_double_compaction(self):
        text=test_failure_excerpt('E assert left == right\nFAILED tests/a.py::test_a\n')
        self.assertIn('Assertion evidence: assert left == right',text)
        compact=feedback_block({'reason':'validation_failed','validation':{'feedback':text}},compact=True)
        self.assertIn('assert left == right',compact)

    def test_archive_source_and_integrity_verified(self):
        with archive() as z:r,raws,_=read_archive_feedback(z,verdict())
        self.assertTrue(r['failure_details_verified']);self.assertIn('test_output.txt',raws)
        self.assertEqual(r['feedback_source'],'artifact_test_output')

    def test_tamper_and_identity_mismatch_not_verified(self):
        for kwargs in ({'tamper':True},{'wrong_id':True}):
            with archive(**kwargs) as z:r,_,_=read_archive_feedback(z,verdict())
            self.assertFalse(r['failure_details_verified']);self.assertFalse(r['log_source_separation_verified'])
            self.assertTrue(r['feedback_provenance']['issues'])

    def test_legacy_combined_log_is_not_verified_test_source(self):
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as z:z.writestr('test_output.txt','AssertionError: failure\n')
        with zipfile.ZipFile(io.BytesIO(stream.getvalue())) as z:r,_,_=read_archive_feedback(z,verdict())
        self.assertEqual(r['feedback_source'],'artifact_test_output');self.assertEqual(r['feedback_quality'],'legacy_unverified_source')
        self.assertFalse(r['failure_details_verified'])

    def test_provenance_hashes_do_not_defeat_semantic_failure_dedup(self):
        a=dict(status='failed',feedback='AssertionError: expected 4',feedback_quality='test_failure_detail',feedback_provenance={'sha':'one'})
        b=dict(a,feedback_provenance={'sha':'two'})
        self.assertEqual(failure_evidence(a),failure_evidence(b))
        b['feedback_quality']='missing_test_details';self.assertNotEqual(failure_evidence(a),failure_evidence(b))
        a['expected']={'feedback_provenance':{'sha':'domain-one'}};b=dict(a,expected={'feedback_provenance':{'sha':'domain-two'}})
        self.assertNotEqual(failure_evidence(a),failure_evidence(b))

    def test_missing_source_record_and_stale_output_removed(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'logs';out=Path(temp)/'out';root.mkdir();out.mkdir()
            (out/'test_output.txt').write_text('stale')
            _,manifest=collect_logs(root,out,verdict())
            self.assertFalse((out/'test_output.txt').exists());self.assertFalse(manifest['files']['test_output.txt']['present'])

    def test_diff_echo_does_not_become_assertion_evidence(self):
        r=select_feedback(verdict(),{'test_output.txt':'--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n+E assert wrong == answer\n'},source_verified=True)
        self.assertEqual(r['feedback_quality'],'missing_test_details');self.assertFalse(r['failure_details_verified'])

    def test_actual_bridge_outputs_independent_files_and_metadata(self):
        import os,subprocess
        bridge=Path(__file__).resolve().parents[1]/'scripts/cloud_official_bridge.py'
        env=dict(os.environ,RUN_ID='r',INSTANCE_ID='case',PATCH_SHA256='p',DATASET_NAME='d',DATASET_SPLIT='test',HARNESS_OUTCOME='success')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'logs/run_evaluation/r/iterative-repair/case';root.mkdir(parents=True)
            (root/'report.json').write_text(json.dumps({'case':{'resolved':False}}))
            (root/'test_output.txt').write_text('E assert actual == expected\nFAILED tests/a.py::test_a\n')
            (root/'run_instance.log').write_text('irrelevant harness footer\n'*10000)
            subprocess.run([sys.executable,str(bridge),'collect'],cwd=temp,env=env,check=True,capture_output=True)
            out=Path(temp)/'official-artifacts';v=json.loads((out/'verdict.json').read_text())
            self.assertIs(v['official_resolved'],False);self.assertIn('assert actual == expected',v['feedback'])
            self.assertNotIn('irrelevant harness footer',(out/'test_output.txt').read_text())
            self.assertTrue((out/'run_instance.log').exists());self.assertTrue((out/'logs_manifest.json').exists())


class CloudIntegrationTests(unittest.TestCase):
    def test_actual_validator_prefers_separated_log_and_keeps_grade(self):
        class SeparatedAPI(API):
            def request(self,path,payload=None,binary=False):
                raw=super().request(path,payload,binary)
                if not binary:return raw
                with zipfile.ZipFile(io.BytesIO(raw)) as old:v=json.loads(old.read('verdict.json'))
                log=b'E assert actual == expected\nFAILED tests/a.py::test_a\n'
                meta=dict(version=VERSION,identity=v,files={'test_output.txt':dict(present=True,source='test_output.txt',
                    captured_bytes=len(log),captured_sha256=hashlib.sha256(log).hexdigest()),'run_instance.log':dict(present=False)})
                out=io.BytesIO()
                with zipfile.ZipFile(out,'w') as z:
                    z.writestr('verdict.json',json.dumps(v));z.writestr('logs_manifest.json',json.dumps(meta));z.writestr('test_output.txt',log)
                return out.getvalue()
        with tempfile.TemporaryDirectory() as temp:
            r=CloudOfficialRepairValidator('case',temp,'o/r','ref',api=SeparatedAPI())(PATCH,None)
            self.assertIs(r['official_resolved'],False);self.assertEqual(r['status'],'failed')
            self.assertTrue(r['failure_details_verified']);self.assertIn('assert actual == expected',r['feedback'])
            raw=json.loads((Path(r['work_dir'])/'verdict.json').read_text())
            self.assertEqual(raw['feedback'],'Traceback\nAssertionError: actual != expected')
            self.assertTrue((Path(r['work_dir'])/'test_output.txt').exists())


if __name__=='__main__':unittest.main()
