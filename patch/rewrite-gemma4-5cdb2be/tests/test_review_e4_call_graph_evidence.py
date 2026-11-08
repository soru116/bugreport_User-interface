from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.review_e4_call_graph_evidence import (  # noqa: E402
    apply_decisions,
    build_call_graph_evidence_cases,
    sample_cases,
    summarize,
)


class E4CallGraphEvidenceReviewTests(unittest.TestCase):
    def prediction(self) -> dict[str, object]:
        evidence = {
            "reference_file": "pkg/service.py",
            "reference_rank": 1,
            "caller_symbol": "process",
            "target_file": "pkg/helpers.py",
            "target_symbol": "normalize",
            "call_name": "normalize",
            "line": 12,
            "resolution_type": "imported_function",
        }
        return {
            "ticket_id": "T-1",
            "stage1_candidate_files": [
                {
                    "rank": 3,
                    "file_path": "pkg/helpers.py",
                    "scoring_signals": {
                        "call_graph_bonus": 0.072,
                        "call_graph_evidence": [evidence, dict(evidence)],
                    },
                }
            ],
        }

    def test_builds_unique_edge_and_marks_gold_target(self) -> None:
        cases = build_call_graph_evidence_cases(
            [{"ticket_id": "T-1", "title": "normalize fails"}],
            [
                {
                    "ticket_id": "T-1",
                    "repo": "org/repo",
                    "fixed_files": ["./pkg/helpers.py"],
                }
            ],
            [self.prediction()],
        )
        self.assertEqual(len(cases), 1)
        self.assertTrue(cases[0]["target_is_gold"])
        self.assertEqual(cases[0]["automatic_hint"], "correct_mapping")
        self.assertEqual(cases[0]["candidate_rank"], 3)

    def test_manual_decision_requires_reason_and_gold_consistency(self) -> None:
        case = {
            "evidence_id": "e4cg-test",
            "target_is_gold": False,
            "classification": "unreviewed",
            "classification_zh": "待人工確認",
        }
        reviewed = apply_decisions(
            [case],
            {
                "e4cg-test": {
                    "classification": "irrelevant_mapping",
                    "reason": "The call is unrelated to the Ticket.",
                }
            },
            require_all=True,
        )
        self.assertEqual(reviewed[0]["classification"], "irrelevant_mapping")
        with self.assertRaisesRegex(ValueError, "Gold-overlap"):
            apply_decisions(
                [case],
                {
                    "e4cg-test": {
                        "classification": "correct_mapping",
                        "reason": "Incorrect manual label.",
                    }
                },
                require_all=True,
            )

    def test_sampling_and_summary_are_reproducible(self) -> None:
        cases = [
            {
                "evidence_id": f"e4cg-{position}",
                "ticket_id": f"T-{position}",
                "repo": "org/repo",
                "candidate_file": f"pkg/{position}.py",
                "resolution_type": "imported_function",
                "reference_rank": 1,
                "target_is_gold": False,
                "classification": "irrelevant_mapping",
            }
            for position in range(5)
        ]
        self.assertEqual(
            sample_cases(cases, size=3, seed=42),
            sample_cases(cases, size=3, seed=42),
        )
        summary = summarize(cases, evidence_pool=cases, seed=42)
        self.assertEqual(summary["classification_counts"]["irrelevant_mapping"], 5)
        self.assertEqual(summary["pool_candidate_file_count"], 5)


if __name__ == "__main__":
    unittest.main()
