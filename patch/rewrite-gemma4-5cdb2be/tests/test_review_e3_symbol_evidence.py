from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.review_e3_symbol_evidence import (  # noqa: E402
    build_evidence_cases,
    classify_evidence_case,
    sample_evidence_cases,
    summarize_cases,
)


class E3SymbolEvidenceReviewTests(unittest.TestCase):
    def prediction(self, evidence: list[dict[str, object]]) -> dict[str, object]:
        return {
            "ticket_id": "T-1",
            "stage1_candidate_files": [
                {"rank": 1, "file_path": "src/right.py"},
                {"rank": 2, "file_path": "src/extra.py"},
            ],
            "stage1_diagnostics": {
                "symbol_expansion": {
                    "ticket_program_names": ["parse"],
                    "evidence": evidence,
                }
            },
        }

    def test_classifies_only_gold_evidence_as_correct(self) -> None:
        case = classify_evidence_case(
            ticket={"ticket_id": "T-1", "title": "Parser fails"},
            gold={"ticket_id": "T-1", "fixed_files": ["./src/right.py"]},
            prediction=self.prediction(
                [
                    {
                        "ticket_symbol": "parse",
                        "matched_symbol": "Parser.parse",
                        "file_path": "src/right.py",
                        "match_type": "exact",
                    }
                ]
            ),
            evidence=[
                {
                    "ticket_symbol": "parse",
                    "matched_symbol": "Parser.parse",
                    "file_path": "src/right.py",
                    "match_type": "exact",
                }
            ],
            ticket_program_names=["parse"],
        )
        self.assertEqual(case["classification"], "correct_mapping")
        self.assertEqual(case["matching_gold_files"], ["src/right.py"])

    def test_classifies_mixed_gold_and_extra_as_ambiguous(self) -> None:
        evidence = [
            {
                "ticket_symbol": "parse",
                "matched_symbol": "parse",
                "file_path": "src/right.py",
                "match_type": "exact",
            },
            {
                "ticket_symbol": "self.parse",
                "matched_symbol": "Other.parse",
                "file_path": "src/extra.py",
                "match_type": "leaf",
            },
        ]
        case = classify_evidence_case(
            ticket={"ticket_id": "T-1"},
            gold={"ticket_id": "T-1", "fixed_files": ["src/right.py"]},
            prediction=self.prediction(evidence),
            evidence=evidence,
            ticket_program_names=["parse", "self.parse"],
        )
        self.assertEqual(case["classification"], "overly_ambiguous")
        self.assertEqual(case["extra_evidence_files"], ["src/extra.py"])
        self.assertEqual(case["leaf_match_count"], 1)

    def test_classifies_no_gold_overlap_as_incorrect(self) -> None:
        evidence = [
            {
                "ticket_symbol": "parse",
                "matched_symbol": "parse",
                "file_path": "src/extra.py",
                "match_type": "exact",
            }
        ]
        case = classify_evidence_case(
            ticket={"ticket_id": "T-1"},
            gold={"ticket_id": "T-1", "fixed_files": ["src/right.py"]},
            prediction=self.prediction(evidence),
            evidence=evidence,
            ticket_program_names=["parse"],
        )
        self.assertEqual(case["classification"], "incorrect_mapping")
        self.assertEqual(case["matching_gold_files"], [])

    def test_build_skips_predictions_without_evidence(self) -> None:
        tickets = [{"ticket_id": "T-1"}, {"ticket_id": "T-2"}]
        gold = [
            {"ticket_id": "T-1", "fixed_files": ["src/right.py"]},
            {"ticket_id": "T-2", "fixed_files": ["src/other.py"]},
        ]
        with_evidence = self.prediction(
            [
                {
                    "ticket_symbol": "parse",
                    "matched_symbol": "parse",
                    "file_path": "src/right.py",
                    "match_type": "exact",
                }
            ]
        )
        without_evidence = {
            "ticket_id": "T-2",
            "stage1_diagnostics": {"symbol_expansion": {"evidence": []}},
        }
        cases = build_evidence_cases(tickets, gold, [with_evidence, without_evidence])
        self.assertEqual(len(cases), 1)

    def test_seeded_sample_and_summary_are_reproducible(self) -> None:
        cases = [
            {
                "classification": "correct_mapping",
                "exact_match_count": 1,
                "leaf_match_count": 0,
                "matching_gold_files": ["src/right.py"],
                "repo": "org/repo",
            },
            {
                "classification": "overly_ambiguous",
                "exact_match_count": 1,
                "leaf_match_count": 2,
                "matching_gold_files": ["src/right.py"],
                "repo": "org/repo",
            },
            {
                "classification": "incorrect_mapping",
                "exact_match_count": 0,
                "leaf_match_count": 1,
                "matching_gold_files": [],
                "repo": "org/repo",
            },
        ]
        first = sample_evidence_cases(cases, 2, 42)
        second = sample_evidence_cases(cases, 2, 42)
        self.assertEqual(first, second)
        summary = summarize_cases(cases, pool_size=3, seed=42)
        self.assertEqual(summary["classification_counts"]["correct_mapping"], 1)
        self.assertEqual(summary["leaf_evidence_rows"], 3)
        self.assertEqual(summary["repository_counts"], {"org/repo": 3})


if __name__ == "__main__":
    unittest.main()
