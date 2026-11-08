import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from fim_patch.diagnostic_trace import verbose_script
from fim_patch.cloud_log_evidence import read_archive_feedback

spec=importlib.util.spec_from_file_location('cloud_traceback_diagnostic',ROOT/'scripts/cloud_traceback_diagnostic.py')
driver=importlib.util.module_from_spec(spec);spec.loader.exec_module(driver)


class TransformTests(unittest.TestCase):
    def test_only_option_changes(self):
        source='set -e\npytest -rA -vv --tb=no tests/a.py::test_x\n'
        changed,record=verbose_script(source)
        self.assertEqual(changed,source.replace('--tb=no','--tb=short'))
        self.assertFalse(record['test_set_changed']);self.assertFalse(record['patch_changed'])

    def test_heredoc_test_patch_and_comments_untouched(self):
        source="git apply - <<'PATCH_EOF'\npytest --tb=no secret_test\nPATCH_EOF\n# pytest --tb=no\npytest --tb=no tests/a.py\n"
        changed,record=verbose_script(source)
        self.assertEqual(changed.count('--tb=no'),2);self.assertEqual(len(record['changes']),1)
        self.assertIn('pytest --tb=short tests/a.py',changed)

    def test_python_module_invocation_supported(self):
        changed,record=verbose_script('python -m pytest --tb=no tests/b.py\n')
        self.assertEqual(record['status'],'modified');self.assertIn('--tb=short',changed)

    def test_unsupported_or_unsafe_preserved(self):
        for source in ('unittest tests/a.py\n','pytest --tb=short tests/a.py\n','pytest --tb=no tests/a.py; echo done\n','pytest "--tb=no" tests/a.py\n'):
            changed,record=verbose_script(source)
            self.assertEqual(changed,source);self.assertEqual(record['status'],'unsupported_no_safe_pytest_traceback_option')

    def test_unterminated_heredoc_rejected(self):
        with self.assertRaises(ValueError):verbose_script('cat <<EOF\npytest --tb=no\n')


class DriverTests(unittest.TestCase):
    def inputs(self,root,resolved=False):
        primary=root/'verdict.json';prediction=root/'predictions.jsonl';text='--- a/a.py\n+++ b/a.py\n'
        value=dict(run_id='repair-x',instance_id='case',patch_sha256=hashlib.sha256(text.encode()).hexdigest(),dataset='data',split='test',
                   status='plausible' if resolved else 'failed',official_resolved=resolved)
        primary.write_text(json.dumps(value),encoding='utf8')
        prediction.write_text(json.dumps(dict(instance_id='case',model_name_or_path='iterative-repair',model_patch=text))+'\n',encoding='utf8')
        return primary,prediction,value

    def test_plan_does_not_import_or_run_harness(self):
        with tempfile.TemporaryDirectory() as temp:
            primary,prediction,_=self.inputs(Path(temp))
            with patch.object(driver.importlib,'import_module',side_effect=AssertionError('no imports')):
                plan=driver.prepare(primary,prediction)
            self.assertEqual(plan['status'],'planned');self.assertEqual(plan['diagnostic_harness_runs'],0)
            self.assertIsNone(plan['official_resolved'])

    def test_primary_pass_skips_diagnosis_and_patch_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            primary,prediction,_=self.inputs(Path(temp),True)
            self.assertEqual(driver.prepare(primary,prediction)['status'],'skipped_primary_not_failed')
            row=json.loads(prediction.read_text());row['model_patch']='different';prediction.write_text(json.dumps(row))
            with self.assertRaises(ValueError):driver.prepare(primary,prediction)

    def test_mock_harness_verbose_replay_never_promotes_secondary_pass(self):
        source=ROOT/'runs/diagnostic-mode-source-review-v1/swebench/harness/run_evaluation.py'
        if not source.exists():self.skipTest('pinned source inspection file unavailable')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);primary,prediction,value=self.inputs(root);before=primary.read_bytes()
            plan=driver.prepare(primary,prediction);out=root/'diagnostic';out.mkdir()
            original=lambda script,restore:script
            fake=SimpleNamespace(__file__=str(source),_inject_asset_restore=original)
            def main(**kwargs):
                script=fake._inject_asset_restore('pytest --tb=no tests/a.py\n',[])
                self.assertIn('--tb=short',script);self.assertEqual(kwargs['instance_ids'],['case'])
                logs=root/'logs/run_evaluation'/plan['diagnostic_run_id']/'iterative-repair/case';logs.mkdir(parents=True)
                (logs/'test_output.txt').write_text('E assert actual == expected\nFAILED tests/a.py::test_x\n')
                (logs/'report.json').write_text(json.dumps({'case':{'resolved':True,'tests_status':{}}}))
            fake.main=main;previous=Path.cwd()
            try:
                os.chdir(root)
                with patch.object(driver.importlib.metadata,'version',return_value='5.0.2'),patch.object(driver.importlib,'import_module',return_value=fake):
                    result=driver.execute(plan,primary,prediction,out,60)
            finally:os.chdir(previous)
            self.assertEqual(result['status'],'completed');self.assertIsNone(result['official_resolved'])
            self.assertIs(result['primary_official_resolved'],False);self.assertEqual(primary.read_bytes(),before)
            self.assertIs(fake._inject_asset_restore,original);self.assertIn('assert actual == expected',result['feedback'])

    def test_version_mismatch_blocks_before_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);primary,prediction,_=self.inputs(root);out=root/'diag';out.mkdir();plan=driver.prepare(primary,prediction)
            with patch.object(driver.importlib.metadata,'version',return_value='other'):
                result=driver.execute(plan,primary,prediction,out,60)
            self.assertEqual(result['status'],'blocked_or_error');self.assertEqual(result['diagnostic_harness_runs'],0)


class DiagnosticArtifactTests(unittest.TestCase):
    def archive(self,tamper=False):
        parent=dict(run_id='r',instance_id='case',patch_sha256='p',dataset='d',split='test',status='failed',official_resolved=False)
        raw=json.dumps(parent).encode();text=b'E assert 1 == 2\nFAILED tests/a.py::test_a\n'
        _,transform=verbose_script('pytest --tb=no tests/a.py\n')
        diagnostic=dict(version='diagnostic-traceback-v1',status='completed',harness_version='5.0.2',identity=parent,
            diagnostic_run_id='r-diagnostic',official_resolved=None,official_result_overridden=False,test_set_changed=False,
            primary_official_resolved=False,primary_verdict_sha256=hashlib.sha256(raw).hexdigest(),transformations=[transform])
        if tamper:diagnostic['primary_verdict_sha256']='other'
        manifest=dict(version='separated-official-logs-v1',identity=parent,files={'test_output.txt':dict(present=True,source='test_output.txt',
            captured_bytes=len(text),captured_sha256=hashlib.sha256(text).hexdigest()),'run_instance.log':dict(present=False)})
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as z:
            z.writestr('verdict.json',raw);z.writestr('diagnostic/diagnostic.json',json.dumps(diagnostic))
            z.writestr('diagnostic/logs_manifest.json',json.dumps(manifest));z.writestr('diagnostic/test_output.txt',text)
        return parent,zipfile.ZipFile(io.BytesIO(stream.getvalue()))

    def test_opt_in_uses_diagnostic_text_never_official_boolean(self):
        parent,z=self.archive()
        with z:r,logs,_=read_archive_feedback(z,parent,allow_diagnostic=True)
        self.assertEqual(r['feedback_source'],'diagnostic_test_output');self.assertTrue(r['diagnostic_failure_details_verified'])
        self.assertNotIn('official_resolved',r);self.assertIn('diagnostic/test_output.txt',logs)
        self.assertFalse(r['failure_details_verified'])

    def test_default_off_and_wrong_parent_not_used(self):
        parent,z=self.archive()
        with z:r,_,_=read_archive_feedback(z,parent)
        self.assertNotEqual(r['feedback_source'],'diagnostic_test_output')
        parent,z=self.archive(True)
        with z:r,_,_=read_archive_feedback(z,parent,allow_diagnostic=True)
        self.assertNotEqual(r['feedback_source'],'diagnostic_test_output');self.assertIn('diagnostic_issue',r['feedback_provenance'])


if __name__=='__main__':unittest.main()
