import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from fim_patch.failure_identity import failure_evidence
from fim_patch.iterative import test_failure_excerpt,feedback_block
from fim_patch.cloud_official import CloudOfficialRepairValidator
from tests.test_cloud_official import API,PATCH


class IdentityTests(unittest.TestCase):
    def test_transport_changes_do_not_change_failure(self):
        a=dict(status='failed',official_resolved=False,feedback='AssertionError: expected 3\n/tmp/pip-build-env-abc123/lib/app.py:9:',
               work_dir='a',cloud_request=dict(run_id='one',workflow_run_id=1,patch_sha256='text-a',instance_id='case',dataset='data',head_sha='fixed'))
        b=copy.deepcopy(a);b['work_dir']='b';b['cloud_request'].update(run_id='two',workflow_run_id=2,patch_sha256='text-b')
        b['feedback']=a['feedback'].replace('abc123','def456');original=copy.deepcopy(a)
        self.assertEqual(failure_evidence(a),failure_evidence(b));self.assertEqual(a,original)

    def test_meaningful_changes_and_domain_run_id_retained(self):
        a=dict(status='failed',feedback='AssertionError: expected 3',expected=dict(run_id='domain-one'))
        b=copy.deepcopy(a);b['expected']['run_id']='domain-two'
        self.assertNotEqual(failure_evidence(a),failure_evidence(b))
        for field,value in [('status','error'),('feedback','AssertionError: expected 4')]:
            b=copy.deepcopy(a);b[field]=value;self.assertNotEqual(failure_evidence(a),failure_evidence(b))
        for head in ('one','two'):
            a['cloud_request']=dict(head_sha=head)
            if head=='one':first=failure_evidence(a)
            else:self.assertNotEqual(first,failure_evidence(a))

    def test_assertion_values_containing_temp_paths_are_not_normalized(self):
        a=dict(status='failed',feedback='AssertionError: expected /tmp/tmpabcdef/file.py but got None')
        b=dict(status='failed',feedback='AssertionError: expected /tmp/tmp/tmpghijkl/file.py but got None')
        self.assertNotEqual(failure_evidence(a),failure_evidence(b))

    def test_failed_expected_exception_survives_double_compaction(self):
        log="E           Failed: DID NOT RAISE <class 'TypeError'>\nFAILED tests/a.py::test_scalar - Failed: DID NOT RAISE <class 'TypeError'>\n"
        excerpt=test_failure_excerpt(log)
        self.assertIn("DID NOT RAISE <class 'TypeError'>",excerpt)
        text=feedback_block(dict(reason='validation_failed',validation=dict(feedback=excerpt)),compact=True)
        self.assertIn("DID NOT RAISE <class 'TypeError'>",text)
        self.assertIn('tests/a.py::test_scalar',text)

    def test_warning_as_error_not_ordinary_warning(self):
        log='lib.py:2: DeprecationWarning: ordinary\nE   DeprecationWarning: raised\nERROR tests/a.py\n'
        excerpt=test_failure_excerpt(log)
        self.assertIn('Warning-as-error: DeprecationWarning: raised',excerpt)
        self.assertNotIn('DeprecationWarning: ordinary',excerpt)
        self.assertIn('Warning-as-error: DeprecationWarning: raised',test_failure_excerpt(excerpt))

    def test_diff_echo_not_failure(self):
        log='--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n+E Failed: DID NOT RAISE\n+E DeprecationWarning: example\n'
        self.assertEqual(test_failure_excerpt(log),'')

    def test_cloud_prefers_log_and_keeps_raw_verdict(self):
        class LogAPI(API):
            def request(self,path,payload=None,binary=False):
                data=super().request(path,payload,binary)
                if not binary:return data
                original=zipfile.ZipFile(io.BytesIO(data));out=io.BytesIO()
                with zipfile.ZipFile(out,'w') as z:
                    for name in original.namelist():z.writestr(name,original.read(name))
                    z.writestr('test_output.txt',"E Failed: DID NOT RAISE <class 'TypeError'>\nFAILED tests/a.py::test_scalar\n")
                return out.getvalue()
        with tempfile.TemporaryDirectory() as d:
            result=CloudOfficialRepairValidator('case',d,'o/r','ref',api=LogAPI())(PATCH,None)
            self.assertEqual(result['status'],'failed');self.assertIs(result['official_resolved'],False)
            self.assertEqual(result['feedback_source'],'artifact_test_output')
            self.assertIn('DID NOT RAISE',result['feedback'])
            raw=json.loads((Path(result['work_dir'])/'verdict.json').read_text())
            self.assertNotEqual(raw['feedback'],result['feedback'])


if __name__=='__main__':unittest.main()
