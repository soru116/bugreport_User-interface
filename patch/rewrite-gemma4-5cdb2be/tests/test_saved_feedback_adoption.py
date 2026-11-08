import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'experiments/generalization_v1'))
from audit_saved_feedback_adoption import audit_transition
from fim_patch.iterative import feedback_block


class AdoptionTests(unittest.TestCase):
    def previous(self):return dict(reason='validation_failed',executable_logic_sha256='a',validation=dict(status='failed',official_resolved=False,feedback='TypeError: incompatible\nFAILED tests/a.py::test_a'))
    def next(self,p):return dict(prompt=feedback_block(p,compact=True)+'# Source context follows\n',raw_response='new code',executable_logic_sha256='b',apply_check_status='passed',patch='diff',validation=dict(status='failed',official_resolved=False,feedback='TypeError: incompatible\nFAILED tests/a.py::test_a'))

    def test_exact_sent_feedback_and_repeat_signal_not_root_cause_proof(self):
        p=self.previous();r=audit_transition(p,self.next(p))
        self.assertTrue(r['feedback_in_observed_next_request'])
        self.assertEqual(r['repeated_exception_signals'],['TypeError: incompatible'])
        self.assertFalse(r['same_executable_logic']);self.assertFalse(r['root_cause_verified'])

    def test_issue_text_only_is_not_feedback_delivery(self):
        p=self.previous();n=self.next(p);n['prompt']='# ISSUE REPORT\n# TypeError: incompatible\n# Source context follows\n'
        r=audit_transition(p,n)
        self.assertFalse(r['feedback_in_observed_next_request'])

    def test_control_arm_and_absent_model_output_are_not_adoption(self):
        p=self.previous();n=self.next(p)
        self.assertEqual(audit_transition(p,n,expected_feedback=False)['feedback_presence'],'control_feedback_disabled')
        self.assertFalse(audit_transition(p,n,expected_feedback=False)['feedback_in_observed_next_request'])
        n['raw_response']=''
        self.assertFalse(audit_transition(p,n)['feedback_in_observed_next_request'])

    def test_new_offline_signal_not_claimed_as_originally_sent(self):
        p=self.previous();r=audit_transition(p,self.next(p),replayed=dict(new_feedback="Failed: DID NOT RAISE <class 'TypeError'>"))
        self.assertEqual(r['offline_richer_signals_missing_from_sent_segment'],["Failed: DID NOT RAISE <class 'TypeError'>"])

    def test_unvalidated_next_patch_cannot_be_official_repeat(self):
        p=self.previous();n=self.next(p);n['validation']={};n['patch']='';n['apply_check_status']='failed'
        r=audit_transition(p,n)
        self.assertIsNone(r['next_official_resolved']);self.assertFalse(r['next_has_static_patch'])
        self.assertEqual(r['repeated_failed_test_ids'],[])


if __name__=='__main__':unittest.main()
