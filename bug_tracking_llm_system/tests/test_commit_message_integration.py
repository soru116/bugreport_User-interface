"""Focused tests for generated patch -> Code Llama commit message integration."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from integration.commit_message import TeamCommitMessageGenerator, build_prompt  # noqa: E402
from integration.common import stage_result  # noqa: E402
from integration.config import config_from_mapping  # noqa: E402
from integration.pipeline import IntegratedPipeline  # noqa: E402
from integration.store import TicketStore  # noqa: E402


PATCH = """--- a/pkg/names.py
+++ b/pkg/names.py
@@ -1,2 +1,4 @@
 def clean(value):
+    if value is None:
+        return ""
     return value.strip()
"""

ALTERNATE_PATCH = """--- a/pkg/names.py
+++ b/pkg/names.py
@@ -1,2 +1,3 @@
 def clean(value):
+    value = value or ""
     return value.strip()
"""


class _PatchGenerator:
    def generate(self, record, project, raw, *, output_dir):
        return stage_result(
            stage="patch", status="done", module="fake-patch", implementation="test",
            output={
                "patch_status": "generated", "patch": PATCH, "modified_files": ["pkg/names.py"],
                "patch_sha256": hashlib.sha256(PATCH.encode("utf-8")).hexdigest(),
                "selected_candidate_id": 1,
                "candidates": [
                    {"candidate_id": 1, "status": "generated", "patch": PATCH,
                     "modified_files": ["pkg/names.py"]},
                    {"candidate_id": 2, "status": "generated", "patch": ALTERNATE_PATCH,
                     "modified_files": ["pkg/names.py"]},
                ],
            },
        )

    def official_verdict(self, patch):
        return {"official_resolved": True, "run_id": "alternate"} if patch == ALTERNATE_PATCH else None


class CommitMessageIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        repo = root / "repo"
        repo.mkdir()
        self.config = config_from_mapping({
            "workspace_root": str(root),
            "store_dir": str(root / "store"),
            "projects": {"demo": {"repo_path": str(repo)}},
            "default_project": "demo",
            "commit_message": {"model": "codellama:13b-instruct"},
        })
        self.store = TicketStore(self.config.store_dir)
        self.record = {
            "key": "T-1", "ticket_id": "T-1", "project": "demo", "status": "processed",
            "current_stage": "", "attention": [], "events": [],
            "raw": {"title": "SECRET title", "patch": "SECRET GOLD PATCH", "hints_text": "SECRET HINT"},
            "structured_ticket": {"ticket_id": "T-1", "title": "clean crashes on None",
                                  "description": "Return an empty string for None."},
            "stages": {"localization": {"status": "done"}},
        }
        self.store.save(self.record)
        self.store.write_artifact("T-1", "bug_location_raw.json", {})

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _pipeline(self, response: str, *, raises: bool = False) -> IntegratedPipeline:
        def call(prompt: str) -> str:
            if raises:
                raise RuntimeError("unexpected transport failure")
            return response

        messenger = TeamCommitMessageGenerator(self.config, llm_call=call)
        unused = object()
        return IntegratedPipeline(
            self.config, store=self.store, extractor=unused, duplicate_detector=unused,
            priority_predictor=unused, assignee_recommender=unused, localizer=unused,
            patcher=_PatchGenerator(), commit_messenger=messenger,
        )

    def test_generated_patch_gets_one_line_commit_message(self) -> None:
        pipeline = self._pipeline(json.dumps({"type": "fix", "subject": "return an empty string for None input"}))
        stage = pipeline.generate_patch("T-1")["stages"]["patch"]
        self.assertEqual(stage["status"], "done")
        commit = stage["output"]["commit_message"]
        self.assertEqual(commit["status"], "generated")
        self.assertEqual(commit["commit_message"], "fix(names): return an empty string for None input")
        self.assertNotIn("\n", commit["commit_message"])

    def test_patch_target_stops_before_commit_message(self) -> None:
        pipeline = self._pipeline(json.dumps({"type": "fix", "subject": "should not be called"}))
        stage = pipeline.generate_patch("T-1", automatic=True, generate_commit_message=False)["stages"]["patch"]
        self.assertEqual(stage["status"], "done")
        self.assertEqual(stage["output"]["patch_status"], "generated")
        self.assertNotIn("commit_message", stage["output"])

    def test_selecting_another_candidate_updates_current_patch_and_commit_source(self) -> None:
        seen_patches = []

        class CapturingMessenger:
            model = "fake"

            def generate(self, record, patch, *, official=None):
                seen_patches.append(patch)
                return {"status": "generated", "commit_message": "fix(names): normalize empty values", "model": "fake"}

        unused = object()
        pipeline = IntegratedPipeline(
            self.config, store=self.store, extractor=unused, duplicate_detector=unused,
            priority_predictor=unused, assignee_recommender=unused, localizer=unused,
            patcher=_PatchGenerator(), commit_messenger=CapturingMessenger(),
        )
        pipeline.generate_patch("T-1", generate_commit_message=False)

        selected = pipeline.select_patch_candidate("T-1", 2)
        output = selected["stages"]["patch"]["output"]
        self.assertEqual(output["selected_candidate_id"], 2)
        self.assertEqual(output["patch"], ALTERNATE_PATCH)
        self.assertEqual(output["patch_sha256"], hashlib.sha256(ALTERNATE_PATCH.encode("utf-8")).hexdigest())
        self.assertEqual(output["official"]["run_id"], "alternate")
        self.assertNotIn("commit_message", output)

        drafted = pipeline.draft_commit_message("T-1", "current")
        self.assertEqual(seen_patches, [ALTERNATE_PATCH])
        self.assertEqual(
            drafted["stages"]["patch"]["output"]["commit_message"]["commit_message"],
            "fix(names): normalize empty values",
        )

    def test_overlong_model_subject_is_shortened_instead_of_blocking_automatic_flow(self) -> None:
        generator = TeamCommitMessageGenerator(
            self.config,
            llm_call=lambda _prompt: json.dumps({"type": "fix", "subject": "handle " + "verbose words " * 12}),
        )
        result = generator.generate(
            {"ticket_id": "PATCH-2", "raw": {"title": "Verbose draft"}},
            "--- a/pkg/names.py\n+++ b/pkg/names.py\n@@ -1 +1 @@\n-old\n+new\n",
        )
        self.assertEqual(result["status"], "generated")
        subject = result["commit_message"].split(": ", 1)[1]
        self.assertLessEqual(len(subject), 80)
        self.assertEqual(result["normalization"], "subject_truncated_to_80_chars")
        prompt = build_prompt(self.record, PATCH)
        self.assertIn("ADDED:     if value is None:", prompt)
        self.assertNotIn("SECRET GOLD PATCH", prompt)
        self.assertNotIn("SECRET HINT", prompt)

    def test_commit_message_failure_keeps_generated_patch(self) -> None:
        pipeline = self._pipeline("", raises=True)
        stage = pipeline.generate_patch("T-1")["stages"]["patch"]
        self.assertEqual(stage["status"], "done")
        self.assertEqual(stage["output"]["patch_status"], "generated")
        self.assertEqual(stage["output"]["commit_message"]["status"], "failed")
        self.assertIn("unexpected transport failure", stage["output"]["commit_message"]["error"])
        self.assertTrue(any("提交訊息生成失敗" in warning for warning in stage["warnings"]))


if __name__ == "__main__":
    unittest.main()
