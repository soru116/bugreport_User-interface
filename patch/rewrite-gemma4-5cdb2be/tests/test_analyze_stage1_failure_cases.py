from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
for import_path in (str(PROJECT_ROOT), str(SRC_ROOT)):
    if import_path not in sys.path:
        sys.path.insert(0, import_path)

from scripts.analyze_stage1_failure_cases import (  # noqa: E402
    choose_failure_stage,
    classify_case,
    matching_gold_files,
    select_manual_review_sample,
    summarize_cases,
)
from scripts.review_stage1_failure_sample import (  # noqa: E402
    evidence_consistency_issues,
    review_case,
    summarize_reviews,
)


class Stage1FailureAnalysisTests(unittest.TestCase):
    def test_matching_gold_files_normalizes_relative_paths(self) -> None:
        recovered = matching_gold_files(
            ["src/pkg/parser.py", "src/pkg/lexer.py"],
            ["./src/pkg/parser.py", "src/other.py"],
        )
        self.assertEqual(recovered, ["src/pkg/parser.py"])

    def test_choose_failure_stage_uses_actionable_precedence(self) -> None:
        self.assertEqual(
            choose_failure_stage(["index_coverage_failure", "reranker_demotion"]),
            "index_coverage_failure",
        )
        self.assertEqual(
            choose_failure_stage(["initial_retrieval_miss", "reranker_demotion"]),
            "mixed_retrieval_and_rerank",
        )
        self.assertEqual(
            choose_failure_stage(["reranker_demotion"]),
            "reranker_demotion",
        )

    def test_classify_partial_case_preserves_failure_stage(self) -> None:
        case = classify_case(
            split_label="validation",
            split_role="method_selection",
            ticket={"ticket_id": "T-1", "repo": "org/repo", "title": "Parser fails"},
            gold={
                "ticket_id": "T-1",
                "repo": "org/repo",
                "fixed_files": ["src/api.py", "src/impl.py"],
            },
            prediction={
                "input_validation": {
                    "signals_present": {
                        "stack_trace": False,
                        "path_hint": False,
                        "identifier": True,
                    }
                }
            },
            outcome="partial_recall",
            gold_files=["src/api.py", "src/impl.py"],
            final_files=["src/api.py", "src/other.py"],
            cache_row={
                "index_available": True,
                "index_path": "index.json",
                "indexed_file_count": 3,
                "indexed_files": ["src/api.py", "src/impl.py", "src/other.py"],
                "tfidf_top50_computed": True,
                "tfidf_top50": [
                    {"rank": 1, "file_path": "src/api.py"},
                    {"rank": 2, "file_path": "src/impl.py"},
                ],
            },
        )
        self.assertEqual(case["primary_category"], "cross_file_partial_miss")
        self.assertEqual(case["failure_stage"], "reranker_demotion")
        self.assertEqual(case["missing_gold_files"], ["src/impl.py"])

    def test_classify_index_coverage_failure(self) -> None:
        case = classify_case(
            split_label="validation",
            split_role="method_selection",
            ticket={"ticket_id": "T-2", "repo": "org/repo"},
            gold={"ticket_id": "T-2", "fixed_files": ["tests/test_parser.py"]},
            prediction={"input_validation": {"signals_present": {}}},
            outcome="miss",
            gold_files=["tests/test_parser.py"],
            final_files=["src/parser.py"],
            cache_row={
                "index_available": True,
                "index_path": "index.json",
                "indexed_file_count": 1,
                "indexed_files": ["src/parser.py"],
                "tfidf_top50_computed": True,
                "tfidf_top50": [{"rank": 1, "file_path": "src/parser.py"}],
            },
        )
        self.assertEqual(case["primary_category"], "index_coverage_failure")
        self.assertTrue(case["manual_review_required"])

    def test_summary_and_review_sample_cover_groups(self) -> None:
        cases = [
            {
                "split": "validation",
                "outcome": "miss",
                "primary_category": "initial_retrieval_miss",
                "failure_stage": "initial_retrieval_miss",
                "repo": "org/a",
                "sparse_ticket_clues": False,
                "manual_review_required": False,
                "gold_file_evidence": [{"status": "initial_retrieval_miss"}],
            },
            {
                "split": "holdout",
                "outcome": "partial_recall",
                "primary_category": "cross_file_partial_miss",
                "failure_stage": "reranker_demotion",
                "repo": "org/b",
                "sparse_ticket_clues": True,
                "manual_review_required": True,
                "gold_file_evidence": [
                    {"status": "recovered_final_top20"},
                    {"status": "reranker_demotion"},
                ],
            },
        ]
        summary = summarize_cases(cases)
        self.assertEqual(summary["failure_cases"], 2)
        self.assertEqual(summary["miss_rows"], 1)
        self.assertEqual(summary["partial_recall_rows"], 1)
        self.assertEqual(len(select_manual_review_sample(cases, size=2)), 2)

    def test_review_case_confirms_consistent_reranker_evidence(self) -> None:
        case = {
            "split": "validation",
            "ticket_id": "T-3",
            "repo": "org/repo",
            "base_commit": "abc",
            "title": "Parser fails",
            "outcome": "miss",
            "failure_stage": "reranker_demotion",
            "primary_category": "reranker_demotion",
            "gold_files": ["src/parser.py"],
            "missing_gold_files": ["src/parser.py"],
            "gold_file_evidence": [
                {
                    "gold_file": "src/parser.py",
                    "status": "reranker_demotion",
                    "index_present": True,
                    "tfidf_top50_rank": 9,
                    "final_top20_rank": None,
                }
            ],
        }
        review = review_case(
            case,
            repo_cache_dir=Path("/missing"),
            manual_inspection_complete=True,
        )
        self.assertEqual(review["review_status"], "confirmed")
        self.assertEqual(review["manual_inspection"], "completed")
        self.assertIn("第9名", review["review_note"])

    def test_evidence_review_detects_inconsistent_status(self) -> None:
        issues = evidence_consistency_issues(
            {
                "gold_file_evidence": [
                    {
                        "gold_file": "src/parser.py",
                        "status": "initial_retrieval_miss",
                        "index_present": False,
                        "tfidf_top50_rank": None,
                        "final_top20_rank": None,
                    }
                ]
            }
        )
        self.assertEqual(len(issues), 1)

    def test_review_summary_counts_confirmed_rows(self) -> None:
        summary = summarize_reviews(
            [
                {
                    "review_status": "confirmed",
                    "reviewed_failure_stage": "initial_retrieval_miss",
                    "manual_inspection": "completed",
                    "index_absence_details": [],
                },
                {
                    "review_status": "confirmed",
                    "reviewed_failure_stage": "index_coverage_failure",
                    "manual_inspection": "completed",
                    "index_absence_details": [
                        {
                            "gold_file": "setup.cfg",
                            "reason": "existing_file_excluded_by_index_rules",
                        }
                    ],
                },
            ]
        )
        self.assertEqual(summary["reviewed_rows"], 2)
        self.assertEqual(summary["review_status_counts"], {"confirmed": 2})
        self.assertEqual(summary["manual_inspection_completed_rows"], 2)


if __name__ == "__main__":
    unittest.main()
