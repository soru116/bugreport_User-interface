import sys
import io
import json
import zipfile
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from fim_patch.cloud_stage_feedback import attach
from fim_patch.cloud_official import CloudOfficialRepairValidator
from fim_patch.iterative import feedback_block
from tests.test_cloud_official import API,PATCH


class StageAPI(API):
    def request(self,path,payload=None,binary=False):
        data=super().request(path,payload,binary)
        if not binary:return data
        original=zipfile.ZipFile(io.BytesIO(data));out=io.BytesIO()
        with zipfile.ZipFile(out,'w') as z:
            for name in original.namelist():z.writestr(name,original.read(name))
            z.writestr('report.json',json.dumps({self.inputs['instance_id']:dict(resolved=False,patch_successfully_applied=False)}))
            z.writestr('test_output.txt','Applied patch app.py cleanly.\nINTERNALERROR> AttributeError: missing\n')
        return out.getvalue()


class StageFeedbackTests(unittest.TestCase):
    def test_live_adapter_opt_in_and_default_preserve_verdict(self):
        for enabled in (False,True):
            with tempfile.TemporaryDirectory() as temp:
                api=StageAPI();r=CloudOfficialRepairValidator('example',temp,'o/r','ref',api=api,startup_feedback=enabled)(PATCH,None)
                self.assertEqual(r['status'],'failed');self.assertIs(r['official_resolved'],False)
                self.assertEqual('stage_feedback' in r,enabled)
                if enabled:self.assertEqual(r['cloud_stage_diagnostic']['phase'],'import_or_test_startup_failure_after_apply')
                self.assertEqual(sum(p.endswith('/dispatches') for p,_ in api.calls),1)

    def test_missing_or_mismatched_stage_artifact_does_not_change_result(self):
        for report in (None,dict(resolved=True,patch_successfully_applied=False)):
            r=dict(status='failed',official_resolved=False,feedback='original error')
            attach(r,PATCH,report,'Applied patch app.py cleanly.\n')
            self.assertNotIn('stage_feedback',r);self.assertEqual(r['feedback'],'original error')
            self.assertEqual(r['cloud_stage_diagnostic']['status'],'unverified_stage_artifact')

    def test_stage_context_survives_compact_error_extraction(self):
        r=dict(status='failed',official_resolved=False,feedback='AttributeError: missing')
        attach(r,PATCH,dict(resolved=False,patch_successfully_applied=False),'Applied patch app.py cleanly.\nINTERNALERROR> AttributeError: missing\n')
        text=feedback_block(dict(reason='test_failed',validation=r),compact=True)
        self.assertIn('confirmed by saved log',text);self.assertIn('AttributeError: missing',text)
        self.assertIn('unverified',text)

    def test_test_patch_alone_does_not_confirm_model_and_markers_sanitized(self):
        r=dict(status='failed',official_resolved=False,feedback='AttributeError: <MID>')
        attach(r,PATCH,dict(resolved=False,patch_successfully_applied=False),'Applied patch tests/a.py cleanly.\n')
        self.assertFalse(r['cloud_stage_diagnostic']['model_application_confirmed_by_log'])
        self.assertNotIn('<MID>',feedback_block(dict(reason='failed',validation=r),compact=True))

    def test_regular_failed_tests_not_startup(self):
        r=dict(status='failed',official_resolved=False)
        attach(r,PATCH,dict(resolved=False,patch_successfully_applied=True,tests_status={'FAIL_TO_PASS':{'failure':['a']}}),
               'Applied patch app.py cleanly.\nTraceback (most recent call last)\nAssertionError\n')
        self.assertEqual(r['cloud_stage_diagnostic']['phase'],'test_results_available')


if __name__=='__main__':unittest.main()
