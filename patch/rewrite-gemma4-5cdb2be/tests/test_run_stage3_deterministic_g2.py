from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
for import_path in (str(SRC_ROOT), str(PROJECT_ROOT)):
    if import_path not in sys.path:
        sys.path.insert(0, import_path)

from scripts.run_stage3_deterministic_g2 import _safe_name, _validate_config, select_variant


class Stage3DeterministicG2Tests(unittest.TestCase):
    def test_frozen_config_contains_b0_b1_and_quota_ablations(self) -> None:
        config = json.loads(
            (
                PROJECT_ROOT
                / "configs/fault_localization/stage3_deterministic_g2_v1.json"
            ).read_text(encoding="utf-8")
        )

        _validate_config(config)
        self.assertEqual(config["data_scope"], "development-only")
        self.assertEqual(config["candidate_k"], 30)
        self.assertEqual(
            [row["per_file_quota"] for row in config["variants"]],
            [0, 0, 4, 6, 8, 0, 0],
        )
        self.assertEqual(
            [row["selection_mode"] for row in config["variants"][-2:]],
            ["module-reserved", "coverage-aware-v1"],
        )

    def test_selection_requires_b1_to_strictly_improve_recall_at_30(self) -> None:
        tied = {
            "b0_tfidf": {"recall_at_30": 0.8, "recall_at_10": 0.5, "per_file_quota": 0},
            "b1_structured": {"recall_at_30": 0.8, "recall_at_10": 0.7, "per_file_quota": 0},
        }
        improved = {
            **tied,
            "b1_structured_q4": {
                "recall_at_30": 0.81,
                "recall_at_10": 0.6,
                "per_file_quota": 4,
            },
        }

        self.assertEqual(select_variant(tied), "b0_tfidf")
        self.assertEqual(select_variant(improved), "b1_structured_q4")

    def test_index_safe_name_matches_existing_cache_layout(self) -> None:
        self.assertEqual(_safe_name("astropy/astropy"), "astropy__astropy")


if __name__ == "__main__":
    unittest.main()
