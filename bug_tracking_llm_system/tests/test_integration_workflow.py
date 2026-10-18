"""Tests for the integration layer (src/integration).

These tests call the real sub-project code (ToJson extractor helpers, DUP package, PRI scripts,
AssigneeTriager, Stage-3 BugLocalizer).  Only the Code Llama HTTP call is replaced by a stub,
because Ollama is not available in CI; the PRI test trains a tiny model with PRI's own scripts.
"""

from __future__ import annotations

import csv
import json
import random
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
for path in (str(SRC_ROOT), str(PROJECT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from integration.config import config_from_mapping  # noqa: E402
from integration.duplicate import TeamDuplicateDetector  # noqa: E402
from integration.extraction import ToJsonExtractor  # noqa: E402
from integration.importers import parse_import_payload  # noqa: E402
from integration.pipeline import IntegratedPipeline  # noqa: E402
from integration.priority import TeamPriorityPredictor  # noqa: E402

WORKSPACE_ROOT = PROJECT_ROOT.parent
SMOKE_REPO = PROJECT_ROOT / "tests" / "fixtures" / "smoke_repo"
HISTORY = [
    {"ticket_id": "H-1", "title": "Login crash when token missing", "description": "Missing auth token causes AttributeError during login validation.", "component": "authentication", "priority": "P2", "assignee": "auth-team@example.com"},
    {"ticket_id": "H-2", "title": "Password reset email not sent", "description": "SMTP timeout prevents password reset emails.", "component": "email", "priority": "P3", "assignee": "mail-team@example.com"},
    {"ticket_id": "H-3", "title": "Session expires immediately after login", "description": "start_session drops the token and users are logged out.", "component": "authentication", "priority": "P3", "assignee": "auth-team@example.com"},
]


def _has(*modules: str) -> bool:
    import importlib.util

    return all(importlib.util.find_spec(name) is not None for name in modules)


def fake_code_llama(prompt: str) -> str:
    """Stands in for Ollama only; parsing/normalization below is ToJson's real code."""

    assert "[INST]" in prompt and "Bug report:" in prompt
    return json.dumps(
        {
            "title": "model title",
            "description": "model description",
            "component": "authentication",
            "severity": "major",
            "bug_type": "crash",
            "error_message": "AttributeError: 'NoneType' object has no attribute 'strip'",
            "steps_to_reproduce": ["Open login", "Submit without token"],
        }
    )


class _Encoder:
    def encode(self, texts, normalize_embeddings=True, **_):
        import numpy as np

        vocab = ["token", "login", "crash", "email", "session", "smtp", "auth"]
        rows = []
        for text in texts:
            lowered = str(text).lower()
            vector = np.array([lowered.count(word) for word in vocab], dtype=float) + 1e-6
            rows.append(vector / np.linalg.norm(vector))
        return np.vstack(rows)


class _FakeSbert:
    """Duck-types SbertDuplicateDetector (title_model/content_model/config) for the SBERT branch."""

    def __init__(self) -> None:
        from types import SimpleNamespace

        self.title_model = _Encoder()
        self.content_model = _Encoder()
        self.config = SimpleNamespace(batch_size=8)

    def _check_fitted(self) -> None:
        return None


@unittest.skipUnless(_has("numpy", "sklearn", "requests"), "integration tests need numpy, scikit-learn, requests")
class IntegrationWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.history_path = self.root / "history.jsonl"
        self.history_path.write_text("".join(json.dumps(row) + "\n" for row in HISTORY), encoding="utf-8")
        self.config = config_from_mapping(
            {
                "workspace_root": str(WORKSPACE_ROOT),
                "store_dir": str(self.root / "store"),
                "historical_tickets_paths": [str(self.history_path)],
                "assignee": {"assignee_dataset_path": str(self.history_path)},
                "projects": {"smoke": {"repo_path": str(SMOKE_REPO)}},
                "default_project": "smoke",
            }
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def raw_ticket(self) -> dict:
        return {
            "ticket_id": "T-1",
            "title": "Login page crashes when token is missing",
            "description": "When I login without a token the app crashes.",
            "logs": "AttributeError: 'NoneType' object has no attribute 'strip' at src/auth/validator.py:9",
            "reporter": "qa@example.com",
        }

    def test_tojson_extraction_uses_tojson_code_and_keeps_explicit_fields(self) -> None:
        extractor = ToJsonExtractor(self.config, llm_call=fake_code_llama)
        structured, result = extractor.extract(self.raw_ticket())
        self.assertEqual(result["status"], "done")
        self.assertIn("ToJson/scripts/To_Json/extract_to_json.py", result["implementation"])
        self.assertEqual(structured["component"], "authentication")  # filled by the model
        self.assertEqual(structured["title"], "Login page crashes when token is missing")  # explicit wins
        self.assertEqual(structured["steps_to_reproduce"], ["Open login", "Submit without token"])
        self.assertEqual(structured["reporter"], "qa@example.com")

    def test_extraction_reports_fallback_when_ollama_unreachable(self) -> None:
        self.config.extraction.ollama_url = "http://127.0.0.1:9/api/generate"
        self.config.extraction.timeout_seconds = 1
        structured, result = ToJsonExtractor(self.config).extract(self.raw_ticket())
        self.assertEqual(result["status"], "fallback")
        self.assertTrue(result["fallback_reason"])
        self.assertEqual(structured["ticket_id"], "T-1")

    def test_imported_predicted_json_is_not_re_extracted(self) -> None:
        def never(_prompt: str) -> str:
            raise AssertionError("LLM must not be called for ToJson predictions")

        raw = {"id": "P-1", "predicted_json": {"ticket_id": "P-1", "title": "x", "component": "email"}}
        structured, result = ToJsonExtractor(self.config, llm_call=never).extract(raw)
        self.assertEqual(result["status"], "done")
        self.assertEqual(structured["component"], "email")

    def test_duplicate_tfidf_fallback_ranks_with_dup_package(self) -> None:
        result = TeamDuplicateDetector(self.config).detect({"ticket_id": "Q", "title": "Login crash when token is missing", "description": "token missing crash", "component": "authentication"})
        self.assertEqual(result["status"], "fallback")
        output = result["output"]
        self.assertEqual(output["top_k_candidates"][0]["ticket_id"], "H-1")
        self.assertTrue(output["needs_review"])  # semi-automatic by default
        self.assertIn("bug-duplicate-detection", result["implementation"])

    def test_duplicate_sbert_branch_applies_stable_rerank(self) -> None:
        detector = TeamDuplicateDetector(self.config, sbert_detector=_FakeSbert())
        result = detector.detect({"ticket_id": "Q", "title": "Login crash token", "description": "auth token crash", "component": "authentication"})
        self.assertEqual(result["status"], "done")
        self.assertTrue(any(step.startswith("stable rerank") for step in result["details"]["pipeline"]))
        self.assertEqual(result["output"]["top_k_candidates"][0]["ticket_id"], "H-1")

    def test_auto_duplicate_threshold_closes_ticket(self) -> None:
        self.config.duplicate.auto_duplicate_threshold = 0.5
        result = TeamDuplicateDetector(self.config).detect({"ticket_id": "Q", "title": "Login crash when token missing", "description": "Missing auth token causes AttributeError during login validation."})
        self.assertTrue(result["output"]["is_duplicate"])
        self.assertEqual(result["output"]["duplicate_of"], "H-1")

    def test_priority_falls_back_without_pri_artifacts(self) -> None:
        self.config.priority.model_path = self.root / "missing.joblib"
        result = TeamPriorityPredictor(self.config).predict({"ticket_id": "T", "title": "crash"})
        self.assertEqual(result["status"], "fallback")
        self.assertIn("missing.joblib", result["fallback_reason"])
        self.assertIn(result["output"]["predicted_priority"], {"P1", "P2", "P3", "P4", "P5"})

    def test_full_workflow_with_duplicate_gate_and_assignee_confirmation(self) -> None:
        self.config.priority.model_path = self.root / "missing.joblib"
        pipeline = IntegratedPipeline(self.config, extractor=ToJsonExtractor(self.config, llm_call=fake_code_llama))
        [record] = pipeline.import_raw([self.raw_ticket()], source="test")
        record = pipeline.run_until_review(record["key"])
        self.assertEqual(record["status"], "awaiting_duplicate_review")
        with self.assertRaises(ValueError):
            pipeline.run_after_duplicate(record["key"])
        pipeline.decide_duplicate(record["key"], "not_duplicate")
        record = pipeline.run_after_duplicate(record["key"])
        self.assertEqual(record["status"], "processed")
        stages = record["stages"]
        self.assertEqual(stages["extraction"]["status"], "done")
        self.assertEqual(stages["priority"]["status"], "fallback")
        self.assertEqual(stages["assignee"]["status"], "done")
        self.assertEqual(stages["assignee"]["output"]["ranked_candidates"][0], "auth-team@example.com")
        self.assertEqual(stages["localization"]["status"], "done")
        symbols = stages["localization"]["output"]["ranked_symbols"]
        self.assertTrue(any(item["file_path"] == "src/auth/validator.py" for item in symbols))
        record = pipeline.confirm_assignee(record["key"], "auth-team@example.com")
        self.assertEqual(record["decisions"]["assignee"]["assignee"], "auth-team@example.com")
        history = pipeline.store.history_rows()
        self.assertTrue(any(row["ticket_id"] == "T-1" and row.get("assignee") == "auth-team@example.com" for row in history))

    def test_confirmed_duplicate_stops_the_flow(self) -> None:
        pipeline = IntegratedPipeline(self.config, extractor=ToJsonExtractor(self.config, llm_call=fake_code_llama))
        [record] = pipeline.import_raw([self.raw_ticket()], source="test")
        pipeline.run_until_review(record["key"])
        record = pipeline.decide_duplicate(record["key"], "duplicate", duplicate_of="H-1")
        self.assertEqual(record["status"], "duplicate")
        self.assertEqual(record["stages"]["localization"]["status"], "pending")

    def test_review_marks_and_history_lookup_for_comparison(self) -> None:
        pipeline = IntegratedPipeline(self.config, extractor=ToJsonExtractor(self.config, llm_call=fake_code_llama))
        [record] = pipeline.import_raw([self.raw_ticket()], source="test")
        pipeline.run_until_review(record["key"])
        self.assertEqual(pipeline.find_history_ticket("H-1")["assignee"], "auth-team@example.com")
        self.assertIsNone(pipeline.find_history_ticket("missing"))
        record = pipeline.mark_candidate(record["key"], "H-1", "possible", note="same error message")
        self.assertEqual(record["review_marks"]["H-1"]["mark"], "possible")
        self.assertEqual(record["review_marks"]["H-1"]["note"], "same error message")
        record = pipeline.mark_candidate(record["key"], "H-1", "")
        self.assertNotIn("H-1", record["review_marks"])
        with self.assertRaises(ValueError):
            pipeline.mark_candidate(record["key"], "H-1", "maybe")

    def test_pinning_survives_a_rerun(self) -> None:
        pipeline = IntegratedPipeline(self.config, extractor=ToJsonExtractor(self.config, llm_call=fake_code_llama))
        [record] = pipeline.import_raw([self.raw_ticket()], source="test")
        record = pipeline.set_pinned(record["key"], True)
        self.assertTrue(record["pinned"])
        self.assertTrue(record["pinned_at"])
        record = pipeline.run_until_review(record["key"])
        self.assertTrue(record["pinned"], "re-running the pipeline must not drop the pin")
        record = pipeline.set_pinned(record["key"], False)
        self.assertFalse(record["pinned"])
        self.assertEqual(record["pinned_at"], "")

    def test_import_formats(self) -> None:
        csv_text = "ticket_id,title,steps_to_reproduce\nA-1,Broken,step one|step two\n"
        self.assertEqual(parse_import_payload(csv_text, filename="t.csv")[0]["steps_to_reproduce"], ["step one", "step two"])
        jsonl = '{"id": "1", "bug_report": "x crashes", "meta": {"title": "x"}}\n'
        self.assertEqual(parse_import_payload(jsonl, filename="unlabeled.jsonl")[0]["id"], "1")
        self.assertEqual(len(parse_import_payload('{"tickets": [{"title": "a"}, {"title": "b"}]}', filename="a.json")), 2)


@unittest.skipUnless(_has("numpy", "sklearn", "pandas", "scipy", "joblib"), "PRI test needs pandas/scipy/joblib")
class PriorityModelIntegrationTests(unittest.TestCase):
    """Trains a tiny model with PRI's own scripts, then predicts through the adapter."""

    def test_pri_recall_balanced_model_predicts_through_build_features(self) -> None:
        pri_scripts = WORKSPACE_ROOT / "bug-priority-drone" / "scripts"
        rep_json = WORKSPACE_ROOT / "bug-priority-drone" / "models" / "rep_minus_weights_bm25_p2_error_keywords.json"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            words = {
                "P1": "crash data loss hang deadlock workspace corrupted",
                "P2": "exception error fails broken regression npe",
                "P3": "incorrect wrong behavior unexpected result",
                "P4": "layout alignment label tooltip cosmetic",
                "P5": "typo spelling javadoc wording documentation",
            }
            severities = {"P1": "blocker", "P2": "critical", "P3": "normal", "P4": "minor", "P5": "trivial"}
            rng = random.Random(3)
            start = datetime(2024, 1, 1)
            rows = []
            for index in range(500):
                priority = f"P{index % 5 + 1}"
                vocab = words[priority].split()
                rows.append(
                    {
                        "id": str(1000 + index),
                        "summary": " ".join(rng.sample(vocab, 3)),
                        "description": "when opening the editor " + " ".join(rng.choices(vocab, k=6)),
                        "priority": priority,
                        "severity": severities[priority],
                        "product": "platform",
                        "component": rng.choice(["ui", "debug"]),
                        "creator": f"user{rng.randint(1, 10)}",
                        "creation_time": (start + timedelta(hours=5 * index)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "status": "RESOLVED",
                        "resolution": "FIXED",
                        "dupe_of": "",
                        "fetch_group": "base",
                    }
                )
            with (root / "raw.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)

            (root / "out").mkdir()

            def run(*args: str) -> None:
                done = subprocess.run([sys.executable, *args], cwd=root, capture_output=True, text=True)
                self.assertEqual(done.returncode, 0, done.stderr[-2000:])

            run(str(pri_scripts / "clean_bug_reports.py"), "--input", "raw.csv", "--output", "clean.csv")
            run(str(pri_scripts / "build_experiment_splits.py"), "--input", "clean.csv", "--output-dir", "splits", "--natural-test-size", "0.2", "--balanced-train-per-class", "50", "--balanced-validation-per-class", "15")
            run(str(pri_scripts / "build_features.py"), "--mode", "fit", "--input", "splits/train_balanced_clean.csv", "--feature-dir", "ft", "--rep-weights-json", str(rep_json))
            for name, out in (("validation_balanced_clean.csv", "fv"), ("natural_test_clean.csv", "fn")):
                run(str(pri_scripts / "build_features.py"), "--mode", "transform", "--input", f"splits/{name}", "--history-input", "clean.csv", "--reference-feature-dir", "ft", "--feature-dir", out, "--rep-weights-json", str(rep_json))
            run(
                str(pri_scripts / "train_recall_balanced_priority_model.py"),
                "--train-feature-dir", "ft", "--validation-feature-dir", "fv", "--natural-feature-dir", "fn",
                "--model-path", "out/model.joblib", "--candidate-csv", "out/c.csv", "--eval-csv", "out/e.csv",
                "--class-report-csv", "out/r.csv", "--summary-md", "out/s.md",
            )

            config = config_from_mapping(
                {
                    "workspace_root": str(WORKSPACE_ROOT),
                    "priority": {"model_path": str(root / "out" / "model.joblib"), "reference_feature_dir": str(root / "ft"), "history_csv": str(root / "clean.csv")},
                }
            )
            results = TeamPriorityPredictor(config).predict_many(
                [
                    {"ticket_id": "N1", "title": "Workspace corrupted crash data loss", "description": "hang deadlock", "severity": "blocker", "product": "platform", "component": "debug"},
                    {"ticket_id": "N2", "title": "Typo in javadoc wording", "description": "spelling documentation", "severity": "trivial", "product": "platform", "component": "ui"},
                ]
            )
            self.assertEqual([item["status"] for item in results], ["done", "done"], results[0].get("fallback_reason"))
            self.assertEqual(results[0]["output"]["model"], "recall_balanced_cost_sensitive")
            self.assertEqual(results[0]["output"]["predicted_priority"], "P1")
            self.assertEqual(results[1]["output"]["predicted_priority"], "P5")


if __name__ == "__main__":
    unittest.main()
