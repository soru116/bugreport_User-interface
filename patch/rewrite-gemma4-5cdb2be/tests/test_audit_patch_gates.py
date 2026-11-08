"""Auditing an already-generated patch against the gate chain."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import audit_patch_gates as audit  # noqa: E402

BEFORE = '''\
import re


class Differ:
    def __init__(self, a, b, rtol=0.0, atol=0.0):
        """Compare two things."""
        self.a = a
        self.b = b
        self.rtol = rtol
        self.atol = atol

    def close(self, x, y):
        return abs(x - y) <= self.atol + self.rtol * abs(y)


def classify(line):
    pattern = re.compile(r"READ")
    if pattern.match(line):
        return "command"
    return "data"
'''


def diff_for(before: str, after: str, path: str = "pkg/d.py") -> str:
    import difflib
    lines = list(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}", n=3))
    return "".join(lines)


class OrphanedAttributeTests(unittest.TestCase):
    """The 13465 failure, generalized: a write removed while reads remain."""

    def test_dropping_an_assignment_the_file_still_reads_is_reported(self):
        after = BEFORE.replace("        self.rtol = rtol\n        self.atol = atol\n", "")
        self.assertEqual(
            audit.orphaned_attributes(BEFORE, after, "Differ.__init__"),
            ["atol", "rtol"])

    def test_dropping_an_assignment_nothing_reads_is_not_reported(self):
        before = BEFORE.replace("    def close", "    def unused_close")
        after = before.replace("        self.rtol = rtol\n        self.atol = atol\n", "")
        after = after.replace(
            "        return abs(x - y) <= self.atol + self.rtol * abs(y)\n",
            "        return x == y\n")
        self.assertEqual(audit.orphaned_attributes(before, after, "Differ.__init__"), [])

    def test_keeping_every_assignment_reports_nothing(self):
        after = BEFORE.replace('"""Compare two things."""', '"""Compare two objects."""')
        self.assertEqual(audit.orphaned_attributes(BEFORE, after, "Differ.__init__"), [])

    def test_an_augmented_assignment_counts_as_a_write(self):
        before = BEFORE.replace("        self.rtol = rtol\n",
                                "        self.rtol = rtol\n        self.rtol += 1\n")
        after = before.replace("        self.rtol += 1\n", "")
        self.assertEqual(audit.orphaned_attributes(before, after, "Differ.__init__"), [])

    def test_a_write_that_only_moved_is_not_an_orphan(self):
        after = BEFORE.replace(
            "        self.rtol = rtol\n        self.atol = atol\n",
            "        self.atol = atol\n        self.rtol = rtol\n")
        self.assertEqual(audit.orphaned_attributes(BEFORE, after, "Differ.__init__"), [])


class SignatureTests(unittest.TestCase):
    def test_a_removed_parameter_is_a_signature_change(self):
        after = BEFORE.replace("def __init__(self, a, b, rtol=0.0, atol=0.0):",
                               "def __init__(self, a, b):")
        self.assertTrue(audit.signature_changed(BEFORE, after, "Differ.__init__"))

    def test_a_changed_default_is_a_signature_change(self):
        after = BEFORE.replace("rtol=0.0", "rtol=1e-5")
        self.assertTrue(audit.signature_changed(BEFORE, after, "Differ.__init__"))

    def test_a_body_only_edit_is_not_a_signature_change(self):
        after = BEFORE.replace('        self.a = a\n', '        self.a = a or 0\n')
        self.assertFalse(audit.signature_changed(BEFORE, after, "Differ.__init__"))


class GateChainTests(unittest.TestCase):
    def verdicts(self, after, qualified_name="classify"):
        return {row["gate"]: row for row in audit.run_gates(BEFORE, after, qualified_name)}

    def test_every_gate_reports_even_after_one_rejects(self):
        after = BEFORE.replace("def __init__(self, a, b, rtol=0.0, atol=0.0):",
                               "def __init__(self, a, b):")
        rows = self.verdicts(after, "Differ.__init__")
        self.assertGreaterEqual(len(rows), 6)
        self.assertEqual(rows["outside_body_equal"]["verdict"], "reject")

    def test_a_real_edit_clears_every_gate(self):
        after = BEFORE.replace('re.compile(r"READ")', 're.compile(r"READ", re.IGNORECASE)')
        for gate, row in self.verdicts(after).items():
            self.assertEqual(row["verdict"], "pass", f"{gate}: {row.get('reason')}")

    def test_an_unchanged_body_is_rejected(self):
        rows = self.verdicts(BEFORE)
        self.assertEqual(rows["check_executable_change"]["verdict"], "reject")

    def test_an_undefined_name_is_rejected(self):
        after = BEFORE.replace('re.compile(r"READ")', 'regex.compile(r"READ")')
        self.assertEqual(self.verdicts(after)["check_new_undefined_names"]["verdict"],
                         "reject")

    def test_an_unresolvable_target_is_an_analysis_error_not_a_rejection(self):
        # This was not unreachable: a nested function the resolver cannot pin down
        # reached it, and every target-dependent gate reported ValueError, which
        # run_gates was reading as "this patch is bad". One such patch was counted
        # as four defects.
        rows = audit.run_gates(BEFORE, BEFORE, "convert.inner")
        verdicts = {row["gate"]: row["verdict"] for row in rows}
        self.assertEqual(verdicts.get("target_resolution"), "error")
        self.assertNotIn("reject", [v for g, v in verdicts.items()
                                    if g != "check_executable_change"])

    def test_an_unresolvable_target_does_not_run_the_gates_that_need_one(self):
        gates = {row["gate"] for row in audit.run_gates(BEFORE, BEFORE, "convert.inner")}
        for needs_target in ("outside_body_equal", "check_new_undefined_names",
                             "check_reachable", "orphaned_attribute"):
            self.assertNotIn(needs_target, gates)

    def test_the_whole_file_gate_still_runs_without_a_resolvable_target(self):
        gates = {row["gate"] for row in audit.run_gates(BEFORE, BEFORE, "convert.inner")}
        self.assertIn("check_executable_change", gates)


class ApplyTests(unittest.TestCase):
    def test_a_patch_that_applies_returns_the_patched_text(self):
        after = BEFORE.replace('return "data"', 'return "data,1"')
        result = audit.apply_patch({"pkg/d.py": BEFORE}, diff_for(BEFORE, after))
        self.assertIsInstance(result, dict)
        self.assertEqual(result["pkg/d.py"], after)

    def test_a_patch_that_does_not_apply_returns_the_reason(self):
        after = BEFORE.replace('return "data"', 'return "data,1"')
        patch = diff_for(BEFORE, after)
        # The hunk's own context has to differ. Editing a distant part of the file
        # is not enough: git apply tolerates offsets, and an earlier version of
        # this test passed a patch that applied cleanly.
        other = BEFORE.replace('    pattern = re.compile(r"READ")\n',
                               '    pattern = re.compile(r"WRITE", re.I)\n')
        other = other.replace('    if pattern.match(line):\n',
                              '    if pattern.search(line):\n')
        other = other.replace('        return "command"\n', '        return "cmd"\n')
        result = audit.apply_patch({"pkg/d.py": other}, patch)
        self.assertIsInstance(result, str)
        self.assertIn("git_apply_failed", result)


class VerdictSeparationTests(unittest.TestCase):
    """A gate that could not run has not approved anything."""

    def audit(self, qualified_name, patch_tail='return "data,1"'):
        after = BEFORE.replace('return "data"', patch_tail)
        applied = audit.apply_patch({"pkg/d.py": BEFORE}, diff_for(BEFORE, after))
        self.assertIsInstance(applied, dict, applied)
        return audit.run_gates(BEFORE, applied["pkg/d.py"], qualified_name)

    def test_a_patch_the_audit_cannot_analyse_is_unknown_not_passing(self):
        findings = self.audit("classify.inner")
        self.assertTrue(any(f["verdict"] == "error" for f in findings))

    def test_a_result_with_only_errors_is_not_reported_as_passing(self):
        result = audit.audit_one("x", "--- a/p.py\n+++ b/p.py\n@@ -1 +1 @@\n-a\n+b\n",
                                 {"repo": "demo/demo", "base_commit": "abc"},
                                 Path("/nonexistent"))
        self.assertNotEqual(result["verdict"], "passes_gates")


class ChangedLinesTests(unittest.TestCase):
    """Only the lines a hunk changes, never its context.

    Context is why this audit gated functions nobody had edited: three lines of
    it reach into the next function, where before and after are identical, and
    check_executable_change then reported "only comments or docstrings differ"
    about a function the patch never touched.
    """

    PATCH = (
        "--- a/pkg/m.py\n"
        "+++ b/pkg/m.py\n"
        "@@ -10,7 +10,7 @@\n"
        " def first():\n"
        "     x = 1\n"
        "     y = 2\n"
        "-    return x\n"
        "+    return x + y\n"
        " \n"
        " \n"
        " def second():\n"
    )

    def test_a_replacement_names_only_the_removed_line(self):
        self.assertEqual(audit.changed_lines(self.PATCH), {"pkg/m.py": {13}})

    def test_context_lines_are_not_reported_as_changed(self):
        touched = audit.changed_lines(self.PATCH)["pkg/m.py"]
        for context_line in (10, 11, 12, 14, 15, 16):
            self.assertNotIn(context_line, touched)

    def test_a_pure_insertion_anchors_inside_the_receiving_function(self):
        patch = ("--- a/pkg/m.py\n+++ b/pkg/m.py\n@@ -10,3 +10,4 @@\n"
                 " def first():\n"
                 "     x = 1\n"
                 "+    y = 2\n"
                 "     return x\n")
        # the line before the insertion is line 11, inside first()
        self.assertEqual(audit.changed_lines(patch), {"pkg/m.py": {11}})

    def test_several_hunks_in_one_file_are_all_reported(self):
        patch = ("--- a/pkg/m.py\n+++ b/pkg/m.py\n"
                 "@@ -3,2 +3,2 @@\n-    a = 1\n+    a = 2\n b = 3\n"
                 "@@ -30,2 +30,2 @@\n-    c = 1\n+    c = 2\n d = 3\n")
        self.assertEqual(audit.changed_lines(patch), {"pkg/m.py": {3, 30}})

    def test_several_files_are_kept_apart(self):
        patch = ("--- a/pkg/a.py\n+++ b/pkg/a.py\n@@ -5,1 +5,1 @@\n-    a\n+    b\n"
                 "--- a/pkg/b.py\n+++ b/pkg/b.py\n@@ -9,1 +9,1 @@\n-    c\n+    d\n")
        self.assertEqual(audit.changed_lines(patch),
                         {"pkg/a.py": {5}, "pkg/b.py": {9}})

    def test_a_no_newline_marker_does_not_advance_the_line_counter(self):
        patch = ("--- a/pkg/m.py\n+++ b/pkg/m.py\n@@ -1,2 +1,2 @@\n"
                 " a = 1\n-b = 2\n\\ No newline at end of file\n+b = 3\n")
        self.assertEqual(audit.changed_lines(patch), {"pkg/m.py": {2}})

    def test_a_patch_naming_no_file_yields_nothing(self):
        self.assertEqual(audit.changed_lines("not a diff at all"), {})

    def test_it_is_narrower_than_the_gold_audit_helper_it_replaced(self):
        from audit_stage4_target_vs_gold import patched_lines
        wide = patched_lines(self.PATCH)["pkg/m.py"]
        narrow = audit.changed_lines(self.PATCH)["pkg/m.py"]
        self.assertTrue(narrow < wide)


class LineEndingTests(unittest.TestCase):
    """The platform must not change what the gates are shown."""

    def test_applied_text_is_lf_whatever_git_on_this_platform_does(self):
        after = BEFORE.replace('return "data"', 'return "data,1"')
        result = audit.apply_patch({"pkg/d.py": BEFORE}, diff_for(BEFORE, after))
        self.assertIsInstance(result, dict)
        self.assertNotIn("\r", result["pkg/d.py"])

    def test_an_lf_patch_still_applies_to_a_crlf_source(self):
        # Real snapshots in this corpus are checked out by git, which on Windows
        # may write CRLF, while SWE-bench patches are LF.
        crlf = BEFORE.replace("\n", "\r\n")
        after = BEFORE.replace('return "data"', 'return "data,1"')
        result = audit.apply_patch({"pkg/d.py": crlf}, diff_for(BEFORE, after))
        self.assertIsInstance(result, dict, result)
        self.assertEqual(result["pkg/d.py"], after)

    def test_lone_carriage_returns_are_normalised_too(self):
        self.assertEqual(audit.to_lf("a\r\nb\rc\nd"), "a\nb\nc\nd")


class CommitsFileTests(unittest.TestCase):
    def test_only_repo_and_base_commit_are_taken_from_the_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "commits.jsonl"
            path.write_text(json.dumps({
                "instance_id": "demo-1", "repo": "demo/demo", "base_commit": "abc123",
                "patch": "diff --git a b", "test_patch": "tests",
                "FAIL_TO_PASS": '["t"]', "hints_text": "hint",
            }) + "\n", encoding="utf-8")
            table = audit.load_commits(path)
        self.assertEqual(table["demo-1"], {"repo": "demo/demo", "base_commit": "abc123"})
        self.assertEqual(set(table["demo-1"]), {"repo", "base_commit"})

    def test_a_blank_line_does_not_end_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "commits.jsonl"
            path.write_text(
                json.dumps({"instance_id": "a", "repo": "r", "base_commit": "1"}) + "\n"
                + "\n"
                + json.dumps({"instance_id": "b", "repo": "r", "base_commit": "2"}) + "\n",
                encoding="utf-8")
            self.assertEqual(sorted(audit.load_commits(path)), ["a", "b"])


class VerdictTests(unittest.TestCase):
    def test_a_missing_base_commit_is_unknown_not_a_pass(self):
        result = audit.audit_one("x", "--- a/p.py\n+++ b/p.py\n@@ -1 +1 @@\n-a\n+b\n",
                                 {"repo": "demo/demo"}, Path("/nonexistent"))
        self.assertEqual(result["verdict"], "unknown")

    def test_a_patch_naming_no_file_is_unknown(self):
        result = audit.audit_one("x", "no diff here",
                                 {"repo": "r", "base_commit": "abc"}, Path("/nonexistent"))
        self.assertEqual(result["verdict"], "unknown")
        self.assertIn("names no files", result["error"])

    def test_a_missing_repository_is_unknown_not_rejected(self):
        result = audit.audit_one("x", "--- a/p.py\n+++ b/p.py\n@@ -1 +1 @@\n-a\n+b\n",
                                 {"repo": "demo/demo", "base_commit": "abc"},
                                 Path("/nonexistent"))
        self.assertEqual(result["verdict"], "unknown")


if __name__ == "__main__":
    unittest.main()
