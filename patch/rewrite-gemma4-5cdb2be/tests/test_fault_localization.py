from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"

for import_path in (str(SRC_ROOT), str(PROJECT_ROOT)):
    if import_path not in sys.path:
        sys.path.insert(0, import_path)

from modules.bug_localizer import BugLocalizer
import utils.fault_localization as fault_localization
from utils.fault_localization import (
    build_symbol_candidate_pool,
    build_bug_report_text,
    build_code_index,
    localize_ticket,
    rank_symbol_candidates,
)
from utils.llm_client import OllamaClient

from scripts.evaluate_fault_localization import (
    discover_base_commit_existing_gold_files,
    evaluate_records,
)
from scripts.fault_localization import (
    build_parser as build_fault_localization_parser,
    resolve_symbol_modes,
)
from scripts.compare_fault_localization_runs import compare_runs
from scripts.prepare_fault_localization_gold import prepare_gold_records
from scripts.prepare_swebench_lite_fault_localization import gold_record, normalize_record, ticket_record
from scripts.run_swebench_lite_fault_localization import (
    BatchRunConfig,
    ProgressReporter,
    load_or_build_call_graph,
    load_or_build_import_graph,
    run_batch,
)


class DuplicateRankRerankClient:
    def generate_json(self, prompt: str) -> dict:
        return {
            "candidates": [
                {"rank": 1, "score": 0.9, "reason": "first"},
                {"rank": 1, "score": 0.1, "reason": "duplicate should be ignored"},
                {"rank": 2, "score": 0.8, "reason": "second"},
            ]
        }


class PartialRerankClient:
    def generate_json(self, prompt: str) -> dict:
        return {"candidates": [{"rank": 1, "score": 0.0, "reason": "short reason"}]}


class FullPipelineRerankClient:
    def generate_json(self, prompt: str) -> dict:
        if "Symbols:\n" in prompt:
            rows = json.loads(prompt.split("Symbols:\n", 1)[1])
            return {
                "symbols": [
                    {"rank": row["rank"], "score": 1.0, "reason": "symbol match"}
                    for row in rows
                ]
            }
        rows = json.loads(prompt.split("Candidates:\n", 1)[1])
        return {
            "candidates": [
                {"rank": row["rank"], "score": 1.0, "reason": "file match"}
                for row in rows
            ]
        }


class SchemaAwareRerankClient:
    def __init__(self) -> None:
        self.batch_sizes: list[int] = []

    def generate_json_with_schema(self, prompt: str, schema: dict) -> dict:
        root_key = next(iter(schema["properties"]))
        count = int(schema["properties"][root_key]["minItems"])
        self.batch_sizes.append(count)
        return {
            root_key: [
                {"rank": rank, "score": rank / count, "reason": "schema constrained"}
                for rank in range(1, count + 1)
            ]
        }


class FaultLocalizationTests(unittest.TestCase):
    def test_sbert_model_is_cached_within_process(self) -> None:
        fake_module = types.ModuleType("sentence_transformers")
        created: list[object] = []

        class FakeSentenceTransformer:
            def __init__(self, model_name: str, *, local_files_only: bool) -> None:
                created.append(self)

        fake_module.SentenceTransformer = FakeSentenceTransformer  # type: ignore[attr-defined]
        fault_localization._load_sbert_model.cache_clear()
        with patch.dict(sys.modules, {"sentence_transformers": fake_module}):
            first = fault_localization._load_sbert_model("fake-model", True)
            second = fault_localization._load_sbert_model("fake-model", True)
        fault_localization._load_sbert_model.cache_clear()

        self.assertIs(first, second)
        self.assertEqual(len(created), 1)

    def test_ollama_client_maps_curl_timeout_to_hard_deadline(self) -> None:
        with patch(
            "utils.llm_client.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="curl", timeout=1),
        ):
            with self.assertRaisesRegex(TimeoutError, "hard deadline"):
                OllamaClient(timeout=1).generate("rerank this")

    def test_hybrid_backend_bounds_sbert_candidate_pool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            index = build_code_index(repo)
            with patch(
                "utils.fault_localization._sbert_scores",
                return_value=[0.2, 0.9],
            ) as sbert_scores:
                result = localize_ticket(
                    {
                        "ticket_id": "HYBRID-1",
                        "title": "Profile rendering fails for a missing user name",
                        "description": "render_profile raises while reading the user name in src/profile/view.py",
                    },
                    code_index=index,
                    top_k=1,
                    embedding_backend="tfidf-sbert-rerank",
                    semantic_candidate_k=2,
                    candidate_file_k=1,
                    file_aggregation=False,
                )

        self.assertEqual(sbert_scores.call_count, 1)
        self.assertLessEqual(len(sbert_scores.call_args.args[1]), 2)
        self.assertTrue(result["method"]["embedding_backend"].startswith("tfidf+sbert-rerank:"))
        self.assertEqual(result["method"]["semantic_candidate_k"], 2)
        self.assertIn("sbert_score", result["localized_candidates"][0]["signals"])

    def test_hybrid_backend_uses_unique_file_representatives_for_top_twenty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            source = repo / "src"
            source.mkdir(parents=True)
            crowded_functions = "\n\n".join(
                f"def parse_payload_{index}(payload):\n    return payload.strip()"
                for index in range(30)
            )
            (source / "crowded.py").write_text(crowded_functions + "\n", encoding="utf-8")
            for index in range(24):
                (source / f"module_{index:02d}.py").write_text(
                    f"def parse_payload_module_{index}(payload):\n"
                    "    return payload.strip()\n",
                    encoding="utf-8",
                )
            index = build_code_index(repo)

            with patch(
                "utils.fault_localization._sbert_scores",
                side_effect=lambda _query, texts, *_args, **_kwargs: [0.5] * len(texts),
            ) as sbert_scores:
                result = localize_ticket(
                    {
                        "ticket_id": "HYBRID-UNIQUE-FILES",
                        "title": "parse payload normalization fails",
                        "description": "The payload parser returns the wrong normalized value.",
                    },
                    code_index=index,
                    top_k=5,
                    embedding_backend="tfidf-sbert-rerank",
                    semantic_candidate_k=50,
                    candidate_file_k=20,
                    domain_path_routing=False,
                )

        semantic_texts = sbert_scores.call_args.args[1]
        self.assertEqual(len(semantic_texts), 25)
        self.assertEqual(len(result["stage1_candidate_files"]), 20)
        self.assertEqual(
            result["stage1_diagnostics"]["semantic_representative"],
            "best_tfidf_chunk_per_file",
        )

    def test_build_code_index_extracts_python_symbols_and_skips_tests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            index = build_code_index(repo)

        function_names = {chunk.function_name for chunk in index.chunks}
        indexed_files = {chunk.file_path for chunk in index.chunks}
        self.assertIn("validate_token", function_names)
        self.assertIn("src/auth/validator.py", indexed_files)
        self.assertNotIn("tests/test_validator.py", indexed_files)
        self.assertEqual(index.repository_path, ".")

    def test_build_code_index_supports_python_stubs_and_scala_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / "stubs").mkdir(parents=True)
            (repo / "src").mkdir(parents=True)
            (repo / "stubs" / "service.pyi").write_text(
                "def resolve_service(name: str) -> str: ...\n",
                encoding="utf-8",
            )
            (repo / "src" / "Resolver.scala").write_text(
                "object Resolver { def resolveService(name: String): String = name }\n",
                encoding="utf-8",
            )
            index = build_code_index(repo)

        indexed_files = {chunk.file_path for chunk in index.chunks}
        languages = {chunk.file_path: chunk.language for chunk in index.chunks}
        self.assertIn("stubs/service.pyi", indexed_files)
        self.assertIn("src/Resolver.scala", indexed_files)
        self.assertEqual(languages["stubs/service.pyi"], "python")
        self.assertEqual(languages["src/Resolver.scala"], "scala")

    def test_code_index_repository_path_is_portable_between_environments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = _make_repo(tmp_path)
            index = build_code_index(repo)
            index_path = tmp_path / "portable" / "code-index.json"
            index.save(index_path)

            serialized = index_path.read_text(encoding="utf-8")
            loaded = fault_localization.load_code_index(index_path)
            legacy_payload = index.to_dict()
            legacy_payload["repository_path"] = "/old-machine/workspace/repository"
            loaded_legacy = fault_localization.CodeIndex.from_dict(legacy_payload)
            legacy_payload["repository_path"] = r"C:\old-machine\workspace\repository"
            loaded_windows_legacy = fault_localization.CodeIndex.from_dict(legacy_payload)
            result = localize_ticket(
                {
                    "ticket_id": "PORTABLE-INDEX",
                    "title": "validate_token rejects a valid token",
                },
                code_index=loaded,
                top_k=1,
                candidate_file_k=1,
            )

        self.assertEqual(index.repository_path, ".")
        self.assertTrue(index.runtime_repository_matches(repo))
        self.assertEqual(loaded.repository_path, ".")
        self.assertIsNone(loaded.runtime_repository_matches(repo))
        self.assertEqual(loaded_legacy.repository_path, ".")
        self.assertEqual(loaded_windows_legacy.repository_path, ".")
        self.assertNotIn(str(repo.resolve()), serialized)
        self.assertEqual(result["repository_path"], ".")

    def test_bug_report_text_excludes_ids_and_duplicate_content(self) -> None:
        text = build_bug_report_text(
            {
                "ticket_id": "project__secret-answer-123",
                "title": "Parser fails on empty input",
                "bug_report": "Parser fails on empty input",
                "description": "Parser fails on empty input",
                "hints_text": "src/parser.py",
                "fail_to_pass": ["tests/test_parser.py::test_empty"],
            }
        )

        self.assertNotIn("project__secret-answer-123", text)
        self.assertNotIn("hints_text", text)
        self.assertNotIn("fail_to_pass", text)
        self.assertEqual(text.count("Parser fails on empty input"), 1)

    def test_build_code_index_keeps_nested_models_package_but_skips_root_models(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            nested_models = repo / "django" / "db" / "models"
            nested_models.mkdir(parents=True)
            (nested_models / "fields.py").write_text(
                "def parse_model_field(value):\n"
                "    return value\n",
                encoding="utf-8",
            )
            root_models = repo / "models"
            root_models.mkdir()
            (root_models / "generated.py").write_text(
                "def generated_model():\n"
                "    return None\n",
                encoding="utf-8",
            )

            index = build_code_index(repo)

        indexed_files = {chunk.file_path for chunk in index.chunks}
        self.assertIn("django/db/models/fields.py", indexed_files)
        self.assertNotIn("models/generated.py", indexed_files)

    def test_build_code_index_rejects_file_path_and_invalid_size_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_file = Path(tmp) / "module.py"
            source_file.write_text("value = 1\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "not a directory"):
                build_code_index(source_file)
            with self.assertRaisesRegex(ValueError, "max_file_bytes"):
                build_code_index(Path(tmp), max_file_bytes=0)

    def test_localize_ticket_ranks_stack_trace_chunk_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            index = build_code_index(repo)
            result = localize_ticket(
                {
                    "ticket_id": "RAW-2",
                    "title": "Login page crashes when token is missing",
                    "description": "Login crashes with TypeError when token is None.",
                    "component": "authentication",
                    "logs": "TypeError: token is None at src/auth/validator.py:2",
                },
                code_index=index,
                embedding_backend="tfidf",
                top_k=3,
            )

        best = result["localized_candidates"][0]
        self.assertEqual(best["file_path"], "src/auth/validator.py")
        self.assertEqual(best["function_name"], "validate_token")
        self.assertIn("symbol_qualified_name", best)
        self.assertIn("scoring_signals", best)
        self.assertIn("final_score", best["scoring_signals"])
        self.assertEqual(result["bug_location"]["file"], "src/auth/validator.py")
        self.assertIn("embedding_backend", result["method"])
        self.assertIn("evaluation_ready_fields", result)
        self.assertLess(best["score"], 1.0)
        self.assertEqual(result["confidence_level"], "high")
        self.assertTrue(result["recommend_patch_generation"])

    def test_stage1_preserves_twenty_unique_files_before_final_top_k(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            source = repo / "src"
            source.mkdir(parents=True)
            for index in range(25):
                (source / f"module_{index:02d}.py").write_text(
                    f"def parse_payload_{index}(payload):\n"
                    "    return payload.strip()\n",
                    encoding="utf-8",
                )

            result = localize_ticket(
                {
                    "ticket_id": "STAGE1-20",
                    "title": "Payload parser strips invalid input",
                    "description": "parse_payload fails while normalizing payload text.",
                },
                repo_path=repo,
                top_k=5,
                candidate_file_k=20,
            )

        stage1_files = [row["file_path"] for row in result["stage1_candidate_files"]]
        self.assertEqual(len(stage1_files), 20)
        self.assertEqual(len(set(stage1_files)), 20)
        self.assertEqual(len(result["localized_files"]), 5)
        self.assertEqual(result["method"]["candidate_file_k"], 20)
        self.assertEqual(result["stage1_diagnostics"]["stage_boundary"], "before_llm_rerank")
        self.assertEqual(result["evaluation_ready_fields"]["candidate_stage"], "stage1_candidate_files[*].file_path")

    def test_domain_path_routing_connects_framework_concepts_to_source_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            conf = repo / "django" / "conf"
            staticfiles = repo / "django" / "contrib" / "staticfiles"
            conf.mkdir(parents=True)
            staticfiles.mkdir(parents=True)
            (conf / "__init__.py").write_text(
                "class LazySettings:\n"
                "    pass\n",
                encoding="utf-8",
            )
            (staticfiles / "storage.py").write_text(
                "def static_url(path):\n"
                "    return path\n",
                encoding="utf-8",
            )

            result = localize_ticket(
                {
                    "ticket_id": "DOMAIN-ROUTING",
                    "title": "SCRIPT_NAME changes STATIC_URL and MEDIA_URL settings",
                },
                repo_path=repo,
                top_k=2,
                candidate_file_k=2,
            )

        conf_candidate = next(
            row for row in result["stage1_candidate_files"]
            if row["file_path"] == "django/conf/__init__.py"
        )
        self.assertEqual(conf_candidate["scoring_signals"]["domain_path_score"], 0.9)
        self.assertIn("django_settings_conf", conf_candidate["reason"])
        self.assertTrue(result["method"]["domain_path_routing"])

    def test_file_aggregation_returns_unique_files_with_supporting_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            source = repo / "src"
            source.mkdir(parents=True)
            (source / "tokens.py").write_text(
                "class TokenService:\n"
                "    def validate_token(self, token):\n"
                "        return token.strip()\n\n"
                "def normalize_token(token):\n"
                "    return token.strip().lower()\n",
                encoding="utf-8",
            )
            (source / "session.py").write_text(
                "def create_token_session(token):\n"
                "    return token\n",
                encoding="utf-8",
            )
            result = localize_ticket(
                {
                    "ticket_id": "AGG",
                    "title": "Token validation normalization fails",
                    "description": "validate_token and normalize_token mishandle the token.",
                },
                repo_path=repo,
                top_k=5,
                advanced_file_aggregation=True,
            )
            basic_result = localize_ticket(
                {
                    "ticket_id": "AGG-BASIC",
                    "title": "Token validation normalization fails",
                    "description": "validate_token and normalize_token mishandle the token.",
                },
                repo_path=repo,
                top_k=5,
                advanced_file_aggregation=False,
            )

        file_paths = [candidate["file_path"] for candidate in result["localized_candidates"]]
        self.assertEqual(len(file_paths), len(set(file_paths)))
        self.assertEqual(file_paths, [row["file_path"] for row in result["localized_files"]])
        token_candidate = next(row for row in result["localized_candidates"] if row["file_path"] == "src/tokens.py")
        self.assertTrue(token_candidate["supporting_chunks"])
        self.assertEqual(result["method"]["ranking_level"], "file_aggregated_chunks")
        self.assertTrue(result["method"]["advanced_file_aggregation"])
        basic_token = next(
            row for row in basic_result["localized_candidates"]
            if row["file_path"] == "src/tokens.py"
        )
        self.assertFalse(basic_result["method"]["advanced_file_aggregation"])
        self.assertEqual(basic_token["scoring_signals"]["supporting_chunk_bonus"], 0.0)
        self.assertEqual(basic_token["scoring_signals"]["package_proximity_bonus"], 0.0)

    def test_hybrid_e1_modes_keep_pre_sbert_supporting_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            source = repo / "src"
            source.mkdir(parents=True)
            (source / "invoice.py").write_text(
                "def calculate_invoice_tax(invoice):\n"
                "    return invoice.tax_total\n\n"
                "def validate_invoice_tax(invoice):\n"
                "    return invoice.tax_total >= 0\n\n"
                "def format_invoice_tax(invoice):\n"
                "    return str(invoice.tax_total)\n",
                encoding="utf-8",
            )
            (source / "unrelated.py").write_text(
                "def health_check():\n"
                "    return True\n",
                encoding="utf-8",
            )
            index = build_code_index(repo)
            ticket = {
                "ticket_id": "E1-HYBRID",
                "title": "Invoice tax total validation is incorrect",
                "description": "calculate and format invoice tax_total use the wrong value.",
            }

            results: dict[str, dict] = {}
            with patch(
                "utils.fault_localization._sbert_scores",
                side_effect=lambda _query, texts, *_args, **_kwargs: [0.5] * len(texts),
            ):
                for mode in (
                    "basic",
                    "supporting-chunks",
                    "supporting-symbols",
                ):
                    results[mode] = localize_ticket(
                        ticket,
                        code_index=index,
                        top_k=2,
                        candidate_file_k=2,
                        semantic_candidate_k=2,
                        embedding_backend="tfidf-sbert-rerank",
                        domain_path_routing=False,
                        file_aggregation_mode=mode,
                    )

        candidates = {
            mode: next(
                row
                for row in result["stage1_candidate_files"]
                if row["file_path"] == "src/invoice.py"
            )
            for mode, result in results.items()
        }
        self.assertGreaterEqual(
            candidates["supporting-chunks"]["scoring_signals"]["support_evidence_count"],
            2,
        )
        self.assertTrue(candidates["supporting-chunks"]["supporting_chunks"])
        self.assertEqual(candidates["basic"]["scoring_signals"]["supporting_chunk_bonus"], 0.0)
        self.assertGreater(
            candidates["supporting-chunks"]["scoring_signals"]["supporting_chunk_bonus"],
            0.0,
        )
        self.assertEqual(
            candidates["supporting-chunks"]["scoring_signals"]["symbol_coverage_bonus"],
            0.0,
        )
        self.assertGreater(
            candidates["supporting-symbols"]["scoring_signals"]["symbol_coverage_bonus"],
            0.0,
        )
        self.assertEqual(
            results["supporting-symbols"]["method"]["file_aggregation_mode"],
            "supporting-symbols",
        )

    def test_invalid_file_aggregation_mode_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            with self.assertRaisesRegex(ValueError, "file_aggregation_mode"):
                localize_ticket(
                    {"ticket_id": "BAD-E1", "title": "Token validation fails"},
                    repo_path=repo,
                    file_aggregation_mode="unbounded-sum",
                )

    def test_repository_proximity_expands_top_candidate_to_imported_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            source = repo / "src"
            source.mkdir(parents=True)
            (source / "controller.py").write_text(
                "from . import formatter\n\n"
                "def render_invoice(invoice):\n"
                "    return formatter.build(invoice)\n",
                encoding="utf-8",
            )
            (source / "formatter.py").write_text(
                "def build(value):\n"
                "    return str(value)\n",
                encoding="utf-8",
            )
            (source / "unrelated.py").write_text(
                "def health_check():\n"
                "    return True\n",
                encoding="utf-8",
            )

            result = localize_ticket(
                {
                    "ticket_id": "IMPORT-NEIGHBOR",
                    "title": "render_invoice crashes while formatting the invoice",
                    "description": "The controller reaches render_invoice but the returned value is wrong.",
                },
                repo_path=repo,
                top_k=3,
                candidate_file_k=3,
                domain_path_routing=False,
                repository_proximity=True,
            )

        formatter = next(
            row for row in result["stage1_candidate_files"]
            if row["file_path"] == "src/formatter.py"
        )
        signals = formatter["scoring_signals"]
        self.assertGreater(signals["repository_proximity_score"], 0.0)
        self.assertIn(
            "imported_by:src/controller.py",
            signals["matching_repository_proximity"],
        )

    def test_python_from_import_indexes_base_and_imported_module(self) -> None:
        modules = fault_localization._import_line_modules(
            "from matplotlib import cbook, colors as mcolors",
            current_file="lib/matplotlib/figure.py",
        )

        self.assertEqual(
            modules,
            ["matplotlib", "matplotlib.cbook", "matplotlib.colors"],
        )

    def test_import_graph_parses_absolute_relative_alias_reexport_and_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            package = repo / "pkg"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text(
                "from .api import public_api\n",
                encoding="utf-8",
            )
            (package / "api.py").write_text(
                "from .core import run as execute\n"
                "import pkg.helpers as helpers\n\n"
                "def public_api(value):\n"
                "    return execute(value)\n",
                encoding="utf-8",
            )
            (package / "core.py").write_text(
                "from . import api\n\n"
                "def run(value):\n"
                "    return value\n",
                encoding="utf-8",
            )
            (package / "helpers.py").write_text("VALUE = 1\n", encoding="utf-8")
            (package / "caller.py").write_text(
                "from pkg import api\n",
                encoding="utf-8",
            )
            (package / "unresolved.py").write_text(
                "import third_party_missing\n",
                encoding="utf-8",
            )
            index = build_code_index(repo)
            index_path = Path(tmp) / "index.json"
            index.save(index_path)
            loaded = fault_localization.load_code_index(index_path)

        graph = loaded.import_graph
        self.assertIsNotNone(graph)
        assert graph is not None
        self.assertEqual(
            graph.outgoing["pkg/api.py"],
            ["pkg/core.py", "pkg/helpers.py"],
        )
        self.assertIn("pkg/api.py", graph.outgoing["pkg/__init__.py"])
        self.assertIn("pkg/api.py", graph.outgoing["pkg/core.py"])
        self.assertIn("pkg/core.py", graph.incoming["pkg/api.py"])
        self.assertIn("pkg/caller.py", graph.incoming["pkg/api.py"])
        self.assertNotIn("pkg/unresolved.py", graph.outgoing)
        self.assertIn("third_party_missing", graph.unresolved["pkg/unresolved.py"])
        self.assertEqual(graph.stats["edges"], sum(map(len, graph.outgoing.values())))

    def test_bidirectional_import_graph_adds_incoming_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            package = repo / "pkg"
            package.mkdir(parents=True)
            (package / "core.py").write_text(
                "def calculate_total(invoice):\n"
                "    return invoice.total\n",
                encoding="utf-8",
            )
            (package / "adapter.py").write_text(
                "from .core import calculate_total\n\n"
                "def process(value):\n"
                "    return calculate_total(value)\n",
                encoding="utf-8",
            )
            (package / "other.py").write_text(
                "def health_check():\n"
                "    return True\n",
                encoding="utf-8",
            )
            index = build_code_index(repo)
            ticket = {
                "ticket_id": "E2-INCOMING",
                "title": "calculate_total returns the wrong invoice total",
                "description": "The calculation in calculate_total must be corrected.",
            }
            outgoing = localize_ticket(
                ticket,
                code_index=index,
                top_k=3,
                candidate_file_k=3,
                domain_path_routing=False,
                import_graph_mode="outgoing",
            )
            bidirectional = localize_ticket(
                ticket,
                code_index=index,
                top_k=3,
                candidate_file_k=3,
                domain_path_routing=False,
                import_graph_mode="bidirectional",
            )

        outgoing_adapter = next(
            row for row in outgoing["stage1_candidate_files"]
            if row["file_path"] == "pkg/adapter.py"
        )
        bidirectional_adapter = next(
            row for row in bidirectional["stage1_candidate_files"]
            if row["file_path"] == "pkg/adapter.py"
        )
        self.assertNotIn(
            "imports:pkg/core.py",
            outgoing_adapter["scoring_signals"]["matching_repository_proximity"],
        )
        self.assertIn(
            "imports:pkg/core.py",
            bidirectional_adapter["scoring_signals"]["import_graph_evidence"],
        )
        self.assertLessEqual(
            bidirectional_adapter["scoring_signals"]["import_graph_bonus"],
            fault_localization.IMPORT_GRAPH_PARAMETERS["maximum_bonus"],
        )

    def test_import_graph_off_preserves_baseline_and_rejects_unknown_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            index = build_code_index(repo)
            ticket = {"ticket_id": "E2-OFF", "title": "Token validation fails unexpectedly"}
            baseline = localize_ticket(
                ticket,
                code_index=index,
                top_k=3,
                candidate_file_k=3,
                domain_path_routing=False,
            )
            explicit_off = localize_ticket(
                ticket,
                code_index=index,
                top_k=3,
                candidate_file_k=3,
                domain_path_routing=False,
                import_graph_mode="off",
            )
            with self.assertRaisesRegex(ValueError, "import_graph_mode"):
                localize_ticket(
                    ticket,
                    code_index=index,
                    import_graph_mode="recursive",
                )

        baseline_rows = [
            (row["file_path"], row["retrieval_score"])
            for row in baseline["stage1_candidate_files"]
        ]
        explicit_rows = [
            (row["file_path"], row["retrieval_score"])
            for row in explicit_off["stage1_candidate_files"]
        ]
        self.assertEqual(explicit_rows, baseline_rows)

    def test_legacy_index_import_graph_sidecar_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            package = repo / "pkg"
            package.mkdir(parents=True)
            (package / "a.py").write_text("from . import b\n", encoding="utf-8")
            (package / "b.py").write_text("VALUE = 1\n", encoding="utf-8")
            index = build_code_index(repo)
            index.import_graph = None
            config = types.SimpleNamespace(
                import_graph_cache_dir=root / "graphs",
                index_cache_dir=root / "indexes",
                include_tests=False,
                max_file_bytes=500_000,
            )
            ticket = {"ticket_id": "E2-CACHE", "repo": "owner/repo", "base_commit": "abc123"}
            reporter = ProgressReporter("none")
            built, first_source = load_or_build_import_graph(ticket, index, config, reporter)
            loaded, second_source = load_or_build_import_graph(ticket, index, config, reporter)

        self.assertEqual(first_source, "built")
        self.assertEqual(second_source, "cache")
        self.assertEqual(loaded.outgoing, built.outgoing)

    def test_e4_builds_static_local_imported_module_and_self_call_edges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            package = repo / "pkg"
            package.mkdir(parents=True)
            (package / "helpers.py").write_text(
                "def normalize(value):\n"
                "    return value.strip()\n\n"
                "class Worker:\n"
                "    def execute(self, value):\n"
                "        return value\n\n"
                "    def run(self, value):\n"
                "        return self.execute(value)\n",
                encoding="utf-8",
            )
            (package / "service.py").write_text(
                "from .helpers import normalize\n"
                "from . import helpers as helper_module\n\n"
                "def local_step(value):\n"
                "    return value\n\n"
                "def process(value):\n"
                "    value = normalize(value)\n"
                "    value = helper_module.normalize(value)\n"
                "    return local_step(value)\n",
                encoding="utf-8",
            )
            index = build_code_index(repo)
            index_path = Path(tmp) / "index.json"
            index.save(index_path)
            loaded = fault_localization.load_code_index(index_path)

        graph = loaded.call_graph
        self.assertIsNotNone(graph)
        assert graph is not None
        service_edges = graph.outgoing["pkg/service.py"]
        service_targets = {
            (row["target_file"], row["target_symbol"], row["resolution_type"])
            for row in service_edges
        }
        self.assertIn(
            ("pkg/helpers.py", "normalize", "imported_function"),
            service_targets,
        )
        self.assertIn(
            ("pkg/helpers.py", "normalize", "module_function"),
            service_targets,
        )
        self.assertIn(
            ("pkg/service.py", "local_step", "local_function"),
            service_targets,
        )
        helper_targets = {
            (row["target_symbol"], row["resolution_type"])
            for row in graph.outgoing["pkg/helpers.py"]
        }
        self.assertIn(("Worker.execute", "self_method"), helper_targets)
        self.assertGreaterEqual(graph.stats["cross_file_edges"], 2)

    def test_e4_outgoing_call_graph_adds_bounded_one_hop_evidence(self) -> None:
        caller = fault_localization.CodeChunk(
            chunk_id="caller",
            file_path="pkg/service.py",
            language="python",
            symbol_kind="function",
            function_name="process",
            class_name="",
            start_line=1,
            end_line=2,
            code_text="def process():\n    return normalize()\n",
        )
        target = fault_localization.CodeChunk(
            chunk_id="target",
            file_path="pkg/helpers.py",
            language="python",
            symbol_kind="function",
            function_name="normalize",
            class_name="",
            start_line=1,
            end_line=2,
            code_text="def normalize():\n    return True\n",
        )
        graph = fault_localization.CallGraph(
            outgoing={
                "pkg/service.py": [
                    {
                        "caller_file": "pkg/service.py",
                        "caller_symbol": "process",
                        "target_file": "pkg/helpers.py",
                        "target_symbol": "normalize",
                        "call_name": "normalize",
                        "line": 2,
                        "resolution_type": "imported_function",
                    }
                ]
            }
        )
        candidates = [
            fault_localization.LocalizationCandidate(
                chunk=caller,
                score=0.5,
                embedding_score=0.5,
                reason="caller",
                signals={"final_score": 0.5},
            ),
            fault_localization.LocalizationCandidate(
                chunk=target,
                score=0.1,
                embedding_score=0.1,
                reason="target",
                signals={"final_score": 0.1},
            ),
        ]

        reranked = fault_localization._apply_call_graph(
            candidates,
            graph,
            mode="outgoing",
        )

        self.assertEqual(reranked[0].score, 0.5)
        self.assertGreater(reranked[1].score, 0.1)
        self.assertLessEqual(
            reranked[1].signals["call_graph_bonus"],
            fault_localization.CALL_GRAPH_PARAMETERS["maximum_bonus"],
        )
        self.assertEqual(
            reranked[1].signals["call_graph_evidence"][0]["reference_file"],
            "pkg/service.py",
        )

    def test_e4_top3_mode_excludes_fourth_reference_file(self) -> None:
        candidates: list[fault_localization.LocalizationCandidate] = []
        outgoing: dict[str, list[dict[str, object]]] = {}
        for position in range(1, 5):
            reference_file = f"pkg/reference_{position}.py"
            chunk = fault_localization.CodeChunk(
                chunk_id=f"reference-{position}",
                file_path=reference_file,
                language="python",
                symbol_kind="function",
                function_name=f"reference_{position}",
                class_name="",
                start_line=1,
                end_line=2,
                code_text=f"def reference_{position}():\n    return target_{position}()\n",
            )
            candidates.append(
                fault_localization.LocalizationCandidate(
                    chunk=chunk,
                    score=0.9 - position * 0.1,
                    embedding_score=0.9 - position * 0.1,
                    reason="reference",
                    signals={"final_score": 0.9 - position * 0.1},
                )
            )
            target_file = f"pkg/target_{position}.py"
            target = fault_localization.CodeChunk(
                chunk_id=f"target-{position}",
                file_path=target_file,
                language="python",
                symbol_kind="function",
                function_name=f"target_{position}",
                class_name="",
                start_line=1,
                end_line=2,
                code_text=f"def target_{position}():\n    return True\n",
            )
            candidates.append(
                fault_localization.LocalizationCandidate(
                    chunk=target,
                    score=0.1 - position * 0.001,
                    embedding_score=0.1 - position * 0.001,
                    reason="target",
                    signals={"final_score": 0.1 - position * 0.001},
                )
            )
            outgoing[reference_file] = [
                {
                    "caller_file": reference_file,
                    "caller_symbol": f"reference_{position}",
                    "target_file": target_file,
                    "target_symbol": f"target_{position}",
                    "call_name": f"target_{position}",
                    "line": 2,
                    "resolution_type": "imported_function",
                }
            ]

        top3 = fault_localization._apply_call_graph(
            candidates,
            fault_localization.CallGraph(outgoing=outgoing),
            mode="outgoing-top3",
        )
        by_file = {candidate.chunk.file_path: candidate for candidate in top3}

        self.assertGreater(by_file["pkg/target_1.py"].score, 0.099)
        self.assertGreater(by_file["pkg/target_3.py"].score, 0.097)
        self.assertEqual(by_file["pkg/target_4.py"].score, 0.096)
        self.assertNotIn(
            "call_graph_bonus",
            by_file["pkg/target_4.py"].signals,
        )
        self.assertEqual(
            fault_localization.get_call_graph_parameters("outgoing-top3")[
                "reference_file_k"
            ],
            3,
        )

    def test_e4_call_graph_sidecar_is_reused_and_off_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            package = repo / "pkg"
            package.mkdir(parents=True)
            (package / "a.py").write_text(
                "from .b import work\n\ndef run():\n    return work()\n",
                encoding="utf-8",
            )
            (package / "b.py").write_text(
                "def work():\n    return True\n",
                encoding="utf-8",
            )
            index = build_code_index(repo)
            index.call_graph = None
            config = types.SimpleNamespace(
                call_graph_cache_dir=root / "graphs",
                index_cache_dir=root / "indexes",
                include_tests=False,
                max_file_bytes=500_000,
            )
            ticket = {
                "ticket_id": "E4-CACHE",
                "repo": "owner/repo",
                "base_commit": "abc123",
            }
            reporter = ProgressReporter("none")
            built, first_source = load_or_build_call_graph(
                ticket, index, config, reporter
            )
            loaded, second_source = load_or_build_call_graph(
                ticket, index, config, reporter
            )
            with self.assertRaisesRegex(ValueError, "call_graph_mode"):
                localize_ticket(
                    {"ticket_id": "E4-BAD", "title": "bad mode"},
                    code_index=index,
                    call_graph_mode="recursive",
                )

        self.assertEqual(first_source, "built")
        self.assertEqual(second_source, "cache")
        self.assertEqual(loaded.outgoing, built.outgoing)

    def test_e3_extracts_only_code_like_ticket_program_names(self) -> None:
        names = fault_localization.extract_ticket_program_names(
            "The invoice page fails when `normalize` calls "
            "InvoiceService.calculate_total() and render_invoice(). "
            "The expected result should remain unchanged."
        )

        normalized = {name.casefold() for name in names}
        self.assertIn("normalize", normalized)
        self.assertIn("invoiceservice.calculate_total", normalized)
        self.assertIn("render_invoice", normalized)
        self.assertNotIn("expected", normalized)
        self.assertNotIn("result", normalized)
        prefixed = fault_localization.extract_ticket_program_names(
            build_bug_report_text(
                {
                    "title": "render_invoice() fails",
                    "description": "The bug_report field label is not a Symbol.",
                }
            )
        )
        self.assertNotIn("bug_report", {name.casefold() for name in prefixed})

    def test_e3_symbol_definition_index_is_serialized_and_traceable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            package = repo / "billing"
            package.mkdir(parents=True)
            (package / "service.py").write_text(
                "class InvoiceService:\n"
                "    def calculate_total(self, invoice):\n"
                "        return invoice.total\n",
                encoding="utf-8",
            )
            (package / "unrelated.py").write_text(
                "def render_dashboard(value):\n"
                "    return value\n",
                encoding="utf-8",
            )
            index = build_code_index(repo)
            index_path = Path(tmp) / "index.json"
            index.save(index_path)
            loaded = fault_localization.load_code_index(index_path)

            result = localize_ticket(
                {
                    "ticket_id": "E3-DEFINITION",
                    "title": "InvoiceService.calculate_total() returns the wrong value",
                    "description": "The public billing result is incorrect.",
                },
                code_index=loaded,
                top_k=2,
                candidate_file_k=2,
                domain_path_routing=False,
                symbol_expansion_mode="symbol-definitions",
            )

        self.assertIn("invoiceservice.calculate_total", loaded.symbol_definitions)
        definition = loaded.symbol_definitions["invoiceservice.calculate_total"][0]
        self.assertEqual(definition["file_path"], "billing/service.py")
        service = next(
            row
            for row in result["stage1_candidate_files"]
            if row["file_path"] == "billing/service.py"
        )
        signals = service["scoring_signals"]
        self.assertGreater(signals["symbol_definition_bonus"], 0.0)
        self.assertEqual(
            signals["symbol_definition_evidence"][0]["ticket_symbol"],
            "InvoiceService.calculate_total",
        )
        self.assertIn("stage1_symbol_definition_expansion", result["method"]["stages"])
        self.assertEqual(result["method"]["symbol_expansion_mode"], "symbol-definitions")
        self.assertIn(
            "InvoiceService.calculate_total",
            result["stage1_diagnostics"]["symbol_expansion"]["ticket_program_names"],
        )

    def test_e3_strict_disables_dotted_name_leaf_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            package = repo / "pkg"
            package.mkdir(parents=True)
            (package / "release.py").write_text(
                "class UnrelatedRelease:\n"
                "    def version(self):\n"
                "        return '1.0'\n",
                encoding="utf-8",
            )
            (package / "arithmetic.py").write_text(
                "class Arithmetic:\n"
                "    def multiply(self, left, right):\n"
                "        return left * right\n",
                encoding="utf-8",
            )
            index = build_code_index(repo)
            ticket = {
                "ticket_id": "E3-STRICT-LEAF",
                "title": "self.version returns an outdated value",
                "description": "The version accessor is incorrect.",
            }
            legacy = localize_ticket(
                ticket,
                code_index=index,
                top_k=1,
                candidate_file_k=1,
                domain_path_routing=False,
                symbol_expansion_mode="symbol-definitions",
            )
            strict = localize_ticket(
                ticket,
                code_index=index,
                top_k=1,
                candidate_file_k=1,
                domain_path_routing=False,
                symbol_expansion_mode="symbol-definitions-strict",
            )
            guarded = localize_ticket(
                {
                    "ticket_id": "E3-GUARDED-LEAF",
                    "title": "`nref_mask.multiply` fails while `self.version` is present",
                },
                code_index=index,
                top_k=2,
                candidate_file_k=2,
                domain_path_routing=False,
                symbol_expansion_mode="symbol-definitions-guarded",
            )

        legacy_signals = legacy["stage1_candidate_files"][0]["scoring_signals"]
        strict_signals = strict["stage1_candidate_files"][0]["scoring_signals"]
        strict_diagnostics = strict["stage1_diagnostics"]["symbol_expansion"]
        self.assertEqual(
            legacy_signals["symbol_definition_evidence"][0]["ticket_symbol"],
            "self.version",
        )
        self.assertEqual(
            legacy_signals["symbol_definition_evidence"][0]["match_type"],
            "leaf",
        )
        self.assertEqual(strict_signals["symbol_definition_evidence"], [])
        self.assertEqual(strict_diagnostics["dotted_leaf_fallback_policy"], "disabled")
        self.assertEqual(strict_diagnostics["skipped_dotted_leaf_fallback_count"], 1)
        self.assertEqual(
            strict_diagnostics["skipped_dotted_leaf_fallback_names"],
            ["self.version"],
        )
        self.assertEqual(
            strict["method"]["symbol_expansion_policy"]["dotted_leaf_fallback"],
            "disabled",
        )
        guarded_diagnostics = guarded["stage1_diagnostics"]["symbol_expansion"]
        guarded_evidence = guarded_diagnostics["evidence"]
        self.assertEqual(guarded_diagnostics["dotted_leaf_fallback_policy"], "guarded")
        self.assertTrue(
            any(
                row["ticket_symbol"] == "nref_mask.multiply"
                and row["match_type"] == "leaf"
                for row in guarded_evidence
            )
        )
        self.assertFalse(
            any(row["ticket_symbol"] == "self.version" for row in guarded_evidence)
        )
        self.assertIn(
            "self.version",
            guarded_diagnostics["skipped_dotted_leaf_fallback_names"],
        )

    def test_e3_c_indexes_reexports_and_direct_wrapper_implementations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            package = repo / "pkg"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text(
                "from .api import public_api\n",
                encoding="utf-8",
            )
            (package / "api.py").write_text(
                "from .core import run as execute\n\n"
                "def public_api(value):\n"
                "    return execute(value)\n\n"
                "def not_a_direct_wrapper(value):\n"
                "    result = execute(value)\n"
                "    return result\n",
                encoding="utf-8",
            )
            (package / "core.py").write_text(
                "def run(value):\n"
                "    return value\n",
                encoding="utf-8",
            )
            (package / "unrelated.py").write_text(
                "def public_dashboard(value):\n"
                "    return value\n",
                encoding="utf-8",
            )
            index = build_code_index(repo)
            index_path = Path(tmp) / "index.json"
            index.save(index_path)
            loaded = fault_localization.load_code_index(index_path)
            result = localize_ticket(
                {
                    "ticket_id": "E3-C-API",
                    "title": "public_api() returns an incorrect result",
                    "description": "The public API delegates to the wrong behavior.",
                },
                code_index=loaded,
                top_k=4,
                candidate_file_k=4,
                domain_path_routing=False,
                symbol_expansion_mode="symbol-definitions-api",
            )

        links = loaded.api_implementation_links["public_api"]
        relations = {
            (row["relation_type"], row["source_file"], row["target_file"])
            for row in links
        }
        self.assertIn(("reexport", "pkg/__init__.py", "pkg/api.py"), relations)
        self.assertIn(("wrapper", "pkg/api.py", "pkg/core.py"), relations)
        self.assertNotIn("not_a_direct_wrapper", loaded.api_implementation_links)

        core = next(
            row
            for row in result["stage1_candidate_files"]
            if row["file_path"] == "pkg/core.py"
        )
        api_evidence = core["scoring_signals"]["api_implementation_evidence"]
        self.assertTrue(
            any(
                row["ticket_symbol"] == "public_api"
                and row["implementation_symbol"] == "run"
                and row["relation_type"] == "wrapper"
                for row in api_evidence
            )
        )
        self.assertGreater(
            core["scoring_signals"]["api_implementation_bonus"],
            0.0,
        )
        self.assertIn(
            "stage1_api_implementation_expansion",
            result["method"]["stages"],
        )
        diagnostics = result["stage1_diagnostics"]["symbol_expansion"]
        self.assertTrue(diagnostics["api_implementation_enabled"])
        self.assertIn("pkg/core.py", diagnostics["matched_implementation_files"])
        self.assertGreater(diagnostics["returned_api_evidence_count"], 0)

    def test_e3_c_caps_ambiguous_api_implementation_links(self) -> None:
        chunks = [
            fault_localization.CodeChunk(
                chunk_id=f"chunk-{position}",
                file_path=f"pkg/impl_{position}.py",
                language="python",
                symbol_kind="function",
                function_name=f"implementation_{position}",
                class_name="",
                start_line=1,
                end_line=2,
                code_text=f"def implementation_{position}():\n    return {position}\n",
            )
            for position in range(5)
        ]
        links = {
            "public_api": [
                {
                    "api_name": "public_api",
                    "source_file": "pkg/__init__.py",
                    "source_symbol": "public_api",
                    "source_line": 1,
                    "target_file": chunk.file_path,
                    "target_symbol": chunk.function_name,
                    "target_symbol_kind": "function",
                    "target_start_line": 1,
                    "relation_type": "reexport",
                    "via_name": chunk.function_name,
                }
                for chunk in chunks
            ]
        }
        candidates = [
            fault_localization.LocalizationCandidate(
                chunk=chunk,
                score=0.1,
                embedding_score=0.1,
                reason="baseline",
                signals={"final_score": 0.1},
            )
            for chunk in chunks
        ]

        reranked = fault_localization._apply_api_implementation_expansion(
            candidates,
            links,
            ["public_api"],
            mode="symbol-definitions-api",
        )

        self.assertEqual([row.score for row in reranked], [0.1] * 5)
        self.assertTrue(
            all(not row.signals.get("api_implementation_evidence") for row in reranked)
        )

    def test_e3_c_namespace_guard_rejects_wrong_prefix_and_keeps_matching_prefix(self) -> None:
        now_chunk = fault_localization.CodeChunk(
            chunk_id="now",
            file_path="django/db/models/functions/datetime.py",
            language="python",
            symbol_kind="class",
            function_name="",
            class_name="Now",
            start_line=1,
            end_line=2,
            code_text="class Now:\n    pass\n",
        )
        image_chunk = fault_localization.CodeChunk(
            chunk_id="image",
            file_path="astropy/io/fits/hdu/image.py",
            language="python",
            symbol_kind="class",
            function_name="",
            class_name="ImageHDU",
            start_line=1,
            end_line=2,
            code_text="class ImageHDU:\n    pass\n",
        )

        def link(
            *, api: str, source: str, target: fault_localization.CodeChunk
        ) -> dict[str, object]:
            return {
                "api_name": api,
                "source_file": source,
                "source_symbol": api,
                "source_line": 1,
                "target_file": target.file_path,
                "target_symbol": target.class_name,
                "target_symbol_kind": "class",
                "target_start_line": 1,
                "relation_type": "reexport",
                "via_name": target.class_name,
            }

        links = {
            "now": [
                link(
                    api="Now",
                    source="django/db/models/functions/__init__.py",
                    target=now_chunk,
                )
            ],
            "imagehdu": [
                link(
                    api="ImageHDU",
                    source="astropy/io/fits/hdu/__init__.py",
                    target=image_chunk,
                )
            ],
        }
        candidates = [
            fault_localization.LocalizationCandidate(
                chunk=chunk,
                score=0.1,
                embedding_score=0.1,
                reason="baseline",
                signals={"final_score": 0.1},
            )
            for chunk in (now_chunk, image_chunk)
        ]
        legacy = fault_localization._apply_api_implementation_expansion(
            candidates,
            links,
            ["timezone.now", "fits.ImageHDU"],
            mode="symbol-definitions-api",
        )
        guarded = fault_localization._apply_api_implementation_expansion(
            candidates,
            links,
            ["timezone.now", "fits.ImageHDU", "p.group"],
            mode="symbol-definitions-api-namespace",
        )

        self.assertGreater(legacy[0].score, 0.1)
        self.assertEqual(guarded[0].score, 0.1)
        self.assertGreater(guarded[1].score, 0.1)
        self.assertTrue(
            fault_localization._api_link_namespace_matches(
                "fits.imagehdu", links["imagehdu"][0]
            )
        )
        self.assertFalse(
            fault_localization._api_link_namespace_matches(
                "p.group", links["imagehdu"][0]
            )
        )

    def test_e3_off_preserves_baseline_and_rejects_unknown_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            index = build_code_index(repo)
            ticket = {
                "ticket_id": "E3-OFF",
                "title": "validate_token() fails unexpectedly",
            }
            baseline = localize_ticket(
                ticket,
                code_index=index,
                top_k=2,
                candidate_file_k=2,
                domain_path_routing=False,
            )
            explicit_off = localize_ticket(
                ticket,
                code_index=index,
                top_k=2,
                candidate_file_k=2,
                domain_path_routing=False,
                symbol_expansion_mode="off",
            )
            with self.assertRaisesRegex(ValueError, "symbol_expansion_mode"):
                localize_ticket(
                    ticket,
                    code_index=index,
                    symbol_expansion_mode="recursive",
                )

        baseline_rows = [
            (row["file_path"], row["retrieval_score"])
            for row in baseline["stage1_candidate_files"]
        ]
        explicit_rows = [
            (row["file_path"], row["retrieval_score"])
            for row in explicit_off["stage1_candidate_files"]
        ]
        self.assertEqual(explicit_rows, baseline_rows)

    def test_index_covers_module_level_code_and_precise_typescript_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            source = repo / "src"
            source.mkdir(parents=True)
            (source / "settings.py").write_text(
                "AUTH_TOKEN_TTL = 0\n\n"
                "def load_settings():\n"
                "    return AUTH_TOKEN_TTL\n",
                encoding="utf-8",
            )
            (source / "handlers.ts").write_text(
                "function firstHandler() {\n"
                "  return 1;\n"
                "}\n\n"
                "function secondHandler() {\n"
                "  return 2;\n"
                "}\n",
                encoding="utf-8",
            )
            index = build_code_index(repo)

        module_chunks = [
            chunk for chunk in index.chunks
            if chunk.file_path == "src/settings.py" and chunk.symbol_kind == "module"
        ]
        self.assertTrue(any("AUTH_TOKEN_TTL" in chunk.code_text for chunk in module_chunks))
        ts_functions = {
            chunk.function_name: (chunk.start_line, chunk.end_line)
            for chunk in index.chunks
            if chunk.file_path == "src/handlers.ts" and chunk.function_name
        }
        self.assertEqual(ts_functions["firstHandler"], (1, 3))
        self.assertEqual(ts_functions["secondHandler"], (5, 7))

    def test_incremental_index_reuses_only_unchanged_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            first = build_code_index(repo)
            unchanged = build_code_index(repo, previous_index=first)
            (repo / "src" / "auth" / "validator.py").write_text(
                "def validate_token(token):\n"
                "    return token.strip() if token else ''\n",
                encoding="utf-8",
            )
            changed = build_code_index(repo, previous_index=unchanged)

        self.assertEqual(unchanged.settings["index_stats"]["reused_files"], 2)
        self.assertEqual(unchanged.settings["index_stats"]["rebuilt_files"], 0)
        self.assertEqual(changed.settings["index_stats"]["reused_files"], 1)
        self.assertEqual(changed.settings["index_stats"]["rebuilt_files"], 1)

    def test_empty_ticket_is_blocked_by_confidence_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            result = localize_ticket({"ticket_id": "EMPTY"}, repo_path=repo)

        self.assertEqual(result["localized_candidates"], [])
        self.assertEqual(result["input_validation"]["status"], "error")
        self.assertEqual(result["confidence_level"], "low")
        self.assertTrue(result["should_manual_review"])
        self.assertFalse(result["recommend_patch_generation"])

    def test_localize_ticket_validates_backend_and_handles_llm_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            index = build_code_index(repo)

            with self.assertRaisesRegex(ValueError, "embedding_backend"):
                localize_ticket(
                    {"ticket_id": "BAD-BACKEND", "title": "Login fails"},
                    code_index=index,
                    embedding_backend="typo",
                )

            result = localize_ticket(
                {"ticket_id": "NO-CLIENT", "title": "Login token fails"},
                code_index=index,
                llm_rerank=True,
            )

        self.assertFalse(result["method"]["llm_rerank"])
        self.assertTrue(any("no LLM client" in warning for warning in result["warnings"]))

    def test_llm_rerank_ignores_duplicate_candidate_ranks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            result = localize_ticket(
                {
                    "ticket_id": "RERANK",
                    "title": "Login token and profile rendering fail",
                    "description": "Check token validation and profile rendering.",
                },
                repo_path=repo,
                top_k=2,
                llm_client=DuplicateRankRerankClient(),
                llm_rerank=True,
            )

        chunk_ids = [candidate["chunk_id"] for candidate in result["localized_candidates"]]
        self.assertEqual(len(chunk_ids), len(set(chunk_ids)))
        self.assertTrue(result["method"]["llm_rerank"])
        self.assertIn("llm_blended_score", result["localized_candidates"][0]["signals"])

    def test_llm_rerank_rejects_incomplete_candidate_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            result = localize_ticket(
                {
                    "ticket_id": "PARTIAL-RERANK",
                    "title": "Login token and profile rendering fail",
                    "description": "Check token validation and profile rendering.",
                },
                repo_path=repo,
                top_k=2,
                llm_client=PartialRerankClient(),
                llm_rerank=True,
            )

        self.assertFalse(result["method"]["llm_rerank"])
        self.assertTrue(any("no usable candidates" in warning for warning in result["warnings"]))

    def test_full_pipeline_connects_stage1_files_to_indexed_ast_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            result = localize_ticket(
                {
                    "ticket_id": "FULL-PIPELINE",
                    "title": "Login token validation fails",
                    "description": "The token validator rejects a valid login token.",
                },
                repo_path=repo,
                top_k=2,
                candidate_file_k=3,
                llm_client=FullPipelineRerankClient(),
                llm_rerank=True,
                llm_candidate_k=3,
                symbol_localization=True,
                symbol_llm_rerank=True,
                symbol_candidate_k=10,
                symbol_top_k=3,
            )

        self.assertEqual(len(result["stage1_candidate_files"]), 2)
        self.assertEqual(len(result["stage2_localized_files"]), 2)
        self.assertTrue(result["stage3_candidate_symbols"])
        self.assertTrue(result["stage3_retrieval_symbols"])
        self.assertTrue(result["stage3_ranked_symbols"])
        self.assertTrue(result["stage3_diagnostics"]["eligible"])
        self.assertTrue(result["stage3_diagnostics"]["llm_attempted"])
        self.assertTrue(result["stage3_diagnostics"]["llm_rerank_used"])
        self.assertTrue(result["stage3_diagnostics"]["llm_valid"])
        self.assertFalse(result["stage3_diagnostics"]["fallback_used"])
        self.assertEqual(result["stage3_diagnostics"]["fallback_reason"], "")
        self.assertEqual(
            result["evaluation_ready_fields"]["symbol_level"],
            "stage3_ranked_symbols[*].symbol_qualified_name",
        )

    def test_symbol_localization_runs_without_calling_llm(self) -> None:
        class UnexpectedLlmClient:
            def generate_json(self, prompt: str, schema: dict | None = None) -> dict:
                raise AssertionError("deterministic symbol localization called the LLM")

        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            result = localize_ticket(
                {
                    "ticket_id": "SYMBOL-BASELINE",
                    "title": "Login token validation fails",
                    "description": "The token validator rejects a valid login token.",
                },
                repo_path=repo,
                top_k=2,
                llm_client=UnexpectedLlmClient(),
                symbol_localization=True,
                symbol_candidate_k=30,
                symbol_top_k=5,
            )

        self.assertTrue(result["stage3_candidate_symbols"])
        self.assertEqual(
            result["stage3_retrieval_symbols"],
            result["stage3_ranked_symbols"],
        )
        self.assertTrue(result["method"]["symbol_localization"])
        self.assertFalse(result["method"]["symbol_llm_rerank_requested"])
        diagnostics = result["stage3_diagnostics"]
        self.assertFalse(diagnostics["requested"])
        self.assertFalse(diagnostics["llm_attempted"])
        self.assertFalse(diagnostics["fallback_used"])
        self.assertEqual(diagnostics["fallback_reason"], "llm_not_requested")
        self.assertEqual(diagnostics["source"], "symbol_retrieval")

    def test_b1_symbol_retrieval_records_structured_evidence(self) -> None:
        chunks = [
            fault_localization.CodeChunk(
                chunk_id="src/target.py:10-12:target_handler",
                file_path="src/target.py",
                language="python",
                symbol_kind="function",
                function_name="target_handler",
                class_name="",
                start_line=10,
                end_line=12,
                code_text="def target_handler(value):\n    return value",
            ),
            fault_localization.CodeChunk(
                chunk_id="src/other.py:1-3:other_handler",
                file_path="src/other.py",
                language="python",
                symbol_kind="function",
                function_name="other_handler",
                class_name="",
                start_line=1,
                end_line=3,
                code_text="def other_handler(value):\n    return value",
            ),
        ]

        ranked = rank_symbol_candidates(
            {"title": "Calling `target_handler` fails"},
            chunks,
            stage2_file_scores={"src/target.py": 0.5, "src/other.py": 1.0},
            mode="b1-structured",
            top_k=2,
        )

        self.assertEqual(ranked[0].chunk.function_name, "target_handler")
        self.assertEqual(
            ranked[0].signals["exact_program_name_match"],
            "target_handler",
        )
        self.assertEqual(ranked[0].signals["identifier_score"], 1.0)

    def test_symbol_candidate_pool_exposes_one_module_identity_per_file(self) -> None:
        generic_chunk = fault_localization.CodeChunk(
            chunk_id="src/settings.py:1-2:chunk",
            file_path="src/settings.py",
            language="python",
            symbol_kind="chunk",
            function_name="",
            class_name="",
            start_line=1,
            end_line=2,
            code_text="FEATURE_FLAG = True",
        )

        pool = build_symbol_candidate_pool(
            [generic_chunk],
            {"src/settings.py"},
        )

        self.assertEqual(len(pool), 1)
        self.assertEqual(pool[0].symbol_kind, "module")
        self.assertEqual(pool[0].symbol_qualified_name, "<module>")
        self.assertIn("FEATURE_FLAG", pool[0].code_text)

    def test_existing_module_chunks_have_exact_module_qualified_name(self) -> None:
        module_chunk = fault_localization.CodeChunk(
            chunk_id="src/settings.py:1-2:module",
            file_path="src/settings.py",
            language="python",
            symbol_kind="module",
            function_name="",
            class_name="",
            start_line=1,
            end_line=2,
            code_text="FEATURE_FLAG = True",
        )

        pool = build_symbol_candidate_pool(
            [module_chunk],
            {"src/settings.py"},
        )

        self.assertEqual(len(pool), 1)
        self.assertEqual(pool[0].symbol_qualified_name, "<module>")

    def test_symbol_per_file_quota_diversifies_before_backfill(self) -> None:
        chunks = [
            fault_localization.CodeChunk(
                chunk_id=f"src/a.py:{index}-{index}:a_{index}",
                file_path="src/a.py",
                language="python",
                symbol_kind="function",
                function_name=f"a_{index}",
                class_name="",
                start_line=index,
                end_line=index,
                code_text="target target target",
            )
            for index in range(1, 4)
        ]
        chunks.append(
            fault_localization.CodeChunk(
                chunk_id="src/b.py:1-1:b_1",
                file_path="src/b.py",
                language="python",
                symbol_kind="function",
                function_name="b_1",
                class_name="",
                start_line=1,
                end_line=1,
                code_text="target",
            )
        )

        ranked = rank_symbol_candidates(
            {"title": "target failure"},
            chunks,
            mode="b0-tfidf",
            top_k=3,
            per_file_quota=1,
        )

        self.assertEqual(len(ranked), 3)
        self.assertEqual(
            {ranked[0].chunk.file_path, ranked[1].chunk.file_path},
            {"src/a.py", "src/b.py"},
        )

    def test_module_reserved_selector_keeps_each_stage2_file_module(self) -> None:
        candidates = [
            fault_localization.LocalizationCandidate(
                chunk=fault_localization.CodeChunk(
                    chunk_id=f"{path}:{rank}-{rank}:{name}",
                    file_path=path,
                    language="python",
                    symbol_kind=kind,
                    function_name="" if kind == "module" else name,
                    class_name="",
                    start_line=rank,
                    end_line=rank,
                    code_text=name,
                ),
                score=1.0 - rank / 100,
                embedding_score=1.0 - rank / 100,
                reason="test",
            )
            for rank, (path, kind, name) in enumerate(
                [
                    ("src/a.py", "function", "a1"),
                    ("src/a.py", "function", "a2"),
                    ("src/a.py", "function", "a3"),
                    ("src/a.py", "module", ""),
                    ("src/b.py", "module", ""),
                ],
                start=1,
            )
        ]

        selected = fault_localization._select_coverage_aware_symbols(
            candidates,
            top_k=4,
            stage2_file_scores={"src/a.py": 1.0, "src/b.py": 0.5},
            mode="module-reserved",
        )

        self.assertEqual(len(selected), 4)
        self.assertEqual(
            {
                candidate.chunk.file_path
                for candidate in selected
                if candidate.chunk.symbol_kind == "module"
            },
            {"src/a.py", "src/b.py"},
        )

    def test_coverage_selector_expands_init_from_a_strong_class_family(self) -> None:
        rows = [
            ("class", "Service", 1),
            ("function", "unrelated", 2),
            ("module", "", 3),
            ("method", "Service.run", 4),
            ("method", "Service.__init__", 5),
        ]
        candidates = [
            fault_localization.LocalizationCandidate(
                chunk=fault_localization.CodeChunk(
                    chunk_id=f"src/service.py:{rank}-{rank}:{name or kind}",
                    file_path="src/service.py",
                    language="python",
                    symbol_kind=kind,
                    function_name="" if kind in {"class", "module"} else name,
                    class_name=name if kind == "class" else "",
                    start_line=rank,
                    end_line=rank,
                    code_text=name,
                ),
                score=1.0 - rank / 100,
                embedding_score=1.0 - rank / 100,
                reason="test",
            )
            for kind, name, rank in rows
        ]

        selected = fault_localization._select_coverage_aware_symbols(
            candidates,
            top_k=3,
            stage2_file_scores={"src/service.py": 1.0},
            mode="coverage-aware-v1",
        )

        self.assertEqual(len(selected), 3)
        self.assertIn(
            "Service.__init__",
            [candidate.chunk.symbol_qualified_name for candidate in selected],
        )

    def test_call_neighbors_expand_both_directions_and_ignore_text_mentions(self) -> None:
        for anchor_name in ("caller", "callee"):
            with self.subTest(anchor=anchor_name):
                rows = [
                    ("caller", 1, "def caller():\n    return callee()"),
                    ("callee", 4, "def callee():\n    return 1"),
                    ("decoy", 7, "def decoy():\n    return 'caller callee'"),
                ]
                rows.sort(key=lambda row: 0 if row[0] == anchor_name else 1 if row[0] == "decoy" else 2)
                candidates = [
                    fault_localization.LocalizationCandidate(
                        chunk=fault_localization.CodeChunk(
                            chunk_id=name, file_path="a.py", language="python",
                            symbol_kind="function", function_name=name, class_name="",
                            start_line=line, end_line=line + 1, code_text=code,
                        ),
                        score=1.0 - rank / 10, embedding_score=1.0 - rank / 10,
                        reason="test",
                    )
                    for rank, (name, line, code) in enumerate(rows)
                ]
                selected = fault_localization._select_coverage_aware_symbols(
                    candidates, top_k=2, stage2_file_scores={"a.py": 1.0},
                    mode="call-neighborhood-v1",
                )
                self.assertEqual({item.chunk.function_name for item in selected},
                                 {"caller", "callee"})

    def test_call_neighbors_v2_use_cached_repository_graph(self) -> None:
        names = ["caller", "decoy", "callee"]
        candidates = [
            fault_localization.LocalizationCandidate(
                chunk=fault_localization.CodeChunk(
                    chunk_id=name, file_path="a.py", language="python",
                    symbol_kind="function", function_name=name, class_name="",
                    start_line=rank * 3 + 1, end_line=rank * 3 + 2,
                    code_text=f"def {name}():\n    return 1",
                ),
                score=1.0 - rank / 10, embedding_score=1.0 - rank / 10,
                reason="test",
            )
            for rank, name in enumerate(names)
        ]
        graph = fault_localization.CallGraph(
            outgoing={
                "a.py": [{
                    "caller_file": "a.py", "caller_symbol": "caller",
                    "target_file": "a.py", "target_symbol": "callee",
                    "call_name": "callee", "line": 2,
                    "resolution_type": "local_function",
                }]
            }
        )

        selected = fault_localization._select_coverage_aware_symbols(
            candidates, top_k=2, stage2_file_scores={"a.py": 1.0},
            mode="call-neighborhood-v2", call_graph=graph,
        )

        self.assertEqual([item.chunk.function_name for item in selected],
                         ["caller", "callee"])

    def test_stage3_pool_adds_ticket_seed_and_cross_file_called_helper(self) -> None:
        def chunk(path: str, name: str, line: int) -> fault_localization.CodeChunk:
            return fault_localization.CodeChunk(
                chunk_id=f"{path}:{name}", file_path=path, language="python",
                symbol_kind="function", function_name=name, class_name="",
                start_line=line, end_line=line + 1,
                code_text=f"def {name}():\n    return 1",
            )

        stage2 = chunk("src/report.py", "render_report", 1)
        ticket_seed = chunk("src/model.py", "compare_values", 1)
        helper = chunk("src/helpers.py", "report_diff_values", 1)
        decoy = chunk("src/other.py", "unrelated", 1)
        repository = [stage2, ticket_seed, helper, decoy]
        graph = fault_localization.CallGraph(outgoing={
            "src/model.py": [{
                "caller_file": "src/model.py", "caller_symbol": "compare_values",
                "target_file": "src/helpers.py", "target_symbol": "report_diff_values",
                "call_name": "report_diff_values", "line": 2,
                "resolution_type": "imported_symbol",
            }]
        })

        expanded, diagnostics = fault_localization.expand_stage3_symbol_candidate_pool(
            [stage2], repository,
            ticket_program_names=["compare_values"],
            symbol_definitions=fault_localization.build_symbol_definition_index(repository),
            call_graph=graph,
        )

        identities = {(item.file_path, item.function_name) for item in expanded}
        self.assertIn(("src/model.py", "compare_values"), identities)
        self.assertIn(("src/helpers.py", "report_diff_values"), identities)
        self.assertNotIn(("src/other.py", "unrelated"), identities)
        self.assertEqual(diagnostics["ticket_seed_count"], 1)
        self.assertEqual(diagnostics["call_neighbor_count"], 1)

    def test_stage3_pool_does_not_use_unlisted_ticket_fields(self) -> None:
        public_text = fault_localization.build_bug_report_text({
            "description": "Rendering is wrong.",
            "hints_text": "Call secret_helper().",
            "patch": "secret_helper()",
        })
        self.assertNotIn("secret_helper", public_text)
        self.assertNotIn(
            "secret_helper",
            fault_localization.extract_ticket_program_names(public_text),
        )

    def test_called_helper_inherits_bounded_caller_score(self) -> None:
        def candidate(path: str, name: str, score: float, stage2_score: float):
            return fault_localization.LocalizationCandidate(
                chunk=fault_localization.CodeChunk(
                    chunk_id=f"{path}:{name}", file_path=path, language="python",
                    symbol_kind="function", function_name=name, class_name="",
                    start_line=1, end_line=2, code_text=f"def {name}(): pass",
                ),
                score=score, embedding_score=score, reason="test.",
                signals={"stage2_file_score": stage2_score},
            )

        caller = candidate("src/caller.py", "caller", 0.5, 1.0)
        helper = candidate("src/helper.py", "helper", 0.2, 0.2)
        decoy = candidate("src/decoy.py", "decoy", 0.3, 0.0)
        graph = fault_localization.CallGraph(outgoing={
            "src/caller.py": [{
                "caller_file": "src/caller.py", "caller_symbol": "caller",
                "target_file": "src/helper.py", "target_symbol": "helper",
                "call_name": "helper", "line": 2,
                "resolution_type": "imported_function",
            }]
        })

        rescored = fault_localization._apply_called_helper_score(
            [caller, helper, decoy], graph,
        )
        by_name = {item.chunk.function_name: item for item in rescored}
        self.assertEqual(by_name["caller"].score, 0.5)
        self.assertEqual(by_name["decoy"].score, 0.3)
        self.assertAlmostEqual(by_name["helper"].score, 0.425)
        self.assertEqual(by_name["helper"].signals["called_by_symbol"], "caller")

    def test_source_neighbors_respect_file_window_and_nonoverlap(self) -> None:
        rows = [
            ("a.py", "anchor", 100, 110),
            ("b.py", "wrong_file", 111, 112),
            ("a.py", "containing", 1, 200),
            ("a.py", "too_far", 211, 212),
            ("a.py", "neighbor", 111, 112),
        ]
        candidates = [
            fault_localization.LocalizationCandidate(
                chunk=fault_localization.CodeChunk(
                    chunk_id=name, file_path=path, language="python",
                    symbol_kind="function", function_name=name, class_name="",
                    start_line=start, end_line=end, code_text=name,
                ),
                score=1.0 - rank / 100, embedding_score=1.0 - rank / 100,
                reason="test",
            )
            for rank, (path, name, start, end) in enumerate(rows)
        ]
        selected = fault_localization._select_coverage_aware_symbols(
            candidates, top_k=2, stage2_file_scores={"a.py": 1.0},
            mode="source-neighborhood-v1",
        )
        self.assertEqual([item.chunk.function_name for item in selected],
                         ["anchor", "neighbor"])

    def test_cli_symbol_flags_are_independent_and_legacy_alias_enables_both(self) -> None:
        parser = build_fault_localization_parser()
        localization_only = parser.parse_args(
            ["--ticket", "ticket.json", "--symbol-localization"]
        )
        llm_only = parser.parse_args(
            ["--ticket", "ticket.json", "--symbol-llm-rerank"]
        )
        legacy = parser.parse_args(["--ticket", "ticket.json", "--symbol-rerank"])

        self.assertEqual(resolve_symbol_modes(localization_only), (True, False))
        self.assertEqual(resolve_symbol_modes(llm_only), (True, True))
        self.assertEqual(resolve_symbol_modes(legacy), (True, True))

    def test_symbol_pipeline_records_client_unavailable_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            result = localize_ticket(
                {
                    "ticket_id": "SYMBOL-FALLBACK",
                    "title": "Login token validation fails",
                    "description": "The token validator rejects a valid login token.",
                },
                repo_path=repo,
                top_k=2,
                symbol_rerank=True,
                symbol_candidate_k=10,
                symbol_top_k=3,
            )

        self.assertTrue(result["stage3_candidate_symbols"])
        self.assertEqual(
            result["stage3_retrieval_symbols"],
            result["stage3_ranked_symbols"],
        )
        diagnostics = result["stage3_diagnostics"]
        self.assertTrue(diagnostics["eligible"])
        self.assertFalse(diagnostics["llm_attempted"])
        self.assertFalse(diagnostics["llm_valid"])
        self.assertTrue(diagnostics["fallback_used"])
        self.assertEqual(diagnostics["fallback_reason"], "llm_client_unavailable")

    def test_file_reranker_uses_json_schema_batches_of_at_most_five(self) -> None:
        client = SchemaAwareRerankClient()
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            for index in range(12):
                (repo / f"module_{index:02d}.py").write_text(
                    f"def handler_{index}():\n    return 'token validation {index}'\n",
                    encoding="utf-8",
                )
            result = localize_ticket(
                {
                    "ticket_id": "BATCHED-RERANK",
                    "title": "Token validation handler fails",
                    "description": "Find the token validation implementation.",
                },
                repo_path=repo,
                top_k=5,
                candidate_file_k=12,
                llm_client=client,
                llm_rerank=True,
                llm_candidate_k=12,
            )

        self.assertEqual(client.batch_sizes, [5, 5, 2])
        self.assertTrue(result["method"]["llm_rerank"])
        self.assertTrue(
            all(
                1 <= int(row["signals"]["llm_rerank_batch"]) <= 3
                for row in result["localized_candidates"]
            )
        )

    def test_evaluator_reads_integrated_stage2_and_stage3_fields(self) -> None:
        metrics = evaluate_records(
            [
                {
                    "ticket_id": "INTEGRATED",
                    "fixed_files": ["src/auth.py"],
                    "fixed_symbols": ["TokenValidator.validate"],
                }
            ],
            [
                {
                    "ticket_id": "INTEGRATED",
                    "stage1_candidate_files": [{"file_path": "src/auth.py"}],
                    "stage2_localized_files": [{"file_path": "src/auth.py"}],
                    "stage3_ranked_symbols": [
                        {"symbol_qualified_name": "TokenValidator.validate"}
                    ],
                }
            ],
        )

        self.assertEqual(metrics["file_top_1_accuracy"], 1.0)
        self.assertEqual(metrics["symbol_top_1_accuracy"], 1.0)

    def test_evaluator_treats_empty_symbol_mapping_as_missing_ground_truth(self) -> None:
        metrics = evaluate_records(
            [{"ticket_id": "NO-SYMBOL-GOLD", "fixed_files": ["src/auth.py"], "fixed_symbols": {}}],
            [
                {
                    "ticket_id": "NO-SYMBOL-GOLD",
                    "localized_files": [{"file_path": "src/auth.py"}],
                    "stage3_ranked_symbols": [
                        {"symbol_qualified_name": "TokenValidator.validate"}
                    ],
                }
            ],
        )

        self.assertEqual(metrics["rows_with_symbol_ground_truth"], 0)

    def test_bug_localizer_reuses_unchanged_repository_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            localizer = BugLocalizer(top_k=1)
            ticket = {"ticket_id": "CACHE", "title": "Login token fails"}

            localizer.localize(ticket, str(repo))
            first_index = localizer._cached_index
            localizer.localize(ticket, str(repo))

        self.assertIsNotNone(first_index)
        self.assertIs(first_index, localizer._cached_index)

    def test_localize_ticket_rejects_non_positive_top_k(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            index = build_code_index(repo)

            for top_k in (0, -1):
                with self.subTest(top_k=top_k):
                    with self.assertRaisesRegex(ValueError, "top_k"):
                        localize_ticket(
                            {"ticket_id": "RAW-3", "title": "Login fails"},
                            code_index=index,
                            top_k=top_k,
                        )

    def test_evaluate_records_reports_top_k_and_mrr(self) -> None:
        gold = [
            {"ticket_id": "A", "fixed_files": ["src/auth/validator.py"], "fixed_symbols": ["validate_token"]},
            {"ticket_id": "B", "fixed_files": ["src/profile/view.py"], "fixed_symbols": ["ProfileView.render"]},
        ]
        pred = [
            {
                "ticket_id": "A",
                "localized_candidates": [
                    {"file_path": "src/other.py", "function_name": "other"},
                    {"file_path": "src/auth/validator.py", "function_name": "validate_token"},
                ],
            },
            {
                "ticket_id": "B",
                "localized_candidates": [
                    {"file_path": "src/profile/view.py", "symbol_qualified_name": "ProfileView.render"},
                ],
            },
        ]

        metrics = evaluate_records(gold, pred)

        self.assertEqual(metrics["rows_with_file_ground_truth"], 2)
        self.assertEqual(metrics["rows_with_symbol_ground_truth"], 2)
        self.assertEqual(metrics["top_1_accuracy"], 0.5)
        self.assertEqual(metrics["top_3_accuracy"], 1.0)
        self.assertEqual(metrics["top_5_accuracy"], 1.0)
        self.assertEqual(metrics["mrr"], 0.75)
        self.assertEqual(metrics["symbol_top_1_accuracy"], 0.5)
        self.assertEqual(metrics["symbol_top_3_accuracy"], 1.0)
        self.assertEqual(metrics["symbol_mrr"], 0.75)

    def test_evaluate_records_counts_missing_predictions_as_misses(self) -> None:
        gold = [
            {"ticket_id": "A", "fixed_files": ["src/a.py"]},
            {"ticket_id": "B", "fixed_files": ["src/b.py"]},
        ]
        pred = [
            {
                "ticket_id": "B",
                "localized_candidates": [{"file_path": "src/b.py"}],
            }
        ]

        metrics = evaluate_records(gold, pred)

        self.assertEqual(metrics["rows"], 2)
        self.assertEqual(metrics["matched_prediction_rows"], 1)
        self.assertEqual(metrics["missing_prediction_rows"], 1)
        self.assertEqual(metrics["top_1_accuracy"], 0.5)

    def test_evaluate_records_reports_stage1_hit_recall_and_outcomes(self) -> None:
        gold = [
            {"ticket_id": "A", "fixed_files": ["src/a.py", "src/b.py"]},
            {"ticket_id": "B", "fixed_files": ["src/c.py"]},
            {"ticket_id": "C", "fixed_files": ["src/d.py"]},
        ]
        pred = [
            {
                "ticket_id": "A",
                "stage1_candidate_files": [
                    {"file_path": "src/a.py"},
                    {"file_path": "src/other.py"},
                ],
            },
            {
                "ticket_id": "B",
                "stage1_candidate_files": [{"file_path": "src/c.py"}],
            },
            {"ticket_id": "C", "stage1_candidate_files": [{"file_path": "src/other.py"}]},
        ]

        metrics = evaluate_records(gold, pred)

        self.assertEqual(metrics["candidate_hit_at_20"], 2 / 3)
        self.assertEqual(metrics["candidate_recall_at_20"], 0.5)
        self.assertEqual(metrics["average_candidate_count_at_20"], 4 / 3)
        self.assertEqual(
            metrics["candidate_outcomes_at_20"],
            {"full_recall_rows": 1, "partial_recall_rows": 1, "miss_rows": 1},
        )
        self.assertEqual(
            metrics["candidate_outcome_ticket_ids_at_20"],
            {"full_recall": ["B"], "partial_recall": ["A"], "miss": ["C"]},
        )

    def test_reachable_recall_excludes_files_absent_at_base_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo_cache = tmp_path / "repos"
            repo = repo_cache / "example__project"
            source = repo / "src"
            source.mkdir(parents=True)
            (source / "existing.py").write_text("value = 1\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Codex Test",
                    "-c",
                    "user.email=codex@example.invalid",
                    "commit",
                    "-qm",
                    "base",
                ],
                cwd=repo,
                check=True,
            )
            base_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            gold = [
                {
                    "ticket_id": "example__project-1",
                    "repo": "example/project",
                    "base_commit": base_commit,
                    "fixed_files": ["src/existing.py", "src/new_file.py"],
                }
            ]
            pred = [
                {
                    "ticket_id": "example__project-1",
                    "stage1_candidate_files": [{"file_path": "src/existing.py"}],
                }
            ]
            reachable = discover_base_commit_existing_gold_files(gold, repo_cache)
            metrics = evaluate_records(
                gold,
                pred,
                reachable_gold_files_by_ticket=reachable,
            )

        self.assertEqual(reachable["example__project-1"], ["src/existing.py"])
        self.assertEqual(metrics["candidate_recall_at_20"], 0.5)
        reachable_metrics = metrics["base_commit_reachable_evaluation"]
        self.assertEqual(reachable_metrics["candidate_recall_at_20"], 1.0)
        self.assertEqual(reachable_metrics["unreachable_gold_file_count"], 1)

    def test_prepare_gold_records_normalizes_files_and_symbols(self) -> None:
        rows = prepare_gold_records(
            [
                {
                    "ticket_id": "A",
                    "patch": {"modified_files": ["./src/auth/validator.py"]},
                    "bug_location": {"function": "AuthValidator.validate_token"},
                }
            ]
        )

        self.assertEqual(rows[0]["fixed_files"], ["src/auth/validator.py"])
        self.assertEqual(rows[0]["fixed_symbols"], ["AuthValidator.validate_token"])

    def test_swebench_lite_records_map_patch_to_fault_localization_gold(self) -> None:
        raw = {
            "repo": "example/project",
            "instance_id": "example__project-1",
            "base_commit": "abc123",
            "problem_statement": "Parser crashes on empty input\n\nDetails...",
            "patch": (
                "diff --git a/src/parser.py b/src/parser.py\n"
                "--- a/src/parser.py\n"
                "+++ b/src/parser.py\n"
                "@@ -1,2 +1,2 @@\n"
                "-bad\n"
                "+good\n"
            ),
            "FAIL_TO_PASS": '["tests/test_parser.py::test_empty"]',
            "PASS_TO_PASS": "[]",
        }

        record = normalize_record(raw, split="test")
        ticket = ticket_record(record)
        gold = gold_record(record)

        self.assertEqual(ticket["ticket_id"], "example__project-1")
        self.assertEqual(ticket["title"], "Parser crashes on empty input")
        self.assertEqual(ticket["repo"], "example/project")
        self.assertEqual(ticket["fail_to_pass"], ["tests/test_parser.py::test_empty"])
        self.assertEqual(gold["fixed_files"], ["src/parser.py"])
        self.assertEqual(gold["base_commit"], "abc123")

    def test_cli_cache_checkpoint_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = _make_repo(tmp_path)
            tickets_path = tmp_path / "tickets.jsonl"
            output_path = tmp_path / "predictions.jsonl"
            cache_dir = tmp_path / "cache"
            tickets = [
                {"ticket_id": "CLI-1", "title": "validate token fails", "description": "Token validation fails."},
                {"ticket_id": "CLI-2", "title": "profile rendering fails", "description": "Profile view cannot render."},
            ]
            tickets_path.write_text(
                "".join(json.dumps(ticket) + "\n" for ticket in tickets),
                encoding="utf-8",
            )
            command = [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "fault_localization.py"),
                "--tickets-jsonl",
                str(tickets_path),
                "--repo-path",
                str(repo),
                "--index-cache-dir",
                str(cache_dir),
                "--output",
                str(output_path),
                "--checkpoint-every",
                "1",
                "--symbol-localization",
                "--progress",
                "json",
            ]
            first = subprocess.run(command, check=True, capture_output=True, text=True)
            resumed = subprocess.run([*command, "--resume"], check=True, capture_output=True, text=True)
            output_rows = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(len(output_rows), 2)
        self.assertIn('"event": "checkpoint_written"', first.stderr)
        self.assertIn('"event": "resume_loaded"', resumed.stderr)
        self.assertIn('"event": "ticket_skipped"', resumed.stderr)
        self.assertTrue(all("stage1_candidate_files" in row for row in output_rows))
        self.assertTrue(all(row["stage3_candidate_symbols"] for row in output_rows))
        self.assertTrue(
            all(not row["stage3_diagnostics"]["requested"] for row in output_rows)
        )

    def test_swebench_runner_uses_isolated_commit_snapshot_and_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "source_repo"
            source = repo / "src"
            source.mkdir(parents=True)
            (source / "parser.py").write_text(
                "def parse_empty(value):\n"
                "    return value.strip()\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Codex Test",
                    "-c",
                    "user.email=codex@example.invalid",
                    "commit",
                    "-qm",
                    "base snapshot",
                ],
                cwd=repo,
                check=True,
            )
            base_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            tickets_path = tmp_path / "tickets.jsonl"
            gold_path = tmp_path / "gold.jsonl"
            tickets_path.write_text(
                json.dumps(
                    {
                        "ticket_id": "example__project-1",
                        "repo": "example/project",
                        "base_commit": base_commit,
                        "local_repo_path": str(repo),
                        "title": "Parser crashes on empty input",
                        "description": "parse_empty calls strip for an empty value.",
                        "logs": "src/parser.py:2: AttributeError",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            gold_path.write_text(
                json.dumps(
                    {
                        "ticket_id": "example__project-1",
                        "repo": "example/project",
                        "base_commit": base_commit,
                        "fixed_files": ["src/parser.py"],
                        "fixed_symbols": ["parse_empty"],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            config = BatchRunConfig(
                tickets_path=tickets_path,
                gold_path=gold_path,
                repo_cache_dir=tmp_path / "repos",
                snapshot_cache_dir=tmp_path / "snapshots",
                index_cache_dir=tmp_path / "indexes",
                predictions_output=tmp_path / "output" / "predictions.jsonl",
                metrics_output=tmp_path / "output" / "metrics.json",
                failures_output=tmp_path / "output" / "failures.jsonl",
                manifest_output=tmp_path / "output" / "manifest.json",
                checkpoint_every=1,
                progress="none",
                frozen_stage1_v11=True,
            )

            first = run_batch(config)
            config.resume = True
            resumed = run_batch(config)
            prediction = json.loads(config.predictions_output.read_text(encoding="utf-8").strip())
            source_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            config.candidate_file_k = 19
            with self.assertRaisesRegex(ValueError, "frozen stage1-v11"):
                run_batch(config)

        self.assertEqual(first["failures"], 0)
        self.assertEqual(first["metrics"]["file_top_1_accuracy"], 1.0)
        self.assertEqual(first["metrics"]["candidate_hit_at_20"], 1.0)
        self.assertEqual(first["metrics"]["candidate_recall_at_20"], 1.0)
        self.assertEqual(first["metrics"]["symbol_top_1_accuracy"], 1.0)
        self.assertEqual(resumed["processed_this_run"], 0)
        self.assertEqual(prediction["base_commit"], base_commit)
        self.assertEqual(prediction["benchmark_run"]["method"]["embedding_backend"], "tfidf")
        self.assertEqual(prediction["benchmark_run"]["method"]["candidate_file_k"], 20)
        self.assertEqual(prediction["benchmark_run"]["frozen_protocol"], "stage1-v11")
        self.assertEqual(prediction["stage1_candidate_files"][0]["file_path"], "src/parser.py")
        self.assertEqual(prediction["repository_path"], ".")
        self.assertEqual(source_head, base_commit)

    def test_compare_runs_reports_metric_deltas_and_denominator_warning(self) -> None:
        comparison = compare_runs(
            {
                "tfidf": {
                    "rows_with_file_ground_truth": 10,
                    "rows_with_symbol_ground_truth": 5,
                    "file_top_1_accuracy": 0.5,
                    "file_top_3_accuracy": 0.7,
                    "file_top_5_accuracy": 0.8,
                    "file_mrr": 0.6,
                },
                "sbert": {
                    "rows_with_file_ground_truth": 10,
                    "rows_with_symbol_ground_truth": 5,
                    "file_top_1_accuracy": 0.6,
                    "file_top_3_accuracy": 0.8,
                    "file_top_5_accuracy": 0.9,
                    "file_mrr": 0.7,
                },
                "llm": {
                    "rows_with_file_ground_truth": 9,
                    "rows_with_symbol_ground_truth": 5,
                    "file_top_1_accuracy": 0.7,
                    "run_diagnostics": {
                        "llm_rerank_requested": True,
                        "llm_rerank_used_predictions": 8,
                        "llm_rerank_fallback_predictions": 1,
                    },
                },
            },
            baseline="tfidf",
        )

        sbert = next(row for row in comparison["runs"] if row["label"] == "sbert")
        self.assertEqual(sbert["delta_vs_baseline"]["file_top_1_accuracy"], 0.1)
        self.assertTrue(any("different evaluation denominators" in warning for warning in comparison["warnings"]))
        self.assertTrue(any("fell back to retrieval" in warning for warning in comparison["warnings"]))


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    source = repo / "src" / "auth"
    source.mkdir(parents=True)
    (source / "validator.py").write_text(
        "def validate_token(token):\n"
        "    return token.strip()\n",
        encoding="utf-8",
    )
    profile = repo / "src" / "profile"
    profile.mkdir(parents=True)
    (profile / "view.py").write_text(
        "def render_profile(user):\n"
        "    return user.name\n",
        encoding="utf-8",
    )
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_validator.py").write_text(
        "def test_validate_token():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    return repo


if __name__ == "__main__":
    unittest.main()
