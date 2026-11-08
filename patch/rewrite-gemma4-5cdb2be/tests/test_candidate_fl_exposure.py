from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'experiments/generalization_v1'),str(ROOT/'src'),str(ROOT)]
from audit_candidate_fl_exposure import development_membership, id_paths, approved_path


class ExposureEvidenceTests(unittest.TestCase):
    def test_exact_membership_in_nested_arrays_and_absent_stays_absent(self):
        metrics=dict(rows=3,matched_prediction_rows=3,missing_prediction_rows=0,
            candidate_outcome_ticket_ids_at_20={'group_a':['p__q-1','p__q-2'],'group_b':['p__q-3']})
        result=development_membership(metrics,{'p__q-2','p__q-4'})
        self.assertEqual(result,{'p__q-2':[['candidate_outcome_ticket_ids_at_20','group_a','1']]})

    def test_scores_and_outcome_group_do_not_select_or_replace_ids(self):
        self.assertEqual(set(id_paths({'a':['p__q-1'],'score':0.0},{'p__q-1'})),
                         set(id_paths({'b':['p__q-1'],'score':1.0},{'p__q-1'})))
        self.assertEqual(id_paths({'note':'similar p__q-1 text'},{'p__q-1'}),{})

    def test_incomplete_metrics_not_confused_with_complete_exposure(self):
        with self.assertRaises(ValueError):development_membership(
            dict(rows=2,matched_prediction_rows=1,missing_prediction_rows=1,
                 candidate_outcome_ticket_ids_at_20={'a':['p__q-1']}),{'p__q-1'})

    def test_missing_arrays_or_reserved_evidence_refused(self):
        with self.assertRaises(ValueError):development_membership(
            dict(rows=1,matched_prediction_rows=1,missing_prediction_rows=0),{'p__q-1'})
        for name in ('sealed/metrics.json','final_holdout/result.json','gold.json'):
            with self.assertRaises(ValueError):approved_path(Path(name))


if __name__=='__main__':unittest.main()
