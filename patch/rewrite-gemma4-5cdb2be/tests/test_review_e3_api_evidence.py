from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.review_e3_api_evidence import (  # noqa: E402
    api_evidence_from_prediction,
    apply_decisions,
    build_api_evidence_cases,
    sample_cases,
    summarize,
)


class E3ApiEvidenceReviewTests(unittest.TestCase):
    def prediction(self) -> dict[str, object]:
        evidence = {
            "ticket_symbol": "public_api",
            "api_symbol": "public_api",
            "source_file": "pkg/api.py",
            "implementation_symbol": "run",
            "file_path": "pkg/core.py",
            "relation_type": "wrapper",
        }
        return {
            "ticket_id": "T-1",
            "repo": "org/repo",
            "stage1_candidate_files": [
                {
                    "rank": 2,
                    "file_path": "pkg/core.py",
                    "scoring_signals": {
                        "api_implementation_evidence": [evidence, dict(evidence)]
                    },
                }
            ],
            "stage1_diagnostics": {
                "symbol_expansion": {"ticket_program_names": ["public_api"]}
            },
        }

    def test_deduplicates_evidence_and_marks_gold_overlap(self) -> None:
        prediction = self.prediction()
        evidence = api_evidence_from_prediction(prediction)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["candidate_rank"], 2)
        cases = build_api_evidence_cases(
            [{"ticket_id": "T-1", "title": "public_api fails"}],
            [{"ticket_id": "T-1", "repo": "org/repo", "fixed_files": ["./pkg/core.py"]}],
            [prediction],
        )
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["automatic_hint"], "correct_mapping")
        self.assertEqual(cases[0]["matching_implementation_gold_files"], ["pkg/core.py"])

    def test_applies_complete_manual_decisions(self) -> None:
        case = {
            "ticket_id": "T-1",
            "classification": "unreviewed",
            "classification_zh": "待人工確認",
        }
        reviewed = apply_decisions(
            [case],
            {
                "T-1": {
                    "classification": "relationship_correct_not_modified",
                    "reason": "API關係成立，但Gold修改入口檔案。",
                }
            },
            require_all=True,
        )
        self.assertEqual(
            reviewed[0]["classification"],
            "relationship_correct_not_modified",
        )
        with self.assertRaisesRegex(ValueError, "Missing manual decision"):
            apply_decisions([case], {}, require_all=True)

    def test_seed_and_summary_are_reproducible(self) -> None:
        cases = [
            {
                "ticket_id": f"T-{position}",
                "repo": "org/repo",
                "classification": "correct_mapping",
                "matching_implementation_gold_files": ["pkg/core.py"],
                "matching_source_gold_files": [],
                "evidence_count": 1,
            }
            for position in range(5)
        ]
        self.assertEqual(
            sample_cases(cases, size=3, seed=42),
            sample_cases(cases, size=3, seed=42),
        )
        summary = summarize(cases, pool_size=5, seed=42)
        self.assertEqual(summary["classification_counts"]["correct_mapping"], 5)
        self.assertEqual(summary["implementation_gold_overlap_cases"], 5)


if __name__ == "__main__":
    unittest.main()
