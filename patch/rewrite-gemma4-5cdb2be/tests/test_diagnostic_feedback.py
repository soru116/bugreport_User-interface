import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from fim_patch.diagnostic_feedback import bind_diagnostic, replay_candidate
from fim_patch.diagnostic_trace import verbose_script
from fim_patch.iterative import feedback_block


def digest(raw): return hashlib.sha256(raw).hexdigest()


class DiagnosticFeedbackTests(unittest.TestCase):
    def artifact(self, path, invalid=False):
        text=b"TypeError: '<' not supported between instances of 'int' and 'str'\nTraceback context:\napp.py:4: in compare\n"
        parent=dict(run_id='new-run',instance_id='synthetic',patch_sha256=digest(b'patch'),dataset='data',split='test',status='failed',official_resolved=False)
        raw=json.dumps(parent).encode()
        identity={k:parent[k] for k in ('run_id','instance_id','patch_sha256','dataset','split')}
        _,transform=verbose_script('pytest --tb=no tests/a.py\n')
        diag=dict(version='diagnostic-traceback-v1',status='completed',harness_version='5.0.2',identity=identity,
            diagnostic_run_id='new-run-diagnostic',official_resolved=None,primary_official_resolved=False,
            official_result_overridden=False,test_set_changed=False,transformations=[transform],
            primary_verdict_sha256='invalid' if invalid else digest(raw))
        manifest=dict(version='separated-official-logs-v1',identity=identity,files={'test_output.txt':dict(present=True,source='test_output.txt',captured_bytes=len(text),captured_sha256=digest(text)), 'run_instance.log':dict(present=False)})
        with zipfile.ZipFile(path,'w') as z:
            z.writestr('verdict.json',raw)
            z.writestr('diagnostic/diagnostic.json',json.dumps(diag))
            z.writestr('diagnostic/logs_manifest.json',json.dumps(manifest))
            z.writestr('diagnostic/test_output.txt',text)
        old=dict(identity,run_id='old-run')
        return dict(patch='patch',middle='old_body()',reason='validation_failed',validation=dict(cloud_request=old,status='failed',official_resolved=False,feedback='old'))

    def test_binding_requires_exact_patch_but_allows_saved_patch_retest_run(self):
        with tempfile.TemporaryDirectory() as temp:
            file=Path(temp)/'evidence.zip';candidate=self.artifact(file);before=copy.deepcopy(candidate)
            bound=bind_diagnostic(candidate,file)
            self.assertEqual(candidate,before)
            self.assertIs(bound['validation']['official_resolved'],False)
            self.assertEqual(bound['validation']['cloud_request']['run_id'],'new-run')
            self.assertTrue(bound['validation']['diagnostic_failure_details_verified'])
            candidate['patch']='different'
            with self.assertRaisesRegex(ValueError,'differs'):bind_diagnostic(candidate,file)

    def test_wrong_identity_or_unverified_evidence_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            file=Path(temp)/'evidence.zip';candidate=self.artifact(file)
            candidate['validation']['cloud_request']['instance_id']='another'
            with self.assertRaisesRegex(ValueError,'identity mismatch'):bind_diagnostic(candidate,file)
            candidate=self.artifact(file,True)
            with self.assertRaisesRegex(ValueError,'unavailable'):bind_diagnostic(candidate,file)

    def test_compact_and_full_feedback_keep_provenance_exception_and_trace(self):
        with tempfile.TemporaryDirectory() as temp:
            file=Path(temp)/'evidence.zip';candidate=bind_diagnostic(self.artifact(file),file)
            for compact in (True,False):
                feedback=feedback_block(candidate,compact=compact)
                self.assertIn('Verified secondary test diagnostic',feedback)
                self.assertIn('TypeError',feedback)
                self.assertIn('app.py:4',feedback)
                self.assertTrue(all(not l.strip() or l.startswith('#') for l in feedback.splitlines()))

    def test_unverified_diagnostic_text_is_not_forwarded_as_confirmed(self):
        candidate=dict(validation=dict(feedback_source='diagnostic_test_output',feedback='AssertionError: fabricated',diagnostic_failure_details_verified=False))
        for compact in (True,False):
            text=feedback_block(candidate,compact=compact)
            self.assertIn('unverified',text)
            self.assertNotIn('fabricated',text)

    def test_restore_outer_context_only_with_saved_matching_hashes(self):
        candidate=dict(prompt='prefix',suffix='suffix')
        context=dict(injected=True,context_block='context',base_prefix_sha256=digest(b'prefix'),suffix_sha256=digest(b'suffix'),prefix_sha256=digest(b'contextprefix'))
        self.assertEqual(replay_candidate(candidate,context)['prompt'],'contextprefix')
        self.assertEqual(candidate['prompt'],'prefix')
        context['context_block']='changed'
        with self.assertRaisesRegex(ValueError,'context mismatch'):replay_candidate(candidate,context)


if __name__=='__main__': unittest.main()
