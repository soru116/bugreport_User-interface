from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from scripts.verify_symbol_gold_audit_source import (  # noqa: E402
    extract_independent_symbols,
    find_hunk_text,
    innermost_symbol,
)


class VerifySymbolGoldAuditSourceTests(unittest.TestCase):
    def test_independent_ast_tracks_nested_qualified_names_and_kinds(self) -> None:
        source = (
            "class Outer:\n"
            "    def method(self):\n"
            "        def inner():\n"
            "            return 1\n"
            "        return inner()\n"
        )
        symbols = extract_independent_symbols(source)
        by_name = {symbol.qualified_name: symbol for symbol in symbols}
        self.assertEqual(by_name["Outer"].symbol_kind, "class")
        self.assertEqual(by_name["Outer.method"].symbol_kind, "method")
        self.assertEqual(by_name["Outer.method.<locals>.inner"].symbol_kind, "function")
        self.assertEqual(
            innermost_symbol(symbols, 4).qualified_name,
            "Outer.method.<locals>.inner",
        )

    def test_hunk_lookup_is_file_and_index_qualified(self) -> None:
        patch = (
            "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
            "@@ -1 +1 @@\n-old\n+new\n"
            "@@ -3 +3 @@\n-old2\n+new2\n"
            "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n"
            "@@ -1 +1 @@\n-x\n+y\n"
        )
        self.assertIn("@@ -3 +3 @@", find_hunk_text(patch, old_file_path="a.py", hunk_index=1))
        self.assertEqual(find_hunk_text(patch, old_file_path="missing.py", hunk_index=0), "")


if __name__ == "__main__":
    unittest.main()
