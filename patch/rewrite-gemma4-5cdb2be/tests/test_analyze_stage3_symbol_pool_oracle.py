from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
for import_path in (str(SRC_ROOT), str(PROJECT_ROOT)):
    if import_path not in sys.path:
        sys.path.insert(0, import_path)

from scripts.analyze_stage3_symbol_pool_oracle import (
    aggregate_oracle_results,
    analyze_ticket,
)
from scripts.analyze_stage3_call_reachability import classify_reachability
from scripts.audit_stage3_no_edge_cases import classify_no_edge_case
from utils.symbol_evaluation import SymbolEvaluationItemV1


def symbol(path: str, name: str, kind: str) -> SymbolEvaluationItemV1:
    return SymbolEvaluationItemV1(
        file_path=path,
        qualified_name=name,
        symbol_kind=kind,
    )


class Stage3SymbolPoolOracleTests(unittest.TestCase):
    def test_no_edge_audit_prioritizes_scope_reconstruction_and_resolver_evidence(self) -> None:
        common = {
            "full_graph_peers": set(), "peers_in_candidate_pool": set(),
            "target_file": "src/a.py",
            "reconstructed_parse_ok": True, "reconstructed_target_found": True,
            "incoming_unresolved_calls": [], "symbol_kind": "method",
            "imported_calls": [], "dynamic_calls": [], "direct_calls": [],
        }
        self.assertEqual(
            classify_no_edge_case(**{
                **common, "full_graph_peers": {("src/b.py", "run")}
            })[0],
            "stage2_pool_scope",
        )
        self.assertEqual(
            classify_no_edge_case(**{
                **common,
                "full_graph_peers": {("src/b.py", "run")},
                "peers_in_candidate_pool": {("src/b.py", "run")},
            })[0],
            "import_resolution",
        )
        self.assertEqual(
            classify_no_edge_case(**{**common, "reconstructed_parse_ok": False})[0],
            "source_reconstruction",
        )
        self.assertEqual(
            classify_no_edge_case(**{**common, "incoming_unresolved_calls": ["src/a.py::obj.run"]})[0],
            "dynamic_dispatch",
        )

    def test_call_reachability_distinguishes_selection_anchor_and_graph_limits(self) -> None:
        prefix = {("src/a.py", "caller")}
        outside = {("src/a.py", "other")}
        self.assertEqual(
            classify_reachability(prefix_neighbors=prefix, pool_neighbors=prefix),
            "prefix_one_hop_reachable",
        )
        self.assertEqual(
            classify_reachability(prefix_neighbors=set(), pool_neighbors=outside),
            "connected_outside_prefix",
        )
        self.assertEqual(
            classify_reachability(prefix_neighbors=set(), pool_neighbors=set()),
            "no_resolved_incident_edge",
        )

    def test_ticket_classifies_file_policy_identity_and_ranking_losses(self) -> None:
        gold = [
            symbol("src/a.py", "first", "function"),
            symbol("src/a.py", "<module>", "module"),
            symbol("src/a.py", "Missing.run", "method"),
            symbol("src/a.py", "late", "function"),
            symbol("src/b.py", "outside", "function"),
        ]
        pool = [
            symbol("src/a.py", "first", "function"),
            symbol("src/a.py", "Missing.run", "function"),
            symbol("src/a.py", "late", "function"),
        ]
        top30 = [symbol("src/a.py", "first", "function")]

        result, details = analyze_ticket(
            ticket_id="T-1",
            repository="owner/repo",
            gold_symbols=gold,
            stage2_paths={"src/a.py"},
            pool=pool,
            top30=top30,
        )

        self.assertTrue(result["conditional_eligible"])
        self.assertEqual(result["stage2_file_oracle_recall"], 0.8)
        self.assertEqual(result["ast_pool_oracle_recall"], 0.4)
        self.assertEqual(result["top30_recall"], 0.2)
        self.assertEqual(
            {row["classification"] for row in details},
            {
                "top30_exact_hit",
                "module_excluded_by_candidate_policy",
                "pool_identity_mismatch",
                "top30_ranking_miss",
                "stage2_file_miss",
            },
        )

    def test_aggregate_uses_only_conditional_rows_for_oracle_macro(self) -> None:
        per_ticket = [
            {
                "conditional_eligible": True,
                "stage2_file_oracle_recall": 1.0,
                "ast_pool_oracle_recall": 0.5,
                "top30_recall": 0.25,
                "top30_recall_given_pool_reachable": 0.5,
                "pool_exact_count": 2,
            },
            {
                "conditional_eligible": False,
                "stage2_file_oracle_recall": 0.0,
                "ast_pool_oracle_recall": 0.0,
                "top30_recall": 0.0,
                "top30_recall_given_pool_reachable": 0.0,
                "pool_exact_count": 0,
            },
        ]
        details = [
            {
                "conditional_eligible": True,
                "classification": "top30_exact_hit",
                "pool_exact": True,
                "top30_exact": True,
            },
            {
                "conditional_eligible": True,
                "classification": "top30_ranking_miss",
                "pool_exact": True,
                "top30_exact": False,
            },
        ]

        report = aggregate_oracle_results(
            per_ticket,
            details,
            variant_id="b1_structured",
        )

        self.assertEqual(report["metrics"]["conditional_eligible_tickets"], 1)
        self.assertEqual(report["metrics"]["ast_pool_oracle_macro_recall"], 0.5)
        self.assertEqual(
            report["metrics"]["top30_micro_recall_given_pool_reachable"],
            0.5,
        )
        self.assertEqual(report["decision"]["primary_blocker"], "top30_ranking_miss")


if __name__ == "__main__":
    unittest.main()
