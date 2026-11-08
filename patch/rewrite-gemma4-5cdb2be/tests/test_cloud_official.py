import base64
import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from fim_patch.cloud_official import CloudOfficialRepairValidator, GitHubAPI, SafeRedirect

PATCH='diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n'
class API:
    def __init__(self, mode='failed'):
        self.mode=mode; self.calls=[]
    def request(self,path,payload=None,binary=False):
        self.calls.append((path,payload))
        if path.endswith('/dispatches'):
            self.inputs=payload['inputs'];return {}
        if 'runs?event=' in path:
            return {'workflow_runs':[{'id':10,'display_title':self.inputs['run_id'],
                      'html_url':'https://github.com/o/r/actions/runs/10','head_sha':'a'*40}]}
        if path=='actions/runs/10':return {'status':'completed','conclusion':'success'}
        if path.endswith('/artifacts'):
            if self.mode=='missing':return {'artifacts':[]}
            return {'artifacts':[{'id':20,'name':'swebench-'+self.inputs['run_id'],'expired':False}]}
        meta={k:self.inputs[k] for k in ('run_id','instance_id','patch_sha256')}
        meta.update(dataset=self.inputs['dataset_name'],split=self.inputs['split'])
        meta.update(status='plausible' if self.mode=='pass' else 'failed',
                    official_resolved=self.mode=='pass',feedback='Traceback\nAssertionError: actual != expected')
        if self.mode=='mismatch':meta['patch_sha256']='wrong'
        if self.mode=='invalid_bool':meta['official_resolved']='true'
        if self.mode=='environment':meta.update(status='error',reason='official_harness_no_verdict');meta.pop('official_resolved')
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as z:
            z.writestr('verdict.json',json.dumps(meta))
            z.writestr('../evil.txt','must not extract')
        return stream.getvalue()

class CloudTests(unittest.TestCase):
    def test_diagnostic_opt_in_requires_preflight_before_dispatch(self):
        with tempfile.TemporaryDirectory() as temp:
            api=API()
            result=CloudOfficialRepairValidator('case',temp,'o/r','ref',api=api,diagnostic_traceback=True)(PATCH,None)
            self.assertEqual(result['status'],'error')
            self.assertIn('diagnostic_preflight_unavailable',result['feedback'])
            self.assertFalse(api.calls)

    def test_diagnostic_flag_is_explicit_and_primary_verdict_unchanged(self):
        class DiagnosticAPI(API):
            def preflight(self,ref,diagnostic_traceback=False):
                self.checked=(ref,diagnostic_traceback)
        with tempfile.TemporaryDirectory() as temp:
            api=DiagnosticAPI()
            result=CloudOfficialRepairValidator('case',temp,'o/r','ref',api=api,diagnostic_traceback=True)(PATCH,None)
            self.assertEqual(api.checked,('ref',True))
            self.assertEqual(api.inputs['diagnostic_traceback'],'true')
            self.assertIs(result['official_resolved'],False)
            self.assertEqual(result['status'],'failed')

    def test_diagnostic_flag_defaults_off_and_requires_boolean(self):
        with tempfile.TemporaryDirectory() as temp:
            api=API()
            CloudOfficialRepairValidator('case',temp,'o/r','ref',api=api)(PATCH,None)
            self.assertNotIn('diagnostic_traceback',api.inputs)
            with self.assertRaisesRegex(ValueError,'must be bool'):
                CloudOfficialRepairValidator('case',temp,'o/r','ref',api=api,diagnostic_traceback='false')

    def run_mode(self,mode):
        with tempfile.TemporaryDirectory() as temp:
            api=API(mode)
            result=CloudOfficialRepairValidator('astropy__astropy-14365',temp,'o/r','cloud-v1',api=api)(PATCH,None)
            self.assertFalse((Path(temp).parent/'evil.txt').exists())
            self.assertTrue((Path(result['work_dir'])/'validation.json').exists())
            self.assertEqual(base64.b64decode(api.inputs['patch_b64']),PATCH.encode())
            return result
    def test_failed_assertion_is_feedback_not_environment_error(self):
        result=self.run_mode('failed')
        self.assertEqual(result['status'],'failed');self.assertIs(result['official_resolved'],False)
        self.assertIn('AssertionError',result['feedback'])
    def test_pass_is_real_boolean_and_tracks_run(self):
        result=self.run_mode('pass');self.assertIs(result['official_resolved'],True)
        self.assertEqual(result['cloud_request']['workflow_run_id'],10)
    def test_environment_has_no_resolved(self):
        result=self.run_mode('environment');self.assertEqual(result['status'],'error')
        self.assertNotIn('official_resolved',result)
    def test_wrong_patch_identity_not_accepted(self):
        self.assertEqual(self.run_mode('mismatch')['status'],'error')
    def test_string_bool_not_accepted(self):
        self.assertEqual(self.run_mode('invalid_bool')['status'],'error')
    def test_missing_artifact_not_a_failed_patch(self):
        self.assertEqual(self.run_mode('missing')['status'],'error')
    def test_oversize_and_empty_never_dispatch(self):
        with tempfile.TemporaryDirectory() as temp:
            api=API()
            validator=CloudOfficialRepairValidator('x',temp,'o/r','ref',api=api)
            for text in ('','x'*35001):self.assertEqual(validator(text,None)['status'],'error')
            self.assertFalse(api.calls)
    def test_missing_token_prevents_model_spending(self):
        with patch.dict('os.environ',{},clear=True):
            with self.assertRaisesRegex(ValueError,'github_token_missing'):GitHubAPI('o/r')
    def test_authorization_removed_on_redirect(self):
        import urllib.request
        req=urllib.request.Request('https://api.github.com/x',headers={'Authorization':'Bearer SECRET'})
        new=SafeRedirect().redirect_request(req,None,302,'',{},'https://blob.example/artifact')
        self.assertIsNone(new.get_header('Authorization'))
    def test_timeout_does_not_redeliver_dispatch(self):
        class NoRun(API):
            def request(self,path,payload=None,binary=False):
                if 'runs?event=' in path:self.calls.append((path,payload));return {'workflow_runs':[]}
                return super().request(path,payload,binary)
        api=NoRun();times=iter([0,0,100])
        with tempfile.TemporaryDirectory() as temp:
            result=CloudOfficialRepairValidator('x',temp,'o/r','ref',api=api,wait_timeout=30,
                sleep=lambda n:None,clock=lambda:next(times))(PATCH,None)
        self.assertEqual(result['status'],'error')
        self.assertEqual(sum(p.endswith('/dispatches') for p,_ in api.calls),1)

    def test_old_workflow_rejected_before_generation(self):
        api=GitHubAPI('o/r',token='test-only')
        with patch.object(api,'request',side_effect=[{}, {'content':base64.b64encode(b'predictions_path: gold').decode()}]):
            with self.assertRaisesRegex(ValueError,'not_deployed'):api.preflight('old')

    def test_actual_bridge_prepares_saved_patch_and_collects_verdict(self):
        import subprocess,os
        bridge=Path(__file__).resolve().parents[1]/'scripts/cloud_official_bridge.py'
        env=dict(os.environ,RUN_ID='offline-1',INSTANCE_ID='astropy__astropy-14365',
            PATCH_B64=base64.b64encode(PATCH.encode()).decode(),PATCH_SHA256=hashlib.sha256(PATCH.encode()).hexdigest(),
            DATASET_NAME='SWE-bench/SWE-bench',DATASET_SPLIT='test',HARNESS_OUTCOME='success')
        with tempfile.TemporaryDirectory() as temp:
            subprocess.run([sys.executable,str(bridge),'prepare'],env=env,cwd=temp,check=True,capture_output=True)
            prediction=json.loads((Path(temp)/'predictions.jsonl').read_text())
            self.assertEqual(prediction['model_patch'],PATCH)
            logs=Path(temp)/'logs/run_evaluation/offline-1/iterative-repair/astropy__astropy-14365'
            logs.mkdir(parents=True)
            (logs/'report.json').write_text(json.dumps({'astropy__astropy-14365':{'resolved':False}}))
            (logs/'test_output.txt').write_text('Traceback\nAssertionError: unexpected command')
            subprocess.run([sys.executable,str(bridge),'collect'],env=env,cwd=temp,check=True,capture_output=True)
            verdict=json.loads((Path(temp)/'official-artifacts/verdict.json').read_text())
            self.assertIs(verdict['official_resolved'],False)
            self.assertIn('AssertionError',verdict['feedback'])
            (logs/'report.json').unlink()
            subprocess.run([sys.executable,str(bridge),'collect'],env=env,cwd=temp,check=True,capture_output=True)
            verdict=json.loads((Path(temp)/'official-artifacts/verdict.json').read_text())
            self.assertEqual(verdict['status'],'error')
            self.assertNotIn('official_resolved',verdict)

    def test_batch_cloud_factory_and_resume_identity(self):
        from scripts.run_stage4_batch import build_parser,make_generator,build_run_config
        args=build_parser().parse_args(['--tickets','tickets','--localizations','locations',
            '--output','runs/cloud-test','--candidates','1','--repair-cloud-repo','o/r',
            '--repair-cloud-ref','cloud-ref'])
        with patch('fim_patch.cloud_official.cloud_factory',return_value=lambda ticket:None) as factory:
            repair=make_generator(args)
            self.assertIsNotNone(repair.validator_factory)
            factory.assert_called_once()
        config=build_run_config(args,[])
        self.assertEqual(config['repair_cloud_repo'],'o/r')
        self.assertEqual(config['repair_cloud_ref'],'cloud-ref')
        self.assertEqual(config['cloud_official'],'github-actions-v1')
        self.assertNotIn('GH_TOKEN',json.dumps(config))

    def test_cloud_local_conflict_rejected_before_calls(self):
        from scripts.run_stage4_batch import build_parser,make_generator
        args=build_parser().parse_args(['--tickets','tickets','--localizations','locations',
            '--output','runs/cloud-test','--candidates','1','--repair-cloud-repo','o/r','--repair-official'])
        with self.assertRaisesRegex(ValueError,'Choose cloud'):make_generator(args)

    def test_cloud_failure_reaches_next_fim_round_and_pass_stops(self):
        from tests import test_fim
        from fim_patch.generator import FIMPatchGenerator
        from fim_patch.iterative import IterativeRepairGenerator
        test_fim.PatchTests.setUp(self)
        class Backend(test_fim.FakeBackend):
            def infill(this,prefix,suffix,**kw):
                this.prompts.append(prefix)
                return super().infill(prefix,suffix,**kw)
        backend=Backend(['    return "" if x is None else x.strip()\n',
                         '    return "" if x is None else str(x).strip()\n'])
        backend.prompts=[]
        class FailThenPass(API):
            def request(this,path,payload=None,binary=False):
                if path.endswith('/dispatches'):
                    this.mode='pass' if hasattr(this,'inputs') else 'failed'
                return super().request(path,payload,binary)
        api=FailThenPass()
        validator=CloudOfficialRepairValidator('test-1',self.repo/'cloud','o/r','ref',api=api)
        inner=FIMPatchGenerator(backend,candidates=1,prompt_layout='compact_fim')
        result=IterativeRepairGenerator(inner,3,lambda ticket:validator).generate(self.ticket,self.loc,self.repo)
        self.assertEqual(result['repair_stop_reason'],'tests_passed')
        self.assertEqual(backend.calls,2)
        self.assertIn('AssertionError',backend.prompts[1])
        self.assertIs(result['official_resolved'],True)
        self.assertEqual((self.repo/'app.py').read_bytes(),self.raw)

if __name__=='__main__':unittest.main()
