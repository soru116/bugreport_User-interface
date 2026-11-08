from dataclasses import asdict
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).resolve().parents[1]/'experiments/generalization_v1'),str(Path(__file__).resolve().parents[1])]
from prepare_source_api_pilot import select_cases
from fim_patch.paired_repair import ARMS,PAIRS,PairedRepairGenerator
from fim_patch.unified_repair import RepairPolicy
from tests import test_paired_repair as fixtures
from run_paired_repair import first_request_preview


class PilotSelectionTests(unittest.TestCase):
    def test_fixed_order_not_success(self):
        tickets=[dict(ticket_id=x,resolved=(x=='b')) for x in ('a','b','c')]
        coverage=[dict(ticket_id='b',context={'status':'injected'}),dict(ticket_id='a',context={'status':'injected'}),
                  dict(ticket_id='c',context={'status':'unknown_no_resolved_signatures'})]
        self.assertEqual([r['ticket_id'] for r in select_cases(tickets,coverage)],['a','c'])

    def test_missing_request_does_not_replace_unknown_category(self):
        with self.assertRaises(ValueError):select_cases([{'ticket_id':'a'}],[dict(ticket_id='a',status='unknown_missing_saved_request')])

    def test_pair_diff_only_one_factor(self):
        off,on=PAIRS['source_api_contracts'];a=asdict(ARMS[off]);b=asdict(ARMS[on])
        self.assertEqual([k for k in a if a[k]!=b[k]],['source_api_contracts'])
        self.assertTrue(a['test_feedback'] and b['test_feedback'])
        self.assertEqual(RepairPolicy().max_generation_calls,3)

    def test_arm_switch_overrides_policy_without_mutation(self):
        p=RepairPolicy(source_api_contracts=True)
        off=PairedRepairGenerator(object(),'A4_api_off',p)
        on=PairedRepairGenerator(object(),'A4_api_on',p)
        legacy=PairedRepairGenerator(object(),'A4',p)
        self.assertFalse(off.policy.source_api_contracts);self.assertTrue(on.policy.source_api_contracts)
        self.assertTrue(legacy.policy.source_api_contracts);self.assertTrue(p.source_api_contracts)


class PilotPreviewTests(unittest.TestCase):
    setUp=fixtures.PairedTests.setUp
    replace_source=fixtures.PairedTests.replace_source
    engine=fixtures.PairedTests.engine
    def test_offline_pair_preview_no_network_same_suffix_seed_output(self):
        self.replace_source(b'def helper(value):\n    return value\n\ndef parse(value):\n    return helper(value)\n','parse',4,5)
        self.ticket['description']='Repair parse behavior.'
        backend,_=self.engine('A4',[]);backend.num_ctx=16384
        model={'model':'mock','expected_digest':'pinned'};previews=[]
        with patch('urllib.request.urlopen',side_effect=AssertionError('network forbidden')):
            for arm in PAIRS['source_api_contracts']:
                previews.append(first_request_preview(backend,arm,RepairPolicy(),self.ticket,self.loc,self.repo,model))
        off,on=previews
        self.assertEqual(off['status'],'captured');self.assertEqual(on['status'],'captured')
        self.assertEqual(off['suffix'],on['suffix']);self.assertEqual(off['seed'],on['seed'])
        self.assertEqual(off['output_limit'],on['output_limit'])
        self.assertIn('SOURCE API EVIDENCE',on['prefix']);self.assertNotIn('SOURCE API EVIDENCE',off['prefix'])
        self.assertTrue(on['source_api_context']['injected'])
        self.assertEqual(off['real_model_calls']+on['real_model_calls'],0)


if __name__=='__main__':unittest.main()
