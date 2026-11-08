from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from scripts.audit_symbol_gold import (  # noqa: E402
    create_stratified_sample,
    validate_audit_sample,
)


SMALL_MINIMUMS = {
    "nested_symbol": 2,
    "module_level": 1,
    "pure_addition": 1,
    "deletion": 1,
    "multi_file_ticket": 2,
}


class AuditSymbolGoldTests(unittest.TestCase):
    def test_fixed_seed_sample_is_order_independent_and_meets_strata(self) -> None:
        rows = _audit_rows()

        first = create_stratified_sample(
            rows,
            sample_size=6,
            seed=17,
            min_repositories=3,
            stratum_minimums=SMALL_MINIMUMS,
        )
        second = create_stratified_sample(
            list(reversed(rows)),
            sample_size=6,
            seed=17,
            min_repositories=3,
            stratum_minimums=SMALL_MINIMUMS,
        )

        self.assertEqual(
            [row["gold_id"] for row in first],
            [row["gold_id"] for row in second],
        )
        self.assertEqual([row["sample_index"] for row in first], [str(i) for i in range(1, 7)])
        self.assertTrue(all(row["audit_seed"] == "17" for row in first))
        self.assertTrue(all(row["review_status"] == "" for row in first))
        summary = validate_audit_sample(
            first,
            expected_sample_size=6,
            min_repositories=3,
            stratum_minimums=SMALL_MINIMUMS,
        )
        self.assertEqual(summary["sampling_gate_status"], "passed")
        self.assertEqual(summary["provenance_gate_status"], "passed")
        self.assertEqual(summary["exact_gate_status"], "pending_review")
        self.assertEqual(summary["overall_gate_status"], "pending_review")

    def test_exact_gate_only_passes_after_every_row_is_reviewed(self) -> None:
        sampled = create_stratified_sample(
            _audit_rows(),
            sample_size=6,
            seed=17,
            min_repositories=3,
            stratum_minimums=SMALL_MINIMUMS,
        )
        _approve(sampled[0])
        pending = validate_audit_sample(
            sampled,
            expected_sample_size=6,
            min_repositories=3,
            stratum_minimums=SMALL_MINIMUMS,
        )
        self.assertEqual(pending["reviewed_count"], 1)
        self.assertEqual(pending["exact_rate"], 1.0)
        self.assertEqual(pending["exact_gate_status"], "pending_review")

        for row in sampled[1:]:
            _approve(row)
        passed = validate_audit_sample(
            sampled,
            expected_sample_size=6,
            min_repositories=3,
            stratum_minimums=SMALL_MINIMUMS,
        )
        self.assertEqual(passed["exact_gate_status"], "passed")
        self.assertEqual(passed["overall_gate_status"], "passed")

    def test_inexact_or_incomplete_provenance_fails_the_gate(self) -> None:
        sampled = create_stratified_sample(
            _audit_rows(),
            sample_size=6,
            seed=17,
            min_repositories=3,
            stratum_minimums=SMALL_MINIMUMS,
        )
        for row in sampled:
            _approve(row)
        sampled[0]["patch_sha256"] = ""
        failed = validate_audit_sample(
            sampled,
            expected_sample_size=6,
            min_repositories=3,
            stratum_minimums=SMALL_MINIMUMS,
        )
        self.assertEqual(failed["provenance_gate_status"], "failed")
        self.assertEqual(failed["provenance_complete_count"], 5)
        self.assertEqual(failed["overall_gate_status"], "failed")

    def test_rejected_review_requires_a_note_and_consistent_boolean_labels(self) -> None:
        sampled = create_stratified_sample(
            _audit_rows(),
            sample_size=6,
            seed=17,
            min_repositories=3,
            stratum_minimums=SMALL_MINIMUMS,
        )
        for row in sampled:
            _approve(row)
        sampled[0].update(
            {
                "review_status": "rejected",
                "file_correct": "false",
                "review_note": "wrong file",
            }
        )
        sampled[1].update({"review_status": "rejected", "review_note": ""})

        summary = validate_audit_sample(
            sampled,
            expected_sample_size=6,
            min_repositories=3,
            stratum_minimums=SMALL_MINIMUMS,
        )

        self.assertEqual(summary["reviewed_count"], 5)
        self.assertEqual(summary["exact_count"], 4)
        self.assertEqual(summary["exact_gate_status"], "invalid_review")
        self.assertEqual(summary["review_error_count"], 1)

    def test_sampling_reports_category_shortage(self) -> None:
        rows = [row for row in _audit_rows() if row["mapping_method"] != "insertion_anchor_containment"]
        with self.assertRaisesRegex(ValueError, "pure_addition=0/1"):
            create_stratified_sample(
                rows,
                sample_size=6,
                seed=17,
                min_repositories=3,
                stratum_minimums=SMALL_MINIMUMS,
            )


def _audit_rows() -> list[dict[str, str]]:
    specs = [
        ("multi", "repo-0", "a.py", "Outer.run", "method", "modified", "modified_line_containment"),
        ("multi", "repo-0", "b.py", "<module>", "module", "modified", "module_level"),
        ("ticket-2", "repo-1", "c.py", "Outer.inner", "function", "modified", "insertion_anchor_containment"),
        ("ticket-3", "repo-2", "d.py", "removed", "function", "deleted", "deleted_line_containment"),
        ("ticket-4", "repo-1", "e.py", "plain", "function", "modified", "modified_line_containment"),
        ("ticket-5", "repo-2", "f.py", "<module>", "module", "modified", "module_level"),
        ("ticket-6", "repo-3", "g.py", "Class.call", "method", "modified", "modified_line_containment"),
        ("ticket-7", "repo-3", "h.py", "helper", "function", "modified", "modified_line_containment"),
    ]
    return [
        _row(index, *spec)
        for index, spec in enumerate(specs, start=1)
    ]


def _row(
    index: int,
    ticket_id: str,
    repo: str,
    file_path: str,
    qualified_name: str,
    symbol_kind: str,
    change_type: str,
    mapping_method: str,
) -> dict[str, str]:
    is_deleted = change_type == "deleted"
    is_addition = mapping_method == "insertion_anchor_containment"
    return {
        "gold_id": f"sha256:{index:064x}",
        "ticket_id": ticket_id,
        "repo": repo,
        "base_commit": "a" * 40,
        "file_path": file_path,
        "mapping_status": "mapped",
        "mapping_method": mapping_method,
        "mapping_confidence": "1.0",
        "exclusion_reason": "",
        "symbol_id": f"sha256:{index + 100:064x}",
        "qualified_name": qualified_name,
        "symbol_kind": symbol_kind,
        "start_line": "1",
        "start_column": "0",
        "end_line": "5",
        "end_column": "0",
        "change_type": change_type,
        "hunk_index": "0",
        "old_file_path": file_path,
        "new_file_path": "" if is_deleted else file_path,
        "old_start_line": "1",
        "old_line_count": "5",
        "new_start_line": "1",
        "new_line_count": "0" if is_deleted else "5",
        "matched_old_lines": "" if is_addition or mapping_method == "module_level" else "2",
        "context_old_lines": "1 3",
        "insertion_anchor_line": "2" if is_addition else "",
        "patch_sha256": "sha256:" + "f" * 64,
        "review_status": "",
        "file_correct": "",
        "qualified_name_correct": "",
        "symbol_kind_correct": "",
        "review_note": "",
    }


def _approve(row: dict[str, str]) -> None:
    row.update(
        {
            "review_status": "approved",
            "file_correct": "true",
            "qualified_name_correct": "true",
            "symbol_kind_correct": "true",
        }
    )


if __name__ == "__main__":
    unittest.main()
