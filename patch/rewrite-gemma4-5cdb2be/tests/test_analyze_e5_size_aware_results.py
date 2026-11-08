from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.analyze_e5_size_aware_results import (
    analyze_size_aware_results,
    load_repository_size_groups,
)


def prediction(ticket_id: str, repo: str, candidates: list[str], runtime: float) -> dict:
    return {
        "ticket_id": ticket_id,
        "repo": repo,
        "stage1_candidate_files": [
            {"file_path": file_path} for file_path in candidates
        ],
        "benchmark_run": {"runtime_seconds": runtime},
    }


class AnalyzeE5SizeAwareResultsTest(unittest.TestCase):
    def test_reports_group_metrics_paired_difference_and_runtime(self) -> None:
        gold = [
            {"ticket_id": "t-small", "repo": "repo/small", "fixed_files": ["a.py"]},
            {"ticket_id": "t-medium", "repo": "repo/medium", "fixed_files": ["b.py"]},
            {"ticket_id": "t-large", "repo": "repo/large", "fixed_files": ["c.py"]},
        ]
        baseline = [
            prediction("t-small", "repo/small", ["a.py"], 1.0),
            prediction("t-medium", "repo/medium", ["x.py"], 2.0),
            prediction("t-large", "repo/large", ["x.py"], 3.0),
        ]
        candidate = [
            prediction("t-small", "repo/small", ["a.py"], 1.5),
            prediction("t-medium", "repo/medium", ["b.py"], 2.5),
            prediction("t-large", "repo/large", ["c.py"], 3.5),
        ]
        result = analyze_size_aware_results(
            gold,
            {"baseline": baseline, "candidate": candidate},
            {"repo/small": "small", "repo/medium": "medium", "repo/large": "large"},
            bootstrap_samples=100,
            seed=7,
        )
        self.assertEqual(result["methods"]["baseline"]["overall"]["hit_at_20"], 1 / 3)
        self.assertEqual(result["methods"]["candidate"]["groups"]["large"]["hit_at_20"], 1.0)
        self.assertEqual(
            result["methods"]["candidate"]["groups"]["medium"]["paired_vs_baseline"]["hit_at_20"]["mean_difference"],
            1.0,
        )
        self.assertEqual(result["methods"]["candidate"]["runtime"]["sum_seconds"], 7.5)

    def test_duplicate_repository_group_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "groups.json"
            path.write_text(
                json.dumps(
                    {
                        "groups": {
                            "small": ["repo/a"],
                            "medium": ["repo/a"],
                            "large": ["repo/c"],
                        }
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "multiple size groups"):
                load_repository_size_groups(path)


if __name__ == "__main__":
    unittest.main()
