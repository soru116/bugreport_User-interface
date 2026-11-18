from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.sparse import csr_matrix


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
BUILD_FEATURES = WORKSPACE_ROOT / "bug-priority-drone" / "scripts" / "build_features.py"


def load_build_features():
    spec = importlib.util.spec_from_file_location("priority_build_features_target_isolation", BUILD_FEATURES)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class PriorityTargetIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.features = load_build_features()

    def rows(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "id": "H1",
                    "creation_time": pd.Timestamp("2026-01-01T00:00:00"),
                    "creator": "unknown",
                    "product": "unknown",
                    "component": "api",
                    "severity": "normal",
                    "priority_num": 1,
                    "__is_target": False,
                },
                {
                    "id": "T1",
                    "creation_time": pd.Timestamp("2026-01-02T00:00:00"),
                    "creator": "unknown",
                    "product": "unknown",
                    "component": "api",
                    "severity": "normal",
                    "priority_num": 3,
                    "__is_target": True,
                },
                {
                    "id": "T2",
                    "creation_time": pd.Timestamp("2026-01-03T00:00:00"),
                    "creator": "unknown",
                    "product": "unknown",
                    "component": "api",
                    "severity": "normal",
                    "priority_num": 3,
                    "__is_target": True,
                },
            ]
        )

    def test_targets_do_not_enter_author_or_temporal_history(self) -> None:
        author = self.features.add_author_features(self.rows(), exclude_targets_from_history=True)
        target = author.loc[author["id"] == "T2"].iloc[0]
        self.assertEqual(target["author_past_count"], 1)
        self.assertEqual(target["author_past_mean_priority"], 1.0)

        temporal = self.features.add_temporal_features(self.rows(), exclude_targets_from_history=True)
        target = temporal.loc[temporal["id"] == "T2"].iloc[0]
        self.assertEqual(target["past_7d_count"], 1)
        self.assertEqual(target["product_past_30d_mean_priority"], 1.0)

    def test_targets_do_not_enter_related_report_history(self) -> None:
        rows = self.rows()
        values = np.ones((len(rows), 1), dtype=float)
        related = self.features.build_related_feature_rows(
            rows,
            "bm25",
            rows["product"].to_numpy(),
            rows["component"].to_numpy(),
            rows["severity"].to_numpy(),
            None,
            csr_matrix(values),
            0.9,
            0.2,
            2.0,
            0.0,
            0.0,
            True,
        )
        self.assertEqual(related[2]["related_top1_priority"], 1.0)
        self.assertEqual(related[2]["related_top5_avg_priority"], 1.0)


if __name__ == "__main__":
    unittest.main()
