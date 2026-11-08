from __future__ import annotations

import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


from utils.symbol_localization import (  # noqa: E402
    SYMBOL_PARSE_RESULT_SCHEMA_VERSION,
    SYMBOL_RECORD_SCHEMA_VERSION,
    SymbolRecordV1,
    extract_python_symbols,
    extract_symbols_from_repository_file,
    extract_symbols_from_source,
)
import utils.fault_localization as legacy_fault_localization  # noqa: E402


REPO = "example/project"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
FILE = "src/example.py"


class SymbolLocalizationTests(unittest.TestCase):
    def test_legacy_range_wrapper_delegates_and_preserves_contract(self) -> None:
        source = (
            "class Outer:\n"
            "    def method(self):\n"
            "        def helper():\n"
            "            return 1\n"
            "        return helper()\n"
        )
        with patch.object(
            legacy_fault_localization,
            "extract_python_symbols",
            wraps=extract_python_symbols,
        ) as extractor:
            rows = legacy_fault_localization._python_symbol_ranges(source)

        extractor.assert_called_once()
        self.assertTrue(
            all(
                set(row)
                == {
                    "symbol_kind",
                    "function_name",
                    "class_name",
                    "start_line",
                    "end_line",
                    "is_top_level",
                }
                for row in rows
            )
        )
        by_name = {
            row["function_name"] or row["class_name"]: row
            for row in rows
        }
        self.assertEqual(by_name["Outer.method"]["symbol_kind"], "method")
        self.assertEqual(by_name["Outer.method"]["class_name"], "Outer")
        self.assertEqual(
            by_name["Outer.method.<locals>.helper"]["symbol_kind"],
            "function",
        )
        self.assertEqual(
            by_name["Outer.method.<locals>.helper"]["class_name"],
            "",
        )
        self.assertFalse(by_name["Outer.method.<locals>.helper"]["is_top_level"])
        self.assertEqual(
            legacy_fault_localization._python_symbol_ranges("def broken(:\n"),
            [],
        )

    def test_code_index_uses_new_qualified_names_through_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / "nested.py").write_text(
                "class Outer:\n"
                "    class Inner:\n"
                "        async def run(self):\n"
                "            return 1\n"
                "\n"
                "    def method(self):\n"
                "        def helper():\n"
                "            return 2\n"
                "        return helper()\n",
                encoding="utf-8",
            )
            index = legacy_fault_localization.build_code_index(repo)

        by_name = {
            chunk.symbol_qualified_name: chunk
            for chunk in index.chunks
            if chunk.symbol_qualified_name
        }
        self.assertIn("Outer.Inner", by_name)
        self.assertIn("Outer.Inner.run", by_name)
        self.assertIn("Outer.method", by_name)
        self.assertIn("Outer.method.<locals>.helper", by_name)
        self.assertEqual(by_name["Outer.Inner.run"].symbol_kind, "async_method")
        self.assertEqual(
            by_name["Outer.method.<locals>.helper"].symbol_kind,
            "function",
        )

    def test_old_code_index_version_does_not_reuse_legacy_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / "module.py").write_text(
                "class Service:\n"
                "    def run(self):\n"
                "        return 1\n",
                encoding="utf-8",
            )
            previous = legacy_fault_localization.build_code_index(repo)
            previous.version = legacy_fault_localization.CODE_INDEX_VERSION - 1
            rebuilt = legacy_fault_localization.build_code_index(
                repo,
                previous_index=previous,
            )

        stats = rebuilt.settings["index_stats"]
        self.assertEqual(rebuilt.version, legacy_fault_localization.CODE_INDEX_VERSION)
        self.assertEqual(stats["reused_files"], 0)
        self.assertEqual(stats["rebuilt_files"], 1)

    def test_code_index_serializes_symbol_records_and_parse_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "valid.py").write_text(
                "class Service:\n"
                "    def run(self):\n"
                "        return 1\n",
                encoding="utf-8",
            )
            (repo / "broken.py").write_text("def broken(:\n", encoding="utf-8")
            (repo / "constants.py").write_text("VALUE = 1\n", encoding="utf-8")
            (repo / "frontend.js").write_text(
                "function render() { return 1; }\n",
                encoding="utf-8",
            )

            index = legacy_fault_localization.build_code_index(
                repo,
                repository_name=REPO,
                base_commit=COMMIT,
            )
            serialized = index.to_dict()
            index_path = root / "code-index.json"
            index.save(index_path)
            restored = legacy_fault_localization.load_code_index(index_path)

        statuses = {
            result.file_path: result.status
            for result in index.symbol_parse_results
        }
        self.assertEqual(
            statuses,
            {
                "broken.py": "syntax_error",
                "constants.py": "no_symbols",
                "frontend.js": "unsupported_language",
                "valid.py": "ok",
            },
        )
        self.assertEqual(index.settings["repository_name"], REPO)
        self.assertEqual(index.settings["base_commit"], COMMIT)
        self.assertEqual(
            index.settings["symbol_record_schema_version"],
            SYMBOL_RECORD_SCHEMA_VERSION,
        )
        self.assertEqual(
            index.settings["symbol_parse_result_schema_version"],
            SYMBOL_PARSE_RESULT_SCHEMA_VERSION,
        )
        self.assertEqual(
            index.settings["index_stats"]["symbol_parse_statuses"],
            {
                "no_symbols": 1,
                "ok": 1,
                "syntax_error": 1,
                "unsupported_language": 1,
            },
        )
        self.assertEqual(
            {record.qualified_name for record in index.symbol_records},
            {"Service", "Service.run"},
        )
        self.assertTrue(
            all("symbols" not in row for row in serialized["symbol_parse_diagnostics"])
        )
        self.assertEqual(restored.symbol_records, index.symbol_records)
        self.assertEqual(restored.symbol_parse_results, index.symbol_parse_results)

    def test_incremental_index_reuses_symbols_only_for_same_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / "module.py").write_text(
                "def run():\n"
                "    return 1\n",
                encoding="utf-8",
            )
            first = legacy_fault_localization.build_code_index(
                repo,
                repository_name=REPO,
                base_commit=COMMIT,
            )
            reused = legacy_fault_localization.build_code_index(
                repo,
                repository_name=REPO,
                base_commit=COMMIT,
                previous_index=first,
            )
            other_snapshot = legacy_fault_localization.build_code_index(
                repo,
                repository_name=REPO,
                base_commit="f" * 40,
                previous_index=first,
            )

        self.assertEqual(reused.settings["index_stats"]["reused_files"], 1)
        self.assertEqual(reused.symbol_records, first.symbol_records)
        self.assertEqual(reused.symbol_parse_results, first.symbol_parse_results)
        self.assertEqual(other_snapshot.settings["index_stats"]["reused_files"], 0)
        self.assertNotEqual(
            other_snapshot.symbol_records[0].symbol_id,
            first.symbol_records[0].symbol_id,
        )

    def test_repository_file_extractor_returns_structured_path_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            invalid = extract_symbols_from_repository_file(
                repo,
                "../outside.py",
                repo=REPO,
                base_commit=COMMIT,
            )
            missing = extract_symbols_from_repository_file(
                repo,
                "src/missing.py",
                repo=REPO,
                base_commit=COMMIT,
            )

        self.assertEqual(invalid.status, "invalid_path")
        self.assertEqual(invalid.file_path, "")
        self.assertEqual(invalid.error_type, "InvalidRepositoryPath")
        self.assertNotIn("../outside.py", str(invalid.to_dict()))
        self.assertEqual(missing.status, "missing_file")
        self.assertEqual(missing.file_path, "src/missing.py")
        self.assertEqual(missing.error_type, "FileNotFoundError")

    def test_code_index_serializes_symlink_escape_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            candidate = repo / "escape.py"
            candidate.write_text("def unsafe():\n    return 1\n", encoding="utf-8")
            outside = root / "outside.py"
            outside.write_text("def outside():\n    return 2\n", encoding="utf-8")
            original_resolve = Path.resolve

            def resolve_with_escape(path: Path, strict: bool = False) -> Path:
                if path == candidate:
                    return outside
                return original_resolve(path, strict=strict)

            with patch.object(Path, "resolve", resolve_with_escape):
                direct = extract_symbols_from_repository_file(
                    repo,
                    "escape.py",
                    repo=REPO,
                    base_commit=COMMIT,
                )
                index = legacy_fault_localization.build_code_index(
                    repo,
                    repository_name=REPO,
                    base_commit=COMMIT,
                )
                restored = legacy_fault_localization.CodeIndex.from_dict(
                    index.to_dict()
                )

        self.assertEqual(direct.status, "symlink_escape")
        self.assertEqual(direct.error_type, "RepositoryBoundaryError")
        self.assertEqual(index.chunks, [])
        self.assertEqual(index.symbol_records, [])
        self.assertEqual(len(index.symbol_parse_results), 1)
        self.assertEqual(index.symbol_parse_results[0].status, "symlink_escape")
        self.assertEqual(restored.symbol_parse_results, index.symbol_parse_results)

    def test_extracts_complete_qualified_names_and_parent_ids(self) -> None:
        result = extract_python_symbols(
            """
class Outer:
    class Inner:
        async def run(self):
            return 1

    def method(self):
        def helper():
            return 2

        class Local:
            def execute(self):
                return helper()

        return Local().execute()

def top_level():
    return 3

async def fetch():
    return 4
""".lstrip(),
            repo=REPO,
            base_commit=COMMIT,
            file_path=FILE,
            source_file_rank=2,
            source_file_score=0.73,
        )

        self.assertEqual(result.status, "ok")
        by_name = {symbol.qualified_name: symbol for symbol in result.symbols}
        self.assertEqual(
            set(by_name),
            {
                "Outer",
                "Outer.Inner",
                "Outer.Inner.run",
                "Outer.method",
                "Outer.method.<locals>.helper",
                "Outer.method.<locals>.Local",
                "Outer.method.<locals>.Local.execute",
                "top_level",
                "fetch",
            },
        )
        self.assertEqual(by_name["Outer"].symbol_kind, "class")
        self.assertEqual(by_name["Outer.Inner.run"].symbol_kind, "async_method")
        self.assertEqual(by_name["Outer.method"].symbol_kind, "method")
        self.assertEqual(
            by_name["Outer.method.<locals>.helper"].symbol_kind,
            "function",
        )
        self.assertEqual(
            by_name["Outer.method.<locals>.Local.execute"].symbol_kind,
            "method",
        )
        self.assertEqual(by_name["top_level"].symbol_kind, "function")
        self.assertEqual(by_name["fetch"].symbol_kind, "async_function")
        self.assertEqual(
            by_name["Outer.Inner"].parent_symbol_id,
            by_name["Outer"].symbol_id,
        )
        self.assertEqual(
            by_name["Outer.method.<locals>.helper"].parent_symbol_id,
            by_name["Outer.method"].symbol_id,
        )
        self.assertEqual(
            by_name["Outer.method.<locals>.Local.execute"].parent_symbol_id,
            by_name["Outer.method.<locals>.Local"].symbol_id,
        )
        self.assertEqual(by_name["Outer.method"].source_file_rank, 2)
        self.assertEqual(by_name["Outer.method"].source_file_score, 0.73)

    def test_stable_ids_repeat_and_change_with_snapshot_identity(self) -> None:
        source = "def handler(value):\n    return value\n"
        first = extract_python_symbols(
            source,
            repo=REPO,
            base_commit=COMMIT,
            file_path=FILE,
        ).symbols[0]
        repeated = extract_python_symbols(
            source,
            repo=REPO,
            base_commit=COMMIT,
            file_path=r"src\example.py",
        ).symbols[0]
        other_commit = extract_python_symbols(
            source,
            repo=REPO,
            base_commit="f" * 40,
            file_path=FILE,
        ).symbols[0]
        moved_symbol = extract_python_symbols(
            "\ndef handler(value):\n    return value\n",
            repo=REPO,
            base_commit=COMMIT,
            file_path=FILE,
        ).symbols[0]

        self.assertEqual(first.symbol_id, repeated.symbol_id)
        self.assertNotEqual(first.symbol_id, other_commit.symbol_id)
        self.assertNotEqual(first.symbol_id, moved_symbol.symbol_id)
        self.assertTrue(first.symbol_id.startswith("sha256:"))
        self.assertEqual(len(first.symbol_id), 71)

    def test_decorator_range_and_serialized_schema_are_explicit(self) -> None:
        result = extract_python_symbols(
            "@register\ndef parse(value):\n    return value\n",
            repo=REPO,
            base_commit=COMMIT,
            file_path=FILE,
            ast_input_source="stage2_retrieval_baseline",
        )

        symbol = result.symbols[0]
        row = symbol.to_dict()
        payload = result.to_dict()
        self.assertEqual(symbol.start_line, 1)
        self.assertEqual(symbol.start_column, 0)
        self.assertEqual(symbol.signature, "def parse(value):")
        self.assertEqual(row["schema_version"], SYMBOL_RECORD_SCHEMA_VERSION)
        self.assertEqual(row["file_path"], FILE)
        self.assertEqual(row["parse_status"], "ok")
        self.assertEqual(payload["schema_version"], SYMBOL_PARSE_RESULT_SCHEMA_VERSION)
        self.assertEqual(payload["symbol_count"], 1)
        self.assertTrue(result.is_success)

    def test_syntax_error_is_not_reported_as_no_symbols(self) -> None:
        result = extract_python_symbols(
            "def broken(:\n    pass\n",
            repo=REPO,
            base_commit=COMMIT,
            file_path=FILE,
        )

        self.assertEqual(result.status, "syntax_error")
        self.assertEqual(result.symbols, ())
        self.assertEqual(result.error_type, "SyntaxError")
        self.assertEqual(result.error_line, 1)
        self.assertIsNotNone(result.error_column)
        self.assertFalse(result.is_success)

    def test_recursion_error_is_reported_as_parser_error(self) -> None:
        with patch(
            "utils.symbol_localization.ast.parse",
            side_effect=RecursionError("maximum recursion depth exceeded"),
        ):
            result = extract_python_symbols(
                "def parse():\n    pass\n",
                repo=REPO,
                base_commit=COMMIT,
                file_path=FILE,
            )

        self.assertEqual(result.status, "parser_error")
        self.assertEqual(result.error_type, "RecursionError")
        self.assertIn("maximum recursion depth", result.message)
        self.assertEqual(result.symbols, ())
        self.assertFalse(result.is_success)

    def test_multiline_signature_preserves_complete_header(self) -> None:
        source = (
            "def parse(\n"
            "    value: int,\n"
            "    transform=lambda item: item,\n"
            "    *,\n"
            "    enabled: bool = True,\n"
            ") -> str:\n"
            "    return str(transform(value)) if enabled else \"\"\n"
        )
        result = extract_python_symbols(
            source,
            repo=REPO,
            base_commit=COMMIT,
            file_path=FILE,
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(
            result.symbols[0].signature,
            "def parse(\n"
            "    value: int,\n"
            "    transform=lambda item: item,\n"
            "    *,\n"
            "    enabled: bool = True,\n"
            ") -> str:",
        )

    def test_no_symbols_and_unsupported_language_are_distinct(self) -> None:
        empty = extract_python_symbols(
            "VALUE = 1\n",
            repo=REPO,
            base_commit=COMMIT,
            file_path=FILE,
        )
        unsupported = extract_symbols_from_source(
            "function parse() {}\n",
            repo=REPO,
            base_commit=COMMIT,
            file_path="src/example.js",
            language="javascript",
        )

        self.assertEqual(empty.status, "no_symbols")
        self.assertTrue(empty.is_success)
        self.assertEqual(unsupported.status, "unsupported_language")
        self.assertFalse(unsupported.is_success)
        self.assertIn("javascript", unsupported.message)

    def test_schema_boundary_rejects_unsafe_or_nonfinite_inputs(self) -> None:
        with self.assertRaisesRegex(ValueError, "parent traversal"):
            extract_python_symbols(
                "def parse():\n    pass\n",
                repo=REPO,
                base_commit=COMMIT,
                file_path="../escape.py",
            )
        with self.assertRaisesRegex(ValueError, "repository-relative"):
            extract_python_symbols(
                "def parse():\n    pass\n",
                repo=REPO,
                base_commit=COMMIT,
                file_path=r"C:\outside\escape.py",
            )
        with self.assertRaisesRegex(ValueError, "finite"):
            SymbolRecordV1.create(
                repo=REPO,
                base_commit=COMMIT,
                file_path=FILE,
                language="python",
                symbol_kind="function",
                qualified_name="parse",
                display_name="parse",
                parent_symbol_id="",
                start_line=1,
                start_column=0,
                end_line=2,
                end_column=8,
                signature="def parse():",
                source_file_score=math.nan,
            )


if __name__ == "__main__":
    unittest.main()
