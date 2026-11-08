from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from scripts.evaluate_fault_localization import evaluate_records  # noqa: E402
from utils.symbol_evaluation import (  # noqa: E402
    SymbolEvaluationItemV1,
    evaluate_ranked_symbols,
    match_symbol,
)


ID_A = "sha256:" + "a" * 64
ID_B = "sha256:" + "b" * 64


def symbol(
    file_path: str,
    qualified_name: str,
    symbol_kind: str,
    symbol_id: str = "",
) -> SymbolEvaluationItemV1:
    return SymbolEvaluationItemV1(
        file_path=file_path,
        qualified_name=qualified_name,
        symbol_kind=symbol_kind,
        symbol_id=symbol_id,
    )


def gold_row(
    ticket_id: str,
    file_path: str,
    qualified_name: str,
    symbol_kind: str,
    symbol_id: str = "",
) -> dict[str, str]:
    return {
        "ticket_id": ticket_id,
        "mapping_status": "mapped",
        "file_path": file_path,
        "qualified_name": qualified_name,
        "symbol_kind": symbol_kind,
        "symbol_id": symbol_id,
    }


class SymbolEvaluationContractTests(unittest.TestCase):
    def test_exact_is_file_qualified_and_relaxed_is_diagnostic_only(self) -> None:
        gold = symbol("src/a.py", "Service.run", "method")
        wrong_file = symbol("src/b.py", "Service.run", "method")

        result = match_symbol(wrong_file, gold)

        self.assertFalse(result.exact)
        self.assertTrue(result.relaxed)
        self.assertEqual(result.reason, "relaxed_qualified_name")

    def test_trusted_symbol_ids_are_authoritative(self) -> None:
        gold = symbol("src/a.py", "Service.run", "method", ID_A)
        same_tuple_wrong_id = symbol("src/a.py", "Service.run", "method", ID_B)
        same_id_different_tuple = symbol("src/moved.py", "Renamed.run", "method", ID_A)

        self.assertFalse(match_symbol(same_tuple_wrong_id, gold).exact)
        self.assertTrue(match_symbol(same_id_different_tuple, gold).exact)
        self.assertEqual(
            match_symbol(same_id_different_tuple, gold).reason,
            "exact_symbol_id",
        )

    def test_module_contract_rejects_non_module_kind(self) -> None:
        with self.assertRaisesRegex(ValueError, "<module>"):
            symbol("src/settings.py", "<module>", "function")

    def test_multi_gold_recall_is_independent_from_hit(self) -> None:
        gold = [
            symbol("src/service.py", "Service.first", "method"),
            symbol("src/service.py", "Service.second", "method"),
        ]
        ranked = [
            symbol("src/service.py", "Service.first", "method"),
            symbol("src/other.py", "other", "function"),
            symbol("src/service.py", "Service.second", "method"),
        ]

        result = evaluate_ranked_symbols(ranked, gold)

        self.assertEqual(result["exact"]["hit_at"]["1"], 1)
        self.assertEqual(result["exact"]["recall_at"]["1"], 0.5)
        self.assertEqual(result["exact"]["recall_at"]["3"], 1.0)

    def test_evaluator_groups_gold_rows_and_keeps_missing_prediction_in_denominator(self) -> None:
        gold = [
            gold_row("A", "src/service.py", "Service.first", "method"),
            gold_row("A", "src/service.py", "Service.second", "method"),
            gold_row("B", "src/settings.py", "<module>", "module"),
        ]
        predictions = [
            {
                "ticket_id": "A",
                "stage2_localized_files": [{"file_path": "src/service.py"}],
                "stage3_ranked_symbols": [
                    {
                        "file_path": "src/service.py",
                        "symbol_qualified_name": "Service.first",
                        "symbol_kind": "method",
                    }
                ],
            }
        ]

        metrics = evaluate_records(gold, predictions)
        formal = metrics["formal_symbol_evaluation"]

        self.assertEqual(formal["eligible_rows"], 2)
        self.assertEqual(formal["missing_prediction_rows"], 1)
        self.assertEqual(formal["exact"]["hit_at_1"], 0.5)
        self.assertEqual(formal["exact"]["recall_at_1"], 0.25)
        self.assertEqual(formal["conditional_eligible_rows"], 1)
        self.assertEqual(formal["conditional_exact"]["hit_at_1"], 1.0)
        self.assertEqual(metrics["symbol_top_1_accuracy"], 0.5)

    def test_wrong_file_counts_only_as_relaxed_diagnostic(self) -> None:
        metrics = evaluate_records(
            [gold_row("A", "src/a.py", "Service.run", "method")],
            [
                {
                    "ticket_id": "A",
                    "stage2_localized_files": [{"file_path": "src/b.py"}],
                    "stage3_ranked_symbols": [
                        {
                            "file_path": "src/b.py",
                            "symbol_qualified_name": "Service.run",
                            "symbol_kind": "method",
                        }
                    ],
                }
            ],
        )["formal_symbol_evaluation"]

        self.assertEqual(metrics["exact"]["hit_at_1"], 0.0)
        self.assertEqual(metrics["relaxed_diagnostic"]["hit_at_1"], 1.0)
        self.assertEqual(metrics["conditional_eligible_rows"], 0)

    def test_candidate_metrics_and_fallback_coverage_use_explicit_denominators(self) -> None:
        candidate_symbols = [
            {
                "file_path": "src/service.py",
                "symbol_qualified_name": "Service.first",
                "symbol_kind": "method",
            },
            *[
                {
                    "file_path": "src/other.py",
                    "symbol_qualified_name": f"helper_{index}",
                    "symbol_kind": "function",
                }
                for index in range(2, 11)
            ],
            {
                "file_path": "src/service.py",
                "symbol_qualified_name": "Service.second",
                "symbol_kind": "method",
            },
        ]
        metrics = evaluate_records(
            [
                gold_row("A", "src/service.py", "Service.first", "method"),
                gold_row("A", "src/service.py", "Service.second", "method"),
            ],
            [
                {
                    "ticket_id": "A",
                    "stage2_localized_files": [{"file_path": "src/service.py"}],
                    "stage3_candidate_symbols": candidate_symbols,
                    "stage3_retrieval_symbols": candidate_symbols[:5],
                    "stage3_ranked_symbols": candidate_symbols[:5],
                    "stage3_diagnostics": {
                        "requested": True,
                        "eligible": True,
                        "llm_attempted": True,
                        "llm_valid": False,
                        "fallback_used": True,
                        "fallback_reason": "invalid_or_incomplete_output",
                        "timed_out": False,
                    },
                }
            ],
        )["formal_symbol_evaluation"]

        self.assertEqual(metrics["candidate_exact"]["eligible_rows"], 1)
        self.assertEqual(metrics["candidate_exact"]["hit_at_10"], 1.0)
        self.assertEqual(metrics["candidate_exact"]["recall_at_10"], 0.5)
        self.assertEqual(metrics["candidate_exact"]["recall_at_30"], 1.0)
        self.assertEqual(metrics["coverage"]["candidate_output_coverage"], 1.0)
        self.assertEqual(metrics["coverage"]["llm_valid_coverage"], 0.0)
        self.assertEqual(metrics["coverage"]["fallback_rate"], 1.0)
        self.assertEqual(
            metrics["coverage"]["fallback_reasons"],
            {"invalid_or_incomplete_output": 1},
        )

    def test_paired_outcomes_and_bootstrap_compare_retrieval_with_final(self) -> None:
        gold = [
            gold_row(ticket_id, f"src/{ticket_id}.py", "run", "function")
            for ticket_id in ("A", "B", "C", "D")
        ]

        def candidate(ticket_id: str, correct: bool) -> dict[str, str]:
            return {
                "file_path": f"src/{ticket_id}.py",
                "symbol_qualified_name": "run" if correct else "other",
                "symbol_kind": "function",
            }

        predictions = []
        for ticket_id, baseline_hit, final_hit in (
            ("A", False, True),
            ("B", True, True),
            ("C", True, False),
            ("D", False, False),
        ):
            predictions.append(
                {
                    "ticket_id": ticket_id,
                    "stage2_localized_files": [{"file_path": f"src/{ticket_id}.py"}],
                    "stage3_retrieval_symbols": [candidate(ticket_id, baseline_hit)],
                    "stage3_ranked_symbols": [candidate(ticket_id, final_hit)],
                }
            )

        paired = evaluate_records(gold, predictions)["formal_symbol_evaluation"][
            "paired_exact"
        ]

        self.assertEqual(paired["rows"], 4)
        self.assertEqual(paired["delta"]["hit_at_1"], 0.0)
        outcomes = paired["hit_outcomes"]["hit_at_1"]
        self.assertEqual(outcomes["improved"]["ticket_ids"], ["A"])
        self.assertEqual(outcomes["unchanged"]["ticket_ids"], ["B"])
        self.assertEqual(outcomes["worsened"]["ticket_ids"], ["C"])
        self.assertEqual(outcomes["both_miss"]["ticket_ids"], ["D"])
        interval = paired["bootstrap_95_ci"]["hit_at_1"]
        self.assertLessEqual(interval["lower"], 0.0)
        self.assertGreaterEqual(interval["upper"], 0.0)


if __name__ == "__main__":
    unittest.main()
