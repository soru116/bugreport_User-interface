import sys
from pathlib import Path
import unittest
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).resolve().parents[1]/'experiments/generalization_v1')]
from fim_patch.unified_repair import UnifiedRepairGenerator,RepairPolicy
from fim_patch.iterative import feedback_block
from fim_patch.paired_repair import PairedRepairGenerator
from preview_api_argument_flow_retry import Capture


class RetryTests(unittest.TestCase):
    def candidate(self):return dict(patch='diff',reason='validation_failed',validation=dict(status='failed',feedback='TypeError: bad argument'))
    def test_default_does_not_analyze_or_change_feedback(self):
        class Adapter:
            def argument_flow(self,diff):raise AssertionError('default must not analyze')
        engine=UnifiedRepairGenerator(None);engine._argument_flow_adapter=Adapter();c=self.candidate()
        engine.annotate_argument_flow(c)
        self.assertNotIn('api_argument_flow',c)
        self.assertEqual(engine.repair_feedback(c),feedback_block(c,compact=True))

    def test_on_is_advisory_and_does_not_change_candidate_status(self):
        class Adapter:
            def argument_flow(self,diff):return dict(feedback_preview='# API value-flow evidence: unknown paths\n',advisory_only=True,automatic_rejection=False)
        engine=UnifiedRepairGenerator(None,RepairPolicy(api_argument_flow_feedback=True));engine._argument_flow_adapter=Adapter();c=self.candidate()
        engine.annotate_argument_flow(c)
        self.assertTrue(engine.repair_feedback(c).endswith('# API value-flow evidence: unknown paths\n'))
        self.assertEqual(c['reason'],'validation_failed');self.assertEqual(c['validation']['status'],'failed');self.assertEqual(c['patch'],'diff')

    def test_unknown_evidence_does_not_create_false_pass_or_feedback(self):
        class Adapter:
            def argument_flow(self,diff):raise ValueError('unresolved')
        engine=UnifiedRepairGenerator(None,RepairPolicy(api_argument_flow_feedback=True));engine._argument_flow_adapter=Adapter();c=self.candidate()
        engine.annotate_argument_flow(c)
        self.assertEqual(c['api_argument_flow']['status'],'unknown')
        self.assertEqual(engine.repair_feedback(c),feedback_block(c,compact=True))

    def test_feedback_disabled_arm_remains_disabled(self):
        engine=PairedRepairGenerator(None,'A3',RepairPolicy(api_argument_flow_feedback=True))
        c=self.candidate();c['api_argument_flow']=dict(feedback_preview='# advisory\n')
        self.assertEqual(engine.repair_feedback(c),'')

    def test_switch_rejects_non_bool_and_does_not_change_request_budget(self):
        with self.assertRaisesRegex(ValueError,'must be bool'):RepairPolicy(api_argument_flow_feedback='true')
        self.assertEqual(RepairPolicy(api_argument_flow_feedback=True).max_generation_calls,3)

    def test_capture_is_one_request_and_never_dispatches(self):
        class Counter:
            def count(self,p,s):return len(p)+len(s)
        b=Capture(dict(num_ctx=100,max_new_tokens=20,model='m',expected_digest='d'),Counter())
        with self.assertRaisesRegex(ValueError,'offline_capture'):b.infill('p','s',seed=4,max_new_tokens=20)
        self.assertEqual(b.request['input_tokens'],2);self.assertTrue(b.request['fits'])
        with self.assertRaisesRegex(AssertionError,'cap'):b.infill('p','s',seed=4,max_new_tokens=20)
        with self.assertRaisesRegex(AssertionError,'forbidden'):b.post('generate',{})


if __name__=='__main__':unittest.main()
