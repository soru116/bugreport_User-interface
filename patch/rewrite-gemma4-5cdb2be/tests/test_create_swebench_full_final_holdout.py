from __future__ import annotations

import unittest

from scripts.create_swebench_full_final_holdout import (
    gold_record,
    normalize_value,
    proportional_quotas,
    select_repository_disjoint_rows,
    ticket_record,
)


class CreateSWEbenchFullFinalHoldoutTest(unittest.TestCase):
    def test_normalizes_parquet_array_values(self) -> None:
        import pandas as pd

        self.assertEqual(normalize_value(pd.array(["a", "b"])), ["a", "b"])

    def test_records_preserve_external_dataset_identity(self) -> None:
        row = {
            "instance_id": "live-1",
            "repo": "new/repo",
            "base_commit": "abc",
            "problem_statement": "External issue",
            "patch": "diff --git a/src/a.py b/src/a.py\n+++ b/src/a.py\n",
        }
        ticket = ticket_record(row, dataset_name="SWE-bench-Live/SWE-bench-Live", source_split="verified")
        gold = gold_record(row, dataset_name="SWE-bench-Live/SWE-bench-Live", source_split="verified")
        self.assertEqual(ticket["source_dataset"], "SWE-bench-Live/SWE-bench-Live")
        self.assertEqual(ticket["source_split"], "verified")
        self.assertEqual(gold["fixed_files"], ["src/a.py"])

    def test_selection_is_deterministic_and_repository_disjoint(self) -> None:
        rows = [
            {"instance_id": f"new-a-{index}", "repo": "new/a"}
            for index in range(6)
        ] + [
            {"instance_id": f"new-b-{index}", "repo": "new/b"}
            for index in range(4)
        ] + [
            {"instance_id": "seen-ticket", "repo": "new/a"},
            {"instance_id": "old-repo-ticket", "repo": "old/repo"},
        ]
        first, eligible = select_repository_disjoint_rows(
            rows,
            seen_ids={"seen-ticket"},
            seen_repositories={"old/repo"},
            target=5,
            seed="fixed",
        )
        second, _ = select_repository_disjoint_rows(
            rows,
            seen_ids={"seen-ticket"},
            seen_repositories={"old/repo"},
            target=5,
            seed="fixed",
        )
        self.assertEqual(first, second)
        self.assertEqual(eligible, 10)
        self.assertEqual(len(first), 5)
        self.assertNotIn("old/repo", {row["repo"] for row in first})
        self.assertNotIn("seen-ticket", {row["instance_id"] for row in first})

    def test_proportional_quotas_preserve_target(self) -> None:
        quotas = proportional_quotas({"a": 6, "b": 4}, 5)
        self.assertEqual(quotas, {"a": 3, "b": 2})

    def test_rejects_insufficient_repository_disjoint_rows(self) -> None:
        with self.assertRaisesRegex(ValueError, "only 1 are eligible"):
            select_repository_disjoint_rows(
                [
                    {"instance_id": "a", "repo": "new/repo"},
                    {"instance_id": "b", "repo": "old/repo"},
                ],
                seen_ids=set(),
                seen_repositories={"old/repo"},
                target=2,
                seed="fixed",
            )


if __name__ == "__main__":
    unittest.main()
