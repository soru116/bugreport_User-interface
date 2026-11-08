from __future__ import annotations

import unittest

from scripts.analyze_e5_tfidf_top50_by_size import (
    analyze_top50_by_size,
    cache_row_is_compatible,
    summarize_top50,
)


class AnalyzeE5TfidfTop50BySizeTest(unittest.TestCase):
    def test_cache_compatibility_checks_repository_commit_and_method(self) -> None:
        ticket = {"ticket_id": "repo__a-1", "repo": "repo/a", "base_commit": "abc"}
        row = {
            "ticket_id": "repo__a-1",
            "repo": "repo/a",
            "base_commit": "abc",
            "index_available": True,
            "tfidf_top50_computed": True,
            "tfidf_top50": [],
        }
        self.assertTrue(cache_row_is_compatible(row, ticket))
        self.assertFalse(cache_row_is_compatible({**row, "base_commit": "def"}, ticket))
        self.assertFalse(cache_row_is_compatible({**row, "tfidf_top50_computed": False}, ticket))

    def test_top50_metrics_distinguish_index_and_retrieval_coverage(self) -> None:
        summary = summarize_top50(
            [
                {
                    "ticket_id": "a",
                    "gold_files": ["src/a.py"],
                    "indexed_files": ["src/a.py"],
                    "tfidf_top50_files": ["src/a.py"],
                },
                {
                    "ticket_id": "b",
                    "gold_files": ["src/b.py", "src/c.py"],
                    "indexed_files": ["src/b.py", "src/c.py"],
                    "tfidf_top50_files": ["src/x.py", "src/b.py"],
                },
                {
                    "ticket_id": "c",
                    "gold_files": ["src/d.py"],
                    "indexed_files": ["src/x.py"],
                    "tfidf_top50_files": ["src/x.py"],
                },
            ]
        )
        self.assertAlmostEqual(summary["index_hit"], 2 / 3)
        self.assertAlmostEqual(summary["index_recall"], 2 / 3)
        self.assertAlmostEqual(summary["hit_at_50"], 2 / 3)
        self.assertAlmostEqual(summary["recall_at_50"], 0.5)
        self.assertAlmostEqual(summary["retrieval_recall_gap"], 1 / 6)
        self.assertAlmostEqual(summary["mrr_at_50"], 0.5)
        self.assertEqual(summary["full_recall_rows"], 1)
        self.assertEqual(summary["partial_recall_rows"], 1)
        self.assertEqual(summary["miss_rows"], 1)

    def test_analysis_preserves_fixed_groups(self) -> None:
        tickets = []
        gold = []
        cache = {}
        repositories = []
        for index, (group, repository) in enumerate(
            (("small", "repo/a"), ("medium", "repo/b"), ("large", "repo/c")),
            start=1,
        ):
            ticket_id = f"repo__{index}-1"
            file_path = f"src/{index}.py"
            tickets.append(
                {"ticket_id": ticket_id, "repo": repository, "base_commit": str(index)}
            )
            gold.append(
                {"ticket_id": ticket_id, "repo": repository, "fixed_files": [file_path]}
            )
            cache[ticket_id] = {
                "ticket_id": ticket_id,
                "repo": repository,
                "base_commit": str(index),
                "indexed_files": [file_path],
                "tfidf_top50": [{"rank": 1, "file_path": file_path}],
            }
            repositories.append({"repository": repository, "size_group": group})
        result = analyze_top50_by_size(
            tickets,
            gold,
            cache,
            {"repositories": repositories},
        )
        self.assertEqual(result["overall"]["recall_at_50"], 1.0)
        self.assertEqual(
            [row["size_group"] for row in result["groups"]],
            ["small", "medium", "large"],
        )
        self.assertFalse(result["data_policy"]["holdout_used"])


if __name__ == "__main__":
    unittest.main()
