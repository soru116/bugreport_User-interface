import sys
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'experiments/generalization_v1'),str(ROOT)]
from diagnose_official_failures import frames,added_lines,classify,api_audit,preview


class OfficialDiagnosticTests(unittest.TestCase):
    def test_frames_exclude_external_and_traversal(self):
        text='File "/testbed/pkg/api.py", line 18\nFile "/usr/lib/python3/a.py", line 20\n../secret.py:12:\npkg/api.py:18:\n'
        self.assertEqual(frames(text),[dict(file='pkg/api.py',line=18)])

    def test_diff_tracks_new_coordinates_and_excludes_context(self):
        patch='--- a/pkg/api.py\n+++ b/pkg/api.py\n@@ -10,3 +10,4 @@\n same\n-old\n+new\n+extra\n same\n'
        self.assertEqual(added_lines(patch),{'pkg/api.py':{11,12}})

    def test_preparation_is_not_an_assertion_failure(self):
        report=dict(patch_successfully_applied=False,resolved=False)
        d=classify(report,"AttributeError: 'Receiver' object has no attribute 'missing'")
        self.assertEqual(d['category'],'preparation_or_apply_unresolved')
        self.assertEqual(d['missing_attribute'],dict(receiver_type='Receiver',attribute='missing'))
        self.assertFalse(d['root_cause_verified'])

    def test_regressions_separate_from_required_failures(self):
        report=dict(patch_successfully_applied=True,tests_status={'FAIL_TO_PASS':{'failure':['new_test']},'PASS_TO_PASS':{'failure':['old_test']}})
        d=classify(report,'AssertionError')
        self.assertEqual(d['category'],'regression_and_unfixed_tests')
        self.assertEqual(d['pass_to_pass'],['old_test'])

    def test_source_inheritance_members_not_replacement_guesses(self):
        raw=b'class Base:\n    def present(self): pass\nclass Child(Base):\n    pass\n'
        d=api_audit({'pkg/api.py':raw},dict(receiver_type='Child',attribute='present'),'')
        self.assertEqual(d['status'],'member_found_in_source_review_runtime_binding')
        d=api_audit({'pkg/api.py':raw},dict(receiver_type='Child',attribute='missing'),'+x.missing()')
        self.assertEqual(d['status'],'not_found_in_available_source')
        self.assertTrue(d['absence_is_not_static_proof']);self.assertIsNone(d['inferred_replacement'])

    def test_feedback_bounded_sanitized_and_budget_stops(self):
        d=classify(dict(patch_successfully_applied=False),'<MID> unknown')
        d.update(scope_audit={'status':'unknown'},feedback_excerpt='<MID> unknown')
        p=preview(d,{'remaining':0},500)
        self.assertEqual(p['action'],'stop_shared_budget_exhausted')
        self.assertNotIn('<MID>',p['rendered']);self.assertLessEqual(p['chars'],500)
        self.assertFalse(p['automatic_retry_enabled'])
        self.assertEqual(preview(d,{'remaining':1})['action'],'review_preparation_and_baseline_before_retry')


if __name__=='__main__':unittest.main()
