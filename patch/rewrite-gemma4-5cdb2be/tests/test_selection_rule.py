"""Choosing between patches on different functions.

The rule under test is the one that lost the repair on baseline-T08: ordering
generated patches by diff size and using the Stage-3 rank only to break ties.
Across different functions, diff size measures how little the model had to say
about a function, and a model with little to say is usually looking at the wrong
one -- 13465's gold symbol sat at rank 2 and a rank-4 patch won on size.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from fim_patch.generator import changed_line_count  # noqa: E402
from fim_patch.multi_target import (  # noqa: E402
    SELECTION_RULES, MultiTargetGenerator, selection_key)

import replay_multitarget_selection as replay  # noqa: E402
from tests.test_fim import FakeBackend  # noqa: E402


def diff(path, removed, added):
    body = "".join(f"-{line}\n" for line in removed) + "".join(f"+{line}\n" for line in added)
    return f"--- a/{path}\n+++ b/{path}\n@@ -1,{len(removed)} +1,{len(added)} @@\n{body}"


class ChangedLineCountTests(unittest.TestCase):
    def test_diff_headers_are_not_counted_as_changes(self):
        self.assertEqual(changed_line_count(diff("a.py", ["x = 1"], ["x = 2"])), 2)

    def test_a_two_file_patch_is_not_surcharged_for_its_second_header(self):
        # The old count charged two lines per file for "--- a/f" and "+++ b/f",
        # so a two-file patch started four lines behind a one-file patch's two.
        one = diff("a.py", ["x = 1"], ["x = 2"])
        two = one + diff("b.py", ["y = 1"], ["y = 2"])
        self.assertEqual(changed_line_count(one), 2)
        self.assertEqual(changed_line_count(two), 4)

    def test_hunk_headers_and_no_newline_markers_are_skipped(self):
        patch = ("--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
                 "\\ No newline at end of file\n")
        self.assertEqual(changed_line_count(patch), 2)

    def test_an_empty_patch_counts_nothing(self):
        for value in ("", None):
            self.assertEqual(changed_line_count(value), 0)


class SelectionKeyTests(unittest.TestCase):
    def triples(self):
        # rank 2 makes a real edit on the right function; rank 4 makes a
        # one-line edit on the wrong one. This is the 13465 shape.
        return [
            (2, {"symbol_qualified_name": "report_diff_values"},
             {"patch": diff("utils/diff.py", ["a != b", "return a", "x = 1"],
                            ["not allclose(a, b)", "return a", "x = 1", "y = 2"])}),
            (4, {"symbol_qualified_name": "FITSDiff._report"},
             {"patch": diff("io/fits/diff.py", ["pass"], ["return"])}),
        ]

    def test_rank_first_prefers_the_better_ranked_target(self):
        self.assertEqual(min(self.triples(), key=selection_key("rank_first"))[0], 2)

    def test_smallest_diff_prefers_the_smaller_patch_on_the_worse_rank(self):
        self.assertEqual(min(self.triples(), key=selection_key("smallest_diff"))[0], 4)

    def test_rank_first_still_uses_diff_size_inside_one_rank(self):
        big = diff("a.py", ["x = 1", "y = 2"], ["x = 3", "y = 4"])
        small = diff("a.py", ["x = 1"], ["x = 3"])
        triples = [(3, {}, {"patch": big}), (3, {}, {"patch": small})]
        chosen = min(triples, key=selection_key("rank_first"))
        self.assertEqual(changed_line_count(chosen[2]["patch"]), 2)

    def test_both_rules_are_total_orders_over_equal_inputs(self):
        same = [(1, {}, {"patch": diff("a.py", ["x"], ["y"])}),
                (1, {}, {"patch": diff("a.py", ["x"], ["y"])})]
        for rule in SELECTION_RULES:
            self.assertIsNotNone(min(same, key=selection_key(rule)))


class GeneratorWiringTests(unittest.TestCase):
    """The rule reaches _merge, and both choices are recorded either way."""

    SOURCE = (
        "def first(rows):\n"
        "    out = []\n"
        "    for row in rows:\n"
        "        out.append(row)\n"
        "    return out\n"
        "\n"
        "\n"
        "def second(rows):\n"
        "    return list(rows)\n"
    )

    def setUp(self):
        from fim_patch.generator import git
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        (self.repo / "app.py").write_bytes(self.SOURCE.encode("utf-8"))
        git(self.repo, "init")
        git(self.repo, "config", "core.autocrlf", "false")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=T", "-c", "user.email=t@example.invalid",
            "commit", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD").decode().strip()
        self.rows = [
            {"file_path": "app.py", "symbol_qualified_name": "first",
             "start_line": 1, "end_line": 5, "symbol_kind": "function"},
            {"file_path": "app.py", "symbol_qualified_name": "second",
             "start_line": 8, "end_line": 9, "symbol_kind": "function"},
        ]
        self.loc = {"base_commit": self.base, "confidence_level": "high",
                    "should_manual_review": False, "recommend_patch_generation": True,
                    "stage3_ranked_symbols": self.rows}
        self.ticket = {"base_commit": self.base,
                       "title": "blank rows are kept",
                       "description": ("### Description\nBlank rows are kept.\n"
                                       "### Expected behavior\nDrop blank rows.\n")}

    def run_with(self, **kwargs):
        class Backend(FakeBackend):
            """A multi-line edit for rank 1, a one-line edit for rank 2.

            This is the baseline-T08 shape in miniature: the better-ranked
            target produces the larger patch, so the two rules disagree.
            """

            # Replacing four lines with two: six changed lines against the
            # one-line edit's two.
            BIG = "    out = [row for row in rows if row]\n    return out\n"
            SMALL = "    return list(r for r in rows if r)\n"

            def __init__(self):
                super().__init__([])

            def infill(self, prefix, suffix, *, seed, max_new_tokens=None):
                self.calls += 1
                middle = self.BIG if "of first on the next line" in prefix else self.SMALL
                return {"middle": middle, "seed": seed, "stop_reason": "visible_eot"}

        generator = MultiTargetGenerator(Backend(), max_targets=2, candidates=1,
                                         include_original_body=True, **kwargs)
        return generator.generate(self.ticket, self.loc, self.repo)

    def test_the_default_rule_is_smallest_diff(self):
        """Unchanged, on purpose: replaying baseline-T08 scored the alternative
        0 better, 1 worse, so the default did not move."""

        result = self.run_with()
        self.assertEqual(result["selection_rule"], "smallest_diff")
        self.assertEqual(result["targets_generated"], 2)
        self.assertEqual(result["target_rank"], 2)

    def test_rank_first_chooses_rank_one_even_though_rank_two_is_smaller(self):
        result = self.run_with(selection_rule="rank_first")
        self.assertEqual(result["targets_generated"], 2)
        self.assertEqual(result["target_rank"], 1)

    def test_both_rules_choices_are_recorded_whichever_one_ran(self):
        for rule in SELECTION_RULES:
            result = self.run_with(selection_rule=rule)
            self.assertEqual(set(result["selection_alternatives"]), set(SELECTION_RULES))
            self.assertEqual(result["selection_alternatives"]["rank_first"], 1)
            self.assertEqual(result["selection_alternatives"]["smallest_diff"], 2)
            self.assertEqual(result["target_rank"],
                             result["selection_alternatives"][rule])

    def test_an_unknown_rule_is_refused(self):
        with self.assertRaises(ValueError):
            MultiTargetGenerator(None, selection_rule="whatever")

    def test_every_attempt_is_still_recorded(self):
        """Both attempts, with the winner marked, whichever rule is in force."""

        for rule, selected in (("smallest_diff", [False, True]),
                               ("rank_first", [True, False])):
            result = self.run_with(selection_rule=rule)
            self.assertEqual([a["rank"] for a in result["target_attempts"]], [1, 2])
            self.assertEqual([a["selected"] for a in result["target_attempts"]], selected)


class ReplayTests(unittest.TestCase):
    """The offline replay reads what a finished run already recorded."""

    def result_json(self):
        big = diff("app.py", ["a != b"], ["allclose(a, b)", "and more"])
        small = diff("other.py", ["pass"], ["return"])
        return {
            "target_rank": 4, "targets_generated": 2,
            "target_attempts": [
                {"rank": 2, "file": "app.py", "function": "report_diff_values",
                 "patch_status": "generated", "selected": False},
                {"rank": 4, "file": "other.py", "function": "FITSDiff._report",
                 "patch_status": "generated", "selected": True},
            ],
            "candidates": [
                {"candidate_id": 1, "target_rank": 2, "status": "generated", "patch": big},
                {"candidate_id": 1, "target_rank": 4, "status": "generated", "patch": small},
                {"candidate_id": 2, "target_rank": 4, "status": "rejected",
                 "patch": "", "reason": "no_op"},
            ],
        }

    def run_dir(self, result=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        run = Path(tmp.name) / "a-run"
        ticket = run / "tickets" / "demo__demo-1"
        ticket.mkdir(parents=True)
        (ticket / "result.json").write_text(
            json.dumps(result if result is not None else self.result_json()),
            encoding="utf-8")
        return run

    def test_each_target_contributes_its_own_passing_candidate(self):
        triples = replay.attempts_from(self.result_json())
        self.assertEqual([rank for rank, _, _ in triples], [2, 4])

    def test_a_rejected_candidate_is_never_a_targets_winner(self):
        triples = replay.attempts_from(self.result_json())
        for _, _, res in triples:
            self.assertTrue(res["patch"])

    def test_a_target_whose_every_candidate_failed_is_dropped(self):
        result = self.result_json()
        result["candidates"] = [c for c in result["candidates"] if c["target_rank"] != 2]
        self.assertEqual([r for r, _, _ in replay.attempts_from(result)], [4])

    def test_a_target_the_run_did_not_mark_generated_is_dropped(self):
        result = self.result_json()
        result["target_attempts"][0]["patch_status"] = "no_valid_candidate"
        self.assertEqual([r for r, _, _ in replay.attempts_from(result)], [4])

    def test_the_two_rules_disagree_on_this_run(self):
        report = replay.replay_run(self.run_dir(), {}, Path("/nonexistent"))
        row = report["tickets"][0]
        self.assertEqual(row["choices"]["rank_first"]["rank"], 2)
        self.assertEqual(row["choices"]["smallest_diff"]["rank"], 4)
        self.assertEqual(report["changed"], 1)

    def test_a_missing_reference_patch_is_no_target_not_a_hit(self):
        report = replay.replay_run(self.run_dir(), {}, Path("/nonexistent"))
        for rule in SELECTION_RULES:
            self.assertEqual(report["tickets"][0]["choices"][rule]["verdict"], "miss")

    def test_a_reference_patch_naming_the_chosen_function_is_a_hit(self):
        tickets = {"demo__demo-1": {
            "repo": "demo/demo", "base_commit": "abc",
            "patch": diff("app.py", ["a != b"], ["allclose(a, b)"])}}
        # git_show cannot resolve the commit, so truth falls back to module
        # level: the verdict must then be same_file, never hit.
        report = replay.replay_run(self.run_dir(), tickets, Path("/nonexistent"))
        self.assertEqual(report["tickets"][0]["choices"]["rank_first"]["verdict"],
                         "same_file")

    def test_a_run_with_no_generated_target_is_reported_not_crashed(self):
        result = self.result_json()
        result["candidates"] = []
        report = replay.replay_run(self.run_dir(result), {}, Path("/nonexistent"))
        self.assertIn("note", report["tickets"][0])

    def test_the_reference_patch_fields_are_the_only_ones_read(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "t.jsonl"
        path.write_text(json.dumps({
            "instance_id": "demo__demo-1", "repo": "demo/demo", "base_commit": "abc",
            "patch": "P", "test_patch": "SECRET", "FAIL_TO_PASS": '["t"]',
            "hints_text": "SECRET HINT"}) + "\n", encoding="utf-8")
        row = replay.load_tickets(path)["demo__demo-1"]
        self.assertEqual(set(row), {"patch", "base_commit", "repo"})

    def test_the_script_runs_end_to_end_and_writes_its_json(self):
        run = self.run_dir()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tickets = Path(tmp.name) / "t.jsonl"
        tickets.write_text("", encoding="utf-8")
        out = Path(tmp.name) / "report.json"
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "replay_multitarget_selection.py"),
             "--run", str(run), "--tickets", str(tickets), "--json", str(out)],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("rank_first", proc.stdout)
        self.assertEqual(len(json.loads(out.read_text(encoding="utf-8"))), 1)


if __name__ == "__main__":
    unittest.main()
