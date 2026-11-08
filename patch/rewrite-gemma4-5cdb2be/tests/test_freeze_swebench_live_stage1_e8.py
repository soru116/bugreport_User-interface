from __future__ import annotations

import unittest

from scripts.freeze_swebench_live_stage1_e8 import validate_holdout_manifest, validate_prior_e7_record


class FreezeSWEbenchLiveStage1E8Test(unittest.TestCase):
    def test_accepts_expected_prior_contract_failure(self) -> None:
        validate_prior_e7_record({
            "status": "holdout_evaluated_once_output_contract_failed",
            "method_label": "e7-a-e6-development-baseline",
            "output_contract": {"exact_top20_rows": 20, "short_rows": 7},
            "method": {"semantic_candidate_k": 50, "call_graph_mode": "outgoing-top3"},
        })

    def test_accepts_repository_disjoint_live_holdout(self) -> None:
        validate_holdout_manifest({
            "protocol": "swebench-live-stage1-e8-holdout-v1",
            "status": "prepared_not_evaluated",
            "dataset": "SWE-bench-Live/SWE-bench-Live",
            "selected_rows": 100,
            "leakage_checks": {
                "ticket_id_overlap_with_seen": 0,
                "repository_overlap_with_seen": 0,
                "unique_selected_ticket_ids": True,
            },
        })


if __name__ == "__main__":
    unittest.main()
