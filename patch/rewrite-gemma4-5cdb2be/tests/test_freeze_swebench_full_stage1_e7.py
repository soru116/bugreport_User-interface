from __future__ import annotations

import unittest

from scripts.freeze_swebench_full_stage1_e7 import validate_e7_selection, validate_holdout_manifest


class FreezeSWEbenchFullStage1E7Test(unittest.TestCase):
    def test_accepts_retained_e7_a_selection(self) -> None:
        selection = {
            "decision": "not_retained",
            "selected_method": "E7-A",
            "selected_runner_method": "e7-a-e6-development-baseline",
            "selected_method_id": "fixed",
            "observed": {"overall_recall_at_20_difference_pp": 0.1},
            "gates": {"overall_recall_improvement": False, "paired_recall_ci_lower_bound": False},
        }
        metrics = {"rows_missing_stage1_output": 0, "average_candidate_count_at_20": 20.0}
        baseline = {"protocol": "swebench-full-stage1-protocol-v1", "method_label": "e7-a-e6-development-baseline", "method_id": "fixed", "tickets": 1294, "predictions": 1294, "failures": 0, "metrics": metrics}
        candidate = {**baseline, "method_label": "e7-b-large-only-100", "method_id": "candidate"}
        validate_e7_selection(selection, baseline, candidate, {"rows": 1294, "paired_baseline": "E7-A"})

    def test_accepts_small_but_nonempty_repository_disjoint_holdout(self) -> None:
        validate_holdout_manifest({
            "protocol": "swebench-full-stage1-final-holdout-v2",
            "status": "prepared_not_evaluated",
            "selected_rows": 27,
            "leakage_checks": {"ticket_id_overlap_with_seen": 0, "repository_overlap_with_seen": 0, "unique_selected_ticket_ids": True},
        })


if __name__ == "__main__":
    unittest.main()
