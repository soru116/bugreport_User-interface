from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.analyze_e7_development_priorities import (
    _guard_development_only_inputs,
    analyze_e7_priorities,
    choose_priority,
    top_level_module_count,
    validate_experiment_config,
)


class AnalyzeE7DevelopmentPrioritiesTest(unittest.TestCase):
    def test_priority_prefers_large_multimodule_retrieval_miss(self) -> None:
        self.assertEqual(
            choose_priority(retrieval_failure=True, large_multimodule=True),
            "P0_large_multimodule_retrieval",
        )
        self.assertEqual(
            choose_priority(retrieval_failure=True, large_multimodule=False),
            "P1_other_retrieval",
        )

    def test_top_level_module_count_normalizes_paths(self) -> None:
        self.assertEqual(
            top_level_module_count(["src/a.py", "tests/test_a.py", "README.py"]),
            3,
        )

    def test_analysis_requires_development_and_builds_e7_matrix(self) -> None:
        failure_analysis = {
            "sources": {"development": {"role": "development_only"}},
            "cases": [
                {
                    "split": "development",
                    "ticket_id": "t-large",
                    "repo": "repo/large",
                    "indexed_file_count": 1000,
                    "outcome": "miss",
                    "failure_stage": "initial_retrieval_miss",
                    "failure_stage_zh": "初步檢索遺漏",
                    "gold_file_count": 1,
                    "recovered_gold_count": 0,
                    "missing_gold_files": ["pkg/fix.py"],
                },
                {
                    "split": "development",
                    "ticket_id": "t-small",
                    "repo": "repo/small",
                    "indexed_file_count": 10,
                    "outcome": "miss",
                    "failure_stage": "reranker_demotion",
                    "failure_stage_zh": "重排遺漏",
                    "gold_file_count": 1,
                    "recovered_gold_count": 0,
                    "missing_gold_files": ["small.py"],
                },
            ],
        }
        caches = [
            {
                "ticket_id": "t-large",
                "indexed_files": ["pkg/a.py", "tests/a.py", "docs/conf.py"],
            },
            {"ticket_id": "t-small", "indexed_files": ["small.py"]},
        ]
        result = analyze_e7_priorities(
            failure_analysis,
            caches,
            {"repo/large": "large", "repo/small": "small"},
        )
        self.assertEqual(result["summary"]["failure_cases"], 2)
        self.assertEqual(result["summary"]["retrieval_failure_cases"], 1)
        self.assertEqual(
            result["priority_counts"]["P0_large_multimodule_retrieval"], 1
        )
        self.assertEqual(
            result["experiment_matrix"]["E7-B"]["runner_method"],
            "e7-b-large-only-100",
        )

    def test_guard_rejects_holdout_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "final_holdout.json"
            with self.assertRaisesRegex(ValueError, "refuses Holdout"):
                _guard_development_only_inputs([path])

    def test_experiment_config_must_match_frozen_e7_methods(self) -> None:
        config = {
            "version": "stage1-e7-development-v1",
            "data_policy": {"rows": 1294},
            "methods": {
                "E7-A": "e7-a-e6-development-baseline",
                "E7-B": "e7-b-large-only-100",
            },
        }
        validate_experiment_config(config)
        config["methods"]["E7-B"] = "unexpected"
        with self.assertRaisesRegex(ValueError, "method labels"):
            validate_experiment_config(config)


if __name__ == "__main__":
    unittest.main()
