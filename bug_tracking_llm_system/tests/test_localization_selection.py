"""Focused tests for manually selecting a fault-localization candidate."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from integration.config import config_from_mapping  # noqa: E402
from integration.pipeline import IntegratedPipeline  # noqa: E402
from integration.store import TicketStore  # noqa: E402


class LocalizationSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config = config_from_mapping({"workspace_root": str(root), "store_dir": str(root / "store")})
        self.store = TicketStore(self.config.store_dir)
        self.candidates = [
            {"rank": 1, "file_path": "pkg/first.py", "symbol_qualified_name": "first", "start_line": 10},
            {"rank": 2, "file_path": "pkg/second.py", "symbol_qualified_name": "second", "start_line": 20},
        ]
        self.store.save({
            "key": "T-LOC", "ticket_id": "T-LOC", "status": "processed", "current_stage": "",
            "attention": ["補丁待人工審查"], "events": [], "commit_messages": {"current": {"status": "generated"}},
            "stages": {
                "localization": {"stage": "localization", "status": "done", "output": {"ranked_symbols": self.candidates}},
                "patch": {"stage": "patch", "status": "done", "output": {"patch_status": "generated", "patch": "old"}},
            },
        })
        self.store.write_artifact("T-LOC", "bug_location_raw.json", {"stage3_ranked_symbols": self.candidates})
        unused = object()
        self.pipeline = IntegratedPipeline(
            self.config, store=self.store, extractor=unused, duplicate_detector=unused,
            priority_predictor=unused, assignee_recommender=unused, localizer=unused,
            patcher=unused, commit_messenger=unused,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_selected_candidate_becomes_first_patch_target_and_clears_old_patch(self) -> None:
        record = self.pipeline.select_localization_candidate("T-LOC", 1)
        output = record["stages"]["localization"]["output"]
        raw = json.loads(self.store.artifact_path("T-LOC", "bug_location_raw.json").read_text(encoding="utf-8"))

        self.assertEqual(output["selected_symbol_index"], 1)
        self.assertEqual(output["selected_symbol"]["symbol_qualified_name"], "second")
        self.assertEqual(raw["stage3_ranked_symbols"][0]["symbol_qualified_name"], "second")
        self.assertEqual(raw["manual_selection"]["original_rank"], 2)
        self.assertNotIn("patch", record["stages"])
        self.assertNotIn("current", record["commit_messages"])
        self.assertNotIn("補丁待人工審查", record["attention"])

    def test_unknown_candidate_is_rejected_without_changing_the_record(self) -> None:
        with self.assertRaisesRegex(ValueError, "找不到"):
            self.pipeline.select_localization_candidate("T-LOC", 9)
        self.assertIn("patch", self.pipeline.get("T-LOC")["stages"])


if __name__ == "__main__":
    unittest.main()
