import sys
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'experiments/generalization_v1'),str(ROOT)]
from prepare_summary_evaluation import select_diagnostic,conflict_fixture_audit


class ProtocolTests(unittest.TestCase):
    def test_selection_uses_original_order_and_routes_not_results(self):
        tickets=[dict(ticket_id=k,repo=r) for k,r in (('a','r1'),('b','r2'),('c','r2'),('d','r3'))]
        coverage=[dict(ticket_id=k,summary_first_request=s,route=route) for k,s,route in
                  (('a',False,'single'),('b',True,'single'),('c',True,'single'),('d',True,'single'))]
        self.assertEqual(select_diagnostic(tickets,coverage),['b','d'])
        self.assertEqual(len(tickets),4)

    def test_rule_audit_reports_abstentions_as_missed_positives(self):
        result=conflict_fixture_audit()
        self.assertEqual(result['positive_count'],6)
        self.assertEqual(result['matched_positive'],3)
        self.assertEqual(result['missed_or_abstained_positive'],3)
        self.assertEqual(result['false_positive'],0)
        self.assertEqual(result['model_calls'],0)
        self.assertTrue(all(r['matches_expected'] for r in result['records']))
        self.assertTrue(all(r['inferred_replacement'] is None for r in result['records']))


if __name__=='__main__':unittest.main()
