from __future__ import annotations

import unittest

from scripts.analyze_e5_repository_size_groups import (
    analyze_repository_size_groups,
    balanced_group_sizes,
    build_repository_groups,
    extract_code_index_file_count,
    summarize_predictions,
)


class AnalyzeE5RepositorySizeGroupsTest(unittest.TestCase):
    def test_balanced_group_sizes(self) -> None:
        self.assertEqual(balanced_group_sizes(12, 3), [4, 4, 4])
        self.assertEqual(balanced_group_sizes(10, 3), [4, 3, 3])

    def test_repository_groups_use_median_file_count(self) -> None:
        rows = build_repository_groups(
            {
                "repo/a": [10, 12, 100],
                "repo/b": [20],
                "repo/c": [30],
                "repo/d": [40],
                "repo/e": [50],
                "repo/f": [60],
            }
        )
        self.assertEqual([row["repository"] for row in rows], [
            "repo/a",
            "repo/b",
            "repo/c",
            "repo/d",
            "repo/e",
            "repo/f",
        ])
        self.assertEqual(
            [row["size_group"] for row in rows],
            ["small", "small", "medium", "medium", "large", "large"],
        )
        self.assertEqual(rows[0]["median_indexed_files"], 12.0)

    def test_extract_code_index_file_count(self) -> None:
        prediction = {
            "ticket_id": "repo__a-1",
            "stage1_diagnostics": {"call_graph": {"files": 123}},
        }
        self.assertEqual(extract_code_index_file_count(prediction), 123)

    def test_stage1_metrics(self) -> None:
        summary = summarize_predictions(
            [
                {
                    "ticket_id": "a",
                    "gold_files": ["src/a.py"],
                    "candidate_files": ["src/a.py", "src/x.py"],
                },
                {
                    "ticket_id": "b",
                    "gold_files": ["src/b.py", "src/c.py"],
                    "candidate_files": ["src/x.py", "src/b.py"],
                },
                {
                    "ticket_id": "c",
                    "gold_files": ["src/d.py"],
                    "candidate_files": ["src/x.py"],
                },
            ]
        )
        self.assertAlmostEqual(summary["hit_at_20"], 2 / 3)
        self.assertAlmostEqual(summary["recall_at_20"], 0.5)
        self.assertAlmostEqual(summary["top_1_accuracy"], 1 / 3)
        self.assertAlmostEqual(summary["mrr_at_20"], 0.5)
        self.assertEqual(summary["full_recall_rows"], 1)
        self.assertEqual(summary["partial_recall_rows"], 1)
        self.assertEqual(summary["miss_rows"], 1)

    def test_full_analysis_does_not_use_gold_for_group_assignment(self) -> None:
        gold = []
        baseline = []
        diagnostics = []
        for index, repository in enumerate(("repo/a", "repo/b", "repo/c"), start=1):
            ticket_id = f"repo__{index}-1"
            gold.append(
                {
                    "ticket_id": ticket_id,
                    "repo": repository,
                    "fixed_files": [f"src/{index}.py"],
                }
            )
            baseline.append(
                {
                    "ticket_id": ticket_id,
                    "repo": repository,
                    "stage1_candidate_files": [
                        {"rank": 1, "file_path": f"src/{index}.py"}
                    ],
                }
            )
            diagnostics.append(
                {
                    "ticket_id": ticket_id,
                    "repo": repository,
                    "stage1_diagnostics": {
                        "call_graph": {"files": index * 100}
                    },
                }
            )
        result = analyze_repository_size_groups(gold, baseline, diagnostics)
        self.assertEqual(
            [row["size_group"] for row in result["repositories"]],
            ["small", "medium", "large"],
        )
        self.assertEqual(result["overall_baseline"]["recall_at_20"], 1.0)
        self.assertEqual(
            result["groups"][0]["macro_repository_metrics"]["recall_at_20"],
            1.0,
        )
        self.assertEqual(len(result["repository_baselines"]), 3)
        self.assertFalse(result["data_policy"]["group_assignment_uses_gold"])


if __name__ == "__main__":
    unittest.main()
