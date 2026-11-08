from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


from utils.symbol_gold import (  # noqa: E402
    PATCH_SYMBOL_PROVENANCE_SCHEMA_VERSION,
    SYMBOL_GOLD_RECORD_SCHEMA_VERSION,
    PatchSymbolProvenanceV1,
    SymbolGoldRecordV1,
    map_patch_hunks_to_symbol_gold,
    make_patch_sha256,
    parse_unified_diff,
)
from utils.symbol_localization import (  # noqa: E402
    SymbolParseResult,
    SymbolRecordV1,
    extract_python_symbols,
    extract_symbols_from_source,
)


REPO = "example/project"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
SYMBOL_ID = "sha256:" + "a" * 64
PATCH = (
    "diff --git a/src/service.py b/src/service.py\n"
    "--- a/src/service.py\n"
    "+++ b/src/service.py\n"
    "@@ -10,3 +10,3 @@\n"
    "-    return old_value\n"
    "+    return new_value\n"
)


def modified_provenance() -> PatchSymbolProvenanceV1:
    return PatchSymbolProvenanceV1(
        patch_sha256=make_patch_sha256(PATCH),
        change_type="modified",
        old_file_path="src/service.py",
        new_file_path="src/service.py",
        hunk_index=0,
        old_start_line=10,
        old_line_count=3,
        new_start_line=10,
        new_line_count=3,
        matched_old_lines=(11,),
        context_old_lines=(10, 12),
    )


class SymbolGoldSchemaTests(unittest.TestCase):
    def test_mapper_selects_innermost_symbols_and_preserves_multi_symbol_hunk(self) -> None:
        source = (
            "class Service:\n"
            "    def first(self):\n"
            "        return 1\n"
            "\n"
            "    def second(self):\n"
            "        return 2\n"
        )
        patch = (
            "diff --git a/src/service.py b/src/service.py\n"
            "--- a/src/service.py\n"
            "+++ b/src/service.py\n"
            "@@ -1,6 +1,6 @@\n"
            " class Service:\n"
            "     def first(self):\n"
            "-        return 1\n"
            "+        return 10\n"
            " \n"
            "     def second(self):\n"
            "-        return 2\n"
            "+        return 20\n"
        )
        parse_result = extract_python_symbols(
            source,
            repo=REPO,
            base_commit=COMMIT,
            file_path="src/service.py",
        )

        gold = map_patch_hunks_to_symbol_gold(
            ticket_id="ticket-multi",
            repo=REPO,
            base_commit=COMMIT,
            hunks=parse_unified_diff(patch),
            parse_results=(parse_result,),
        )

        self.assertEqual(
            [record.qualified_name for record in gold],
            ["Service.first", "Service.second"],
        )
        self.assertEqual(
            [record.provenance.matched_old_lines for record in gold],
            [(3,), (6,)],
        )
        self.assertTrue(
            all(record.mapping_method == "modified_line_containment" for record in gold)
        )
        self.assertNotIn("Service", {record.qualified_name for record in gold})

    def test_mapper_uses_insertion_anchor_and_module_fallback(self) -> None:
        source = "def run():\n    return 1\nVALUE = 1\n"
        patch = (
            "diff --git a/src/module.py b/src/module.py\n"
            "--- a/src/module.py\n"
            "+++ b/src/module.py\n"
            "@@ -1,2 +1,3 @@\n"
            " def run():\n"
            "+    log()\n"
            "     return 1\n"
            "@@ -3 +4 @@\n"
            "-VALUE = 1\n"
            "+VALUE = 2\n"
        )
        parse_result = extract_python_symbols(
            source,
            repo=REPO,
            base_commit=COMMIT,
            file_path="src/module.py",
        )

        gold = map_patch_hunks_to_symbol_gold(
            ticket_id="ticket-module",
            repo=REPO,
            base_commit=COMMIT,
            hunks=parse_unified_diff(patch),
            parse_results=(parse_result,),
        )

        by_name = {record.qualified_name: record for record in gold}
        self.assertEqual(set(by_name), {"run", "<module>"})
        self.assertEqual(by_name["run"].mapping_method, "insertion_anchor_containment")
        self.assertEqual(by_name["run"].provenance.insertion_anchor_line, 1)
        self.assertEqual(by_name["run"].mapping_confidence, 0.8)
        self.assertEqual(by_name["<module>"].mapping_method, "module_level")
        self.assertEqual(by_name["<module>"].provenance.matched_old_lines, (3,))

    def test_mapper_emits_explicit_exclusions_without_guessing(self) -> None:
        patch = (
            "diff --git a/src/new.py b/src/new.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/src/new.py\n"
            "@@ -0,0 +1 @@\n"
            "+VALUE = 1\n"
            "diff --git a/src/missing.py b/src/missing.py\n"
            "--- a/src/missing.py\n"
            "+++ b/src/missing.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
            "diff --git a/src/broken.py b/src/broken.py\n"
            "--- a/src/broken.py\n"
            "+++ b/src/broken.py\n"
            "@@ -1 +1 @@\n"
            "-def broken(:\n"
            "+def fixed():\n"
            "diff --git a/src/client.js b/src/client.js\n"
            "--- a/src/client.js\n"
            "+++ b/src/client.js\n"
            "@@ -1 +1 @@\n"
            "-old();\n"
            "+newCall();\n"
        )
        broken = extract_python_symbols(
            "def broken(:\n",
            repo=REPO,
            base_commit=COMMIT,
            file_path="src/broken.py",
        )
        unsupported = extract_symbols_from_source(
            "old();\n",
            repo=REPO,
            base_commit=COMMIT,
            file_path="src/client.js",
            language="javascript",
        )

        gold = map_patch_hunks_to_symbol_gold(
            ticket_id="ticket-exclusions",
            repo=REPO,
            base_commit=COMMIT,
            hunks=parse_unified_diff(patch),
            parse_results=(broken, unsupported),
        )

        self.assertEqual(
            {record.exclusion_reason for record in gold},
            {"new_file", "missing_base_file", "parser_failure", "unsupported_language"},
        )
        self.assertTrue(all(record.mapping_status == "excluded" for record in gold))
        self.assertTrue(all(record.symbol_id == "" for record in gold))

    def test_mapper_excludes_equal_span_ambiguity(self) -> None:
        first = SymbolRecordV1.create(
            repo=REPO,
            base_commit=COMMIT,
            file_path="src/ambiguous.py",
            language="python",
            symbol_kind="function",
            qualified_name="first",
            display_name="first",
            parent_symbol_id="",
            start_line=1,
            start_column=0,
            end_line=2,
            end_column=8,
            signature="def first():",
        )
        second = SymbolRecordV1.create(
            repo=REPO,
            base_commit=COMMIT,
            file_path="src/ambiguous.py",
            language="python",
            symbol_kind="function",
            qualified_name="second",
            display_name="second",
            parent_symbol_id="",
            start_line=1,
            start_column=0,
            end_line=2,
            end_column=8,
            signature="def second():",
        )
        parse_result = SymbolParseResult(
            repo=REPO,
            base_commit=COMMIT,
            file_path="src/ambiguous.py",
            language="python",
            status="ok",
            symbols=(first, second),
        )
        patch = (
            "diff --git a/src/ambiguous.py b/src/ambiguous.py\n"
            "--- a/src/ambiguous.py\n"
            "+++ b/src/ambiguous.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def first():\n"
            "-    old\n"
            "+    new\n"
        )

        gold = map_patch_hunks_to_symbol_gold(
            ticket_id="ticket-ambiguous",
            repo=REPO,
            base_commit=COMMIT,
            hunks=parse_unified_diff(patch),
            parse_results=(parse_result,),
        )

        self.assertEqual(len(gold), 1)
        self.assertEqual(gold[0].mapping_status, "excluded")
        self.assertEqual(gold[0].exclusion_reason, "ambiguous_mapping")
        self.assertEqual(gold[0].provenance.matched_old_lines, (2,))

    def test_parser_extracts_all_change_types_and_old_line_evidence(self) -> None:
        patch = (
            "diff --git a/src/service.py b/src/service.py\n"
            "--- a/src/service.py\n"
            "+++ b/src/service.py\n"
            "@@ -10,3 +10,4 @@ def run\n"
            " context_before\n"
            "-old_value\n"
            "+new_value\n"
            "+extra_value\n"
            " context_after\n"
            "diff --git a/src/legacy.py b/src/legacy.py\n"
            "deleted file mode 100644\n"
            "--- a/src/legacy.py\n"
            "+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n"
            "-first\n"
            "-second\n"
            "diff --git a/src/new.py b/src/new.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/src/new.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+first\n"
            "+second\n"
            'diff --git "a/src/old name.py" "b/src/new name.py"\n'
            "similarity index 100%\n"
            "rename from src/old name.py\n"
            "rename to src/new name.py\n"
        )

        hunks = parse_unified_diff(patch)

        self.assertEqual([hunk.change_type for hunk in hunks], [
            "modified",
            "deleted",
            "added",
            "renamed",
        ])
        modified, deleted, added, renamed = hunks
        self.assertEqual(modified.deleted_old_lines, (11,))
        self.assertEqual(modified.context_old_lines, (10, 12))
        self.assertEqual(modified.added_new_lines, (11, 12))
        self.assertEqual(modified.section_header, "def run")
        self.assertEqual(modified.to_provenance().matched_old_lines, (11,))
        self.assertEqual(deleted.old_file_path, "src/legacy.py")
        self.assertEqual(deleted.new_file_path, "")
        self.assertEqual(deleted.deleted_old_lines, (1, 2))
        self.assertEqual(added.old_file_path, "")
        self.assertEqual(added.new_file_path, "src/new.py")
        self.assertEqual(added.added_new_lines, (1, 2))
        self.assertEqual(added.insertion_anchor_lines, (0,))
        self.assertTrue(renamed.is_metadata_only)
        self.assertEqual(renamed.old_file_path, "src/old name.py")
        self.assertEqual(renamed.new_file_path, "src/new name.py")

    def test_parser_tracks_multiple_insertion_anchors(self) -> None:
        patch = (
            "diff --git a/src/service.py b/src/service.py\n"
            "--- a/src/service.py\n"
            "+++ b/src/service.py\n"
            "@@ -5,2 +5,4 @@\n"
            " line_five\n"
            "+after_five\n"
            " line_six\n"
            "+after_six\n"
        )

        hunk = parse_unified_diff(patch)[0]

        self.assertEqual(hunk.context_old_lines, (5, 6))
        self.assertEqual(hunk.added_new_lines, (6, 8))
        self.assertEqual(hunk.insertion_anchor_lines, (5, 6))
        self.assertEqual(
            hunk.to_provenance(insertion_anchor_line=6).insertion_anchor_line,
            6,
        )

    def test_parser_rejects_malformed_counts_and_unsafe_paths(self) -> None:
        malformed = (
            "diff --git a/src/a.py b/src/a.py\n"
            "--- a/src/a.py\n"
            "+++ b/src/a.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-old\n"
            "+new\n"
        )
        with self.assertRaisesRegex(ValueError, "line counts do not match"):
            parse_unified_diff(malformed)

        unsafe = (
            "diff --git a/../escape.py b/../escape.py\n"
            "--- a/../escape.py\n"
            "+++ b/../escape.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        with self.assertRaisesRegex(ValueError, "parent traversal"):
            parse_unified_diff(unsafe)

    def test_mapped_record_round_trip_has_complete_provenance_and_stable_id(self) -> None:
        provenance = modified_provenance()
        record = SymbolGoldRecordV1.create_mapped(
            ticket_id="ticket-1",
            repo=REPO,
            base_commit=COMMIT,
            file_path="src/service.py",
            mapping_method="modified_line_containment",
            mapping_confidence=1.0,
            symbol_id=SYMBOL_ID,
            qualified_name="Service.run",
            symbol_kind="method",
            start_line=8,
            start_column=4,
            end_line=13,
            end_column=20,
            provenance=provenance,
        )
        repeated = SymbolGoldRecordV1.create_mapped(
            ticket_id="ticket-1",
            repo=REPO,
            base_commit=COMMIT,
            file_path=r"src\service.py",
            mapping_method="modified_line_containment",
            mapping_confidence=0.95,
            symbol_id=SYMBOL_ID,
            qualified_name="Service.run",
            symbol_kind="method",
            start_line=8,
            start_column=4,
            end_line=13,
            end_column=20,
            provenance=provenance,
        )
        payload = record.to_dict()
        restored = SymbolGoldRecordV1.from_dict(payload)

        self.assertEqual(restored, record)
        self.assertEqual(repeated.gold_id, record.gold_id)
        self.assertEqual(payload["schema_version"], SYMBOL_GOLD_RECORD_SCHEMA_VERSION)
        self.assertEqual(
            payload["provenance"]["schema_version"],
            PATCH_SYMBOL_PROVENANCE_SCHEMA_VERSION,
        )
        self.assertEqual(payload["provenance"]["source"], "developer_patch")
        self.assertEqual(payload["provenance"]["matched_old_lines"], [11])
        self.assertTrue(record.gold_id.startswith("sha256:"))

    def test_new_file_is_explicitly_excluded_without_guessed_symbol(self) -> None:
        provenance = PatchSymbolProvenanceV1(
            patch_sha256=make_patch_sha256("new file patch"),
            change_type="added",
            old_file_path="",
            new_file_path="src/new_service.py",
            hunk_index=0,
            old_start_line=0,
            old_line_count=0,
            new_start_line=1,
            new_line_count=4,
            insertion_anchor_line=0,
        )
        record = SymbolGoldRecordV1.create_excluded(
            ticket_id="ticket-2",
            repo=REPO,
            base_commit=COMMIT,
            file_path="src/new_service.py",
            exclusion_reason="new_file",
            provenance=provenance,
        )

        self.assertEqual(record.mapping_status, "excluded")
        self.assertEqual(record.mapping_method, "unmapped")
        self.assertEqual(record.mapping_confidence, 0.0)
        self.assertEqual(record.symbol_id, "")
        self.assertIsNone(record.start_line)
        self.assertEqual(SymbolGoldRecordV1.from_dict(record.to_dict()), record)

        with self.assertRaisesRegex(ValueError, "New files cannot be mapped"):
            SymbolGoldRecordV1.create_mapped(
                ticket_id="ticket-2",
                repo=REPO,
                base_commit=COMMIT,
                file_path="src/new_service.py",
                mapping_method="module_level",
                mapping_confidence=0.5,
                symbol_id=SYMBOL_ID,
                qualified_name="<module>",
                symbol_kind="module",
                start_line=1,
                start_column=0,
                end_line=4,
                end_column=0,
                provenance=provenance,
            )

    def test_schema_rejects_unsafe_incomplete_or_tampered_records(self) -> None:
        with self.assertRaisesRegex(ValueError, "parent traversal"):
            PatchSymbolProvenanceV1(
                patch_sha256=make_patch_sha256(PATCH),
                change_type="modified",
                old_file_path="../service.py",
                new_file_path="../service.py",
                hunk_index=0,
                old_start_line=1,
                old_line_count=1,
                new_start_line=1,
                new_line_count=1,
            )
        with self.assertRaisesRegex(ValueError, "inside the old hunk range"):
            PatchSymbolProvenanceV1(
                patch_sha256=make_patch_sha256(PATCH),
                change_type="modified",
                old_file_path="src/service.py",
                new_file_path="src/service.py",
                hunk_index=0,
                old_start_line=10,
                old_line_count=2,
                new_start_line=10,
                new_line_count=2,
                matched_old_lines=(99,),
            )

        record = SymbolGoldRecordV1.create_mapped(
            ticket_id="ticket-1",
            repo=REPO,
            base_commit=COMMIT,
            file_path="src/service.py",
            mapping_method="modified_line_containment",
            mapping_confidence=1.0,
            symbol_id=SYMBOL_ID,
            qualified_name="Service.run",
            symbol_kind="method",
            start_line=8,
            start_column=4,
            end_line=13,
            end_column=20,
            provenance=modified_provenance(),
        )
        tampered = record.to_dict()
        tampered["qualified_name"] = "Other.run"
        with self.assertRaisesRegex(ValueError, "gold_id does not match"):
            SymbolGoldRecordV1.from_dict(tampered)
        incomplete = record.to_dict()
        del incomplete["provenance"]["hunk_index"]
        with self.assertRaisesRegex(ValueError, "missing required fields: hunk_index"):
            SymbolGoldRecordV1.from_dict(incomplete)
        with self.assertRaisesRegex(ValueError, "finite"):
            SymbolGoldRecordV1.create_mapped(
                ticket_id="ticket-1",
                repo=REPO,
                base_commit=COMMIT,
                file_path="src/service.py",
                mapping_method="modified_line_containment",
                mapping_confidence=math.nan,
                symbol_id=SYMBOL_ID,
                qualified_name="Service.run",
                symbol_kind="method",
                start_line=8,
                start_column=4,
                end_line=13,
                end_column=20,
                provenance=modified_provenance(),
            )


if __name__ == "__main__":
    unittest.main()
