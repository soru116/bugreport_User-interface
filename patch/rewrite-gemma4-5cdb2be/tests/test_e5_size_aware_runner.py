from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.run_swebench_full_stage1_experiment import METHOD_ARGUMENTS
from scripts.run_swebench_lite_fault_localization import (
    BatchRunConfig,
    _load_repository_size_groups,
    _method_id,
    _method_spec,
    _semantic_candidate_k_for_repository,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GROUPS_PATH = PROJECT_ROOT / "configs/fault_localization/e5_repository_size_groups.json"


def make_config(*, medium_k: int, large_k: int) -> BatchRunConfig:
    return BatchRunConfig(
        tickets_path=Path("tickets.jsonl"),
        gold_path=Path("gold.jsonl"),
        repo_cache_dir=Path("repos"),
        snapshot_cache_dir=Path("snapshots"),
        index_cache_dir=Path("indexes"),
        predictions_output=Path("predictions.jsonl"),
        metrics_output=Path("metrics.json"),
        failures_output=Path("failures.jsonl"),
        manifest_output=Path("manifest.json"),
        embedding_backend="tfidf-sbert-rerank",
        semantic_candidate_k=50,
        semantic_candidate_policy="repository-size",
        repository_size_groups_path=GROUPS_PATH,
        medium_semantic_candidate_k=medium_k,
        large_semantic_candidate_k=large_k,
        candidate_file_k=20,
        domain_path_routing=False,
        file_aggregation_mode="basic",
        symbol_expansion_mode="symbol-definitions-api",
    )


class E5SizeAwareRunnerTest(unittest.TestCase):
    def test_frozen_group_config_has_four_repositories_per_size(self) -> None:
        groups = _load_repository_size_groups(make_config(medium_k=75, large_k=75))
        self.assertEqual(len(groups), 12)
        self.assertEqual(list(groups.values()).count("small"), 4)
        self.assertEqual(list(groups.values()).count("medium"), 4)
        self.assertEqual(list(groups.values()).count("large"), 4)

    def test_e5_b_selects_50_for_small_and_75_for_other_groups(self) -> None:
        config = make_config(medium_k=75, large_k=75)
        groups = _load_repository_size_groups(config)
        self.assertEqual(
            _semantic_candidate_k_for_repository(config, "pallets/flask", groups),
            50,
        )
        self.assertEqual(
            _semantic_candidate_k_for_repository(config, "sphinx-doc/sphinx", groups),
            75,
        )
        self.assertEqual(
            _semantic_candidate_k_for_repository(config, "django/django", groups),
            75,
        )

    def test_e5_c_selects_100_for_medium_and_large(self) -> None:
        config = make_config(medium_k=100, large_k=100)
        groups = _load_repository_size_groups(config)
        self.assertEqual(
            _semantic_candidate_k_for_repository(config, "pylint-dev/pylint", groups),
            100,
        )
        self.assertEqual(
            _semantic_candidate_k_for_repository(config, "sympy/sympy", groups),
            100,
        )

    def test_method_manifest_is_portable_and_e5_b_c_ids_differ(self) -> None:
        e5_b = make_config(medium_k=75, large_k=75)
        e5_c = make_config(medium_k=100, large_k=100)
        e5_b_groups = _load_repository_size_groups(e5_b)
        e5_c_groups = _load_repository_size_groups(e5_c)
        e5_b_method = _method_spec(e5_b, repository_size_groups=e5_b_groups)
        e5_c_method = _method_spec(e5_c, repository_size_groups=e5_c_groups)
        serialized = json.dumps(e5_b_method, sort_keys=True)
        self.assertNotIn(str(PROJECT_ROOT), serialized)
        self.assertEqual(e5_b_method["semantic_candidate_k"], 50)
        self.assertEqual(
            e5_b_method["semantic_candidate_k_by_repository_size"],
            {"small": 50, "medium": 75, "large": 75},
        )
        self.assertNotEqual(_method_id(e5_b_method), _method_id(e5_c_method))

    def test_duplicate_repository_group_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            groups_path = Path(temporary) / "groups.json"
            groups_path.write_text(
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
            config = make_config(medium_k=75, large_k=75)
            config.repository_size_groups_path = groups_path
            with self.assertRaisesRegex(ValueError, "multiple size groups"):
                _load_repository_size_groups(config)

    def test_full_runner_exposes_both_size_aware_methods(self) -> None:
        e5_b = METHOD_ARGUMENTS["e5-b-size-aware-75"]
        e5_c = METHOD_ARGUMENTS["e5-c-size-aware-100"]
        self.assertIn("repository-size", e5_b)
        self.assertIn("75", e5_b)
        self.assertIn("100", e5_c)

    def test_e7_large_only_method_keeps_medium_at_50_and_large_at_100(self) -> None:
        e7_a = METHOD_ARGUMENTS["e7-a-e6-development-baseline"]
        e7_b = METHOD_ARGUMENTS["e7-b-large-only-100"]
        self.assertIn("outgoing-top3", e7_a)
        self.assertIn("repository-size", e7_b)
        medium_index = e7_b.index("--medium-semantic-candidate-k") + 1
        large_index = e7_b.index("--large-semantic-candidate-k") + 1
        self.assertEqual(e7_b[medium_index], "50")
        self.assertEqual(e7_b[large_index], "100")

    def test_e8_a_keeps_e7_a_ranking_settings(self) -> None:
        self.assertEqual(
            METHOD_ARGUMENTS["e8-a-source-extension-contract"],
            METHOD_ARGUMENTS["e7-a-e6-development-baseline"],
        )


if __name__ == "__main__":
    unittest.main()
