"""Target-selection tests for fim_patch.generator.

These cover the two changes made after measuring the real Stage-3 output of this
project:

1. Stage-3 ``end_line`` is a chunk boundary (the index is built in 80-line
   chunks), not a definition end, so requiring ``end_line == node.end_lineno``
   rejects targets on a false premise. ``expand_truncated_end`` repairs exactly
   that case and is now the default for any scanning policy.
2. 17 of the 46 measured tickets have a whole class as the rank-1 symbol, which
   this generator cannot patch at all. ``first_supported_ast`` walks down the
   ranking to the first target the AST can support and records which rank it
   landed on, so strict and fallback runs stay separable in the results.

Every test is offline: the backend is a double and no model is called.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fim_patch.generator import (
    TARGET_POLICY_DEPTH,
    FIMPatchGenerator,
    callable_candidate_rows,
    function_span,
    git,
    propagate_tolerance_keywords,
)

SOURCE = (
    b"class Widget:\n"            # 1
    b"    def render(self):\n"    # 2
    b'        return "x"\n'       # 3
    b"\n"                         # 4
    b"\n"                         # 5
    b"def helper(value):\n"       # 6
    b"    value = value.strip()\n"  # 7
    b"    value = value.lower()\n"  # 8
    b"    return value\n"         # 9
)


class RecordingBackend:
    """Counts calls so a test can prove a rejection happened before generation."""

    max_new_tokens = 2048

    def __init__(self, outputs=()):
        self.outputs = iter(outputs)
        self.calls = 0
        self.budgets = []

    def describe(self):
        return {"model": "test-double"}

    def infill(self, prefix, suffix, *, seed, max_new_tokens=None):
        self.calls += 1
        self.budgets.append(max_new_tokens)
        return {"middle": next(self.outputs), "seed": seed, "stop_reason": "visible_eot"}


class TargetSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        (self.repo / "app.py").write_bytes(SOURCE)
        git(self.repo, "init")
        git(self.repo, "config", "core.autocrlf", "false")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=t@example.invalid",
            "commit", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD").decode().strip()

        # Rank 1 is a whole class: unpatchable by this generator, exactly the
        # shape that 17 of our 46 tickets have.
        self.class_row = {"file_path": "app.py", "symbol_qualified_name": "Widget",
                          "start_line": 1, "end_line": 3}
        self.method_row = {"file_path": "app.py", "symbol_qualified_name": "Widget.render",
                           "start_line": 2, "end_line": 3}
        self.helper_row = {"file_path": "app.py", "symbol_qualified_name": "helper",
                           "start_line": 6, "end_line": 9}
        self.ticket = {"base_commit": self.base, "description": "Lowercase the value"}

    def localization(self, rows):
        return {"base_commit": self.base, "confidence_level": "high",
                "should_manual_review": False, "recommend_patch_generation": True,
                "stage3_ranked_symbols": rows}

    # ---- function_span -------------------------------------------------

    def test_body_lines_counts_the_body_only(self) -> None:
        span = function_span(SOURCE, self.helper_row)
        self.assertEqual(span["line_start"], 7)
        self.assertEqual(span["line_end"], 9)
        self.assertEqual(span["body_lines"], 3)

    def test_whole_function_span_includes_editable_signature(self) -> None:
        span = function_span(SOURCE, self.helper_row, include_signature=True)
        self.assertTrue(span["include_signature"])
        self.assertTrue(span["original_middle"].startswith("def helper(value):"))
        self.assertNotIn("def helper", span["prefix"])

    def test_max_body_lines_rejects_an_oversized_body(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            function_span(SOURCE, self.helper_row, max_body_lines=2)
        self.assertIn("above max_body_lines=2", str(ctx.exception))

    def test_max_body_lines_zero_means_no_limit(self) -> None:
        self.assertEqual(function_span(SOURCE, self.helper_row, max_body_lines=0)["body_lines"], 3)

    def test_truncated_end_line_needs_the_expansion_flag(self) -> None:
        truncated = dict(self.helper_row, end_line=8)  # chunk boundary inside the body
        with self.assertRaises(ValueError) as ctx:
            function_span(SOURCE, truncated)
        self.assertIn("disagree with AST", str(ctx.exception))
        span = function_span(SOURCE, truncated, expand_truncated_end=True)
        self.assertTrue(span["coordinates_expanded"])
        self.assertEqual(span["line_end"], 9)

    def test_expansion_never_accepts_an_end_line_past_the_definition(self) -> None:
        """The repair widens a truncated end; it must not accept an overshoot."""

        with self.assertRaises(ValueError):
            function_span(SOURCE, dict(self.helper_row, end_line=99), expand_truncated_end=True)

    # ---- policy configuration ------------------------------------------

    def test_new_policy_is_available_without_research_mode(self) -> None:
        generator = FIMPatchGenerator(RecordingBackend(), target_policy="first_supported_ast")
        self.assertEqual(generator.target_depth, TARGET_POLICY_DEPTH["first_supported_ast"])
        self.assertTrue(generator.expand_truncated_end)

    def test_legacy_research_policy_still_requires_research_mode(self) -> None:
        with self.assertRaises(ValueError):
            FIMPatchGenerator(RecordingBackend(), target_policy="first_supported_ast_top5")

    def test_called_helper_policy_requires_research_mode(self) -> None:
        with self.assertRaises(ValueError):
            FIMPatchGenerator(RecordingBackend(), target_policy="called_helper_top5")

    def test_unknown_policy_still_rejected(self) -> None:
        with self.assertRaises(ValueError):
            FIMPatchGenerator(RecordingBackend(), target_policy="whatever")

    def test_strict_policy_keeps_strict_coordinates_by_default(self) -> None:
        self.assertFalse(FIMPatchGenerator(RecordingBackend()).expand_truncated_end)

    def test_expansion_can_be_set_explicitly_on_either_policy(self) -> None:
        self.assertTrue(FIMPatchGenerator(RecordingBackend(), expand_truncated_end=True).expand_truncated_end)
        self.assertFalse(FIMPatchGenerator(RecordingBackend(), target_policy="first_supported_ast",
                                           expand_truncated_end=False).expand_truncated_end)

    def test_max_body_lines_is_validated(self) -> None:
        for bad in (-1, True, 1.5, "50"):
            with self.assertRaises(ValueError):
                FIMPatchGenerator(RecordingBackend(), max_body_lines=bad)

    # ---- end-to-end selection ------------------------------------------

    def test_strict_policy_blocks_on_a_class_target(self) -> None:
        backend = RecordingBackend()
        result = FIMPatchGenerator(backend, candidates=1).generate(
            self.ticket, self.localization([self.class_row, self.method_row]), self.repo)
        self.assertEqual(result["patch_status"], "blocked_invalid_context")
        self.assertEqual(backend.calls, 0)
        self.assertIsNone(result["target_rank"])
        self.assertEqual(len(result["target_resolution"]), 1)

    def test_scanning_policy_falls_through_to_the_first_supported_rank(self) -> None:
        backend = RecordingBackend(['        return "y"\n'])
        result = FIMPatchGenerator(backend, candidates=1, target_policy="first_supported_ast").generate(
            self.ticket, self.localization([self.class_row, self.method_row]), self.repo)
        self.assertEqual(result["patch_status"], "generated")
        self.assertEqual(result["target_rank"], 2)
        self.assertTrue(result["target_fallback_used"])
        self.assertEqual([x["status"] for x in result["target_resolution"]], ["rejected", "selected"])
        self.assertIn("Unsupported or ambiguous", result["target_resolution"][0]["reason"])

    def test_rank_one_success_is_not_reported_as_a_fallback(self) -> None:
        backend = RecordingBackend(['        return "y"\n'])
        result = FIMPatchGenerator(backend, candidates=1, target_policy="first_supported_ast").generate(
            self.ticket, self.localization([self.method_row]), self.repo)
        self.assertEqual(result["patch_status"], "generated")
        self.assertEqual(result["target_rank"], 1)
        self.assertFalse(result["target_fallback_used"])

    def test_called_helper_policy_prefers_strongest_helper_evidence(self) -> None:
        backend = RecordingBackend(['    return value.strip().lower()\n'])
        ordinary = dict(self.method_row, called_helper_score=0.0)
        helper = dict(self.helper_row, called_helper_score=0.4,
                      called_by_file="app.py", called_by_symbol="Widget.render")
        result = FIMPatchGenerator(
            backend, candidates=1, research_mode=True,
            target_policy="called_helper_top5",
        ).generate(self.ticket, self.localization([ordinary, helper]), self.repo)
        self.assertEqual(result["patch_status"], "generated")
        self.assertEqual(result["target_rank"], 2)
        self.assertEqual(result["target_resolution"][0]["rank"], 2)

    def test_called_helper_policy_prefers_ticket_matching_helper(self) -> None:
        backend = RecordingBackend(['    return value.strip().lower()\n'])
        stronger_unrelated = dict(
            self.method_row, called_helper_score=0.5,
            called_by_symbol="Widget.__init__",
        )
        matching_helper = dict(
            self.helper_row, called_helper_score=0.4,
            called_by_symbol="Widget.normalize_value",
        )
        result = FIMPatchGenerator(
            backend, candidates=1, research_mode=True,
            target_policy="called_helper_top5",
        ).generate(
            dict(self.ticket, description="Normalize and lowercase the value"),
            self.localization([stronger_unrelated, matching_helper]), self.repo,
        )
        self.assertEqual(result["patch_status"], "generated")
        self.assertEqual(result["target_rank"], 2)

    def test_multisymbol_policy_edits_helper_signature_and_cross_file_caller(self) -> None:
        helper_source = b"def compare(value):\n    return value\n"
        (self.repo / "helpers.py").write_bytes(helper_source)
        with (self.repo / "app.py").open("ab") as stream:
            stream.write(b"\nfrom helpers import compare\n")
        git(self.repo, "add", "helpers.py", "app.py")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=t@example.invalid",
            "commit", "-m", "add helper")
        self.base = git(self.repo, "rev-parse", "HEAD").decode().strip()
        self.ticket["base_commit"] = self.base
        helper_row = {
            "file_path": "helpers.py", "symbol_qualified_name": "compare",
            "start_line": 1, "end_line": 2, "called_helper_score": 0.4,
            "called_by_file": "app.py", "called_by_symbol": "Widget.render",
        }
        location = self.localization([helper_row])
        location["stage3_candidate_symbols"] = [self.method_row]
        backend = RecordingBackend([
            "def compare(value, rtol=0.0):\n    return value\n",
            "        return compare('x', rtol=0.1)\n",
        ])

        result = FIMPatchGenerator(
            backend, candidates=1, research_mode=True,
            target_policy="called_helper_multisymbol",
            include_original_body=True,
        ).generate(self.ticket, location, self.repo)

        self.assertEqual(result["patch_status"], "generated")
        self.assertEqual(set(result["modified_files"]), {"helpers.py", "app.py"})
        self.assertIn("def compare(value, rtol=0.0):", result["patch"])
        self.assertIn("return compare('x', rtol=0.1)", result["patch"])

    def test_related_targets_policy_builds_one_patch_from_direct_call_evidence(self) -> None:
        (self.repo / "helpers.py").write_text(
            "def normalize(value):\n    return value\n", encoding="utf-8")
        with (self.repo / "app.py").open("ab") as stream:
            stream.write(b"\nfrom helpers import normalize\n")
        git(self.repo, "add", "helpers.py", "app.py")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=t@example.invalid",
            "commit", "-m", "add related helper")
        self.base = git(self.repo, "rev-parse", "HEAD").decode().strip()
        self.ticket["base_commit"] = self.base
        helper_row = {
            "file_path": "helpers.py", "symbol_qualified_name": "normalize",
            "start_line": 1, "end_line": 2, "called_helper_score": 0.5,
            "called_by_file": "app.py", "called_by_symbol": "Widget.render",
        }
        location = self.localization([helper_row])
        location["stage3_candidate_symbols"] = [self.method_row]
        backend = RecordingBackend([
            "def normalize(value):\n    return value.strip().lower()\n",
            "    def render(self):\n        return normalize(' X ')\n",
        ])

        result = FIMPatchGenerator(
            backend, candidates=1, research_mode=True,
            target_policy="related_targets_top5", include_original_body=True,
        ).generate(self.ticket, location, self.repo)

        self.assertEqual(result["patch_status"], "generated")
        self.assertEqual(set(result["modified_files"]), {"helpers.py", "app.py"})
        self.assertEqual(len(result["target_set"]), 2)
        self.assertIn("helpers.py:normalize", result["repair_contract"])
        self.assertIn("app.py:Widget.render", result["repair_contract"])
        self.assertEqual(backend.calls, 2)

    def test_related_targets_policy_keeps_single_file_when_no_relation_exists(self) -> None:
        backend = RecordingBackend(["def helper(value):\n    return value.strip()\n"])
        result = FIMPatchGenerator(
            backend, candidates=1, research_mode=True,
            target_policy="related_targets_top5",
        ).generate(self.ticket, self.localization([self.helper_row]), self.repo)

        self.assertEqual(result["patch_status"], "generated")
        self.assertEqual(result["modified_files"], ["app.py"])
        self.assertEqual(len(result["target_set"]), 1)

    def test_tolerance_caller_wiring_is_a_precise_host_transform(self) -> None:
        raw = (
            b"class Reporter:\n"
            b"    def report(self, left, right):\n"
            b"        first = report_diff_values(left, right)\n"
            b"        second = report_diff_values(\n"
            b"            right,\n"
            b"            left,\n"
            b"        )\n"
            b"        return first, second\n"
        )
        row = {"symbol_qualified_name": "Reporter.report", "start_line": 2,
               "end_line": 8}
        span = function_span(raw, row)

        middle = propagate_tolerance_keywords(
            raw, span, "report_diff_values")

        compact = "".join(middle.split())
        self.assertEqual(compact.count("rtol=self.rtol"), 2)
        self.assertEqual(compact.count("atol=self.atol"), 2)
        self.assertIn("return first, second", middle)

    def test_scanning_policy_repairs_a_chunk_truncated_end_line(self) -> None:
        """The real Stage-3 failure mode: end_line is the 80-line chunk edge."""

        backend = RecordingBackend(["    return value.strip().lower()\n"])
        rows = [dict(self.helper_row, end_line=8)]
        result = FIMPatchGenerator(backend, candidates=1, target_policy="first_supported_ast").generate(
            self.ticket, self.localization(rows), self.repo)
        self.assertEqual(result["patch_status"], "generated")
        self.assertTrue(result["target_resolution"][0]["coordinates_expanded"])
        self.assertTrue(result["span"]["coordinates_expanded"])

        strict = FIMPatchGenerator(RecordingBackend(), candidates=1).generate(
            self.ticket, self.localization(rows), self.repo)
        self.assertEqual(strict["patch_status"], "blocked_invalid_context")

    def test_size_limit_rejects_before_any_model_call(self) -> None:
        backend = RecordingBackend()
        result = FIMPatchGenerator(backend, candidates=1, target_policy="first_supported_ast",
                                   max_body_lines=2).generate(
            self.ticket, self.localization([self.helper_row]), self.repo)
        self.assertEqual(result["patch_status"], "blocked_invalid_context")
        self.assertEqual(backend.calls, 0)
        self.assertIn("max_body_lines", result["target_resolution"][0]["reason"])

    def test_size_limit_skips_the_oversized_rank_and_takes_the_next(self) -> None:
        backend = RecordingBackend(['        return "y"\n'])
        result = FIMPatchGenerator(backend, candidates=1, target_policy="first_supported_ast",
                                   max_body_lines=2).generate(
            self.ticket, self.localization([self.helper_row, self.method_row]), self.repo)
        self.assertEqual(result["patch_status"], "generated")
        self.assertEqual(result["target_rank"], 2)
        self.assertEqual(result["max_body_lines"], 2)

    def test_selection_settings_are_recorded_in_the_result(self) -> None:
        """A run's results must say which policy produced them."""

        result = FIMPatchGenerator(RecordingBackend(), candidates=1,
                                   target_policy="first_supported_ast",
                                   max_body_lines=120).generate(
            self.ticket, self.localization([self.class_row]), self.repo)
        self.assertEqual(result["target_policy"], "first_supported_ast")
        self.assertTrue(result["expand_truncated_end"])
        self.assertEqual(result["max_body_lines"], 120)
        self.assertFalse(result["research_mode"])

    def test_source_is_never_modified_by_selection(self) -> None:
        FIMPatchGenerator(RecordingBackend(['        return "y"\n']), candidates=1,
                          target_policy="first_supported_ast").generate(
            self.ticket, self.localization([self.class_row, self.method_row]), self.repo)
        self.assertEqual((self.repo / "app.py").read_bytes(), SOURCE)


if __name__ == "__main__":
    unittest.main()


class CallableCandidatePoolTests(unittest.TestCase):
    """Classes occupy top-5 slots that the AST gate then throws away.

    Measured on the fixed five tickets: astropy-14365 spent 4 of 5 slots on
    classes with the reference patch's symbol at candidate #10; astropy-13236
    spent 3 with its gold symbol at #9. Filtering the pool to callables moves
    both into the top 5 (#5 and #4), taking hit@5 from 1/5 to 3/5, with no new
    scoring signal.
    """

    def pool(self, *kinds):
        return {"stage3_candidate_symbols": [
            {"file_path": "app.py", "symbol_qualified_name": f"sym{i}",
             "symbol_kind": kind}
            for i, kind in enumerate(kinds, 1)]}

    def names(self, rows):
        return [row["symbol_qualified_name"] for row in rows]

    def test_classes_and_modules_are_dropped(self) -> None:
        rows = callable_candidate_rows(
            self.pool("class", "class", "function", "module", "method", "function"), 5)
        self.assertEqual(self.names(rows), ["sym3", "sym5", "sym6"])

    def test_14365_shape_moves_the_tenth_candidate_into_the_top_five(self) -> None:
        kinds = ["class", "class", "function", "class", "class",
                 "function", "function", "module", "method", "function"]
        pool = self.pool(*kinds)
        self.assertNotIn("sym10", self.names(pool["stage3_candidate_symbols"][:5]))
        self.assertIn("sym10", self.names(callable_candidate_rows(pool, 5)))

    def test_order_within_the_pool_is_preserved(self) -> None:
        rows = callable_candidate_rows(self.pool("function", "class", "method"), 5)
        self.assertEqual(self.names(rows), ["sym1", "sym3"])

    def test_limit_is_respected(self) -> None:
        self.assertEqual(len(callable_candidate_rows(self.pool(*(["function"] * 9)), 5)), 5)

    def test_rows_without_an_identity_are_dropped(self) -> None:
        pool = {"stage3_candidate_symbols": [
            {"file_path": "app.py", "symbol_kind": "function"},
            {"symbol_qualified_name": "orphan", "symbol_kind": "function"},
            "not a dict",
            {"file_path": "app.py", "symbol_qualified_name": "good",
             "symbol_kind": "function"}]}
        self.assertEqual(self.names(callable_candidate_rows(pool, 5)), ["good"])

    def test_missing_pool_returns_empty_so_the_caller_keeps_the_ranked_list(self) -> None:
        self.assertEqual(callable_candidate_rows({}, 5), [])
        self.assertEqual(callable_candidate_rows({"stage3_candidate_symbols": None}, 5), [])

    def test_policy_is_registered_and_research_only(self) -> None:
        from fim_patch.generator import RESEARCH_ONLY_TARGET_POLICIES
        self.assertEqual(TARGET_POLICY_DEPTH["callable_candidates_top5"], 5)
        self.assertIn("callable_candidates_top5", RESEARCH_ONLY_TARGET_POLICIES)


class CallableCandidatePolicyTests(TargetSelectionTests):
    """End to end: the policy reaches a method the ranked top-5 never offered."""

    def test_pool_target_is_used_when_the_ranked_list_only_has_a_class(self) -> None:
        location = self.localization([dict(self.class_row, symbol_kind="class")])
        location["stage3_candidate_symbols"] = [
            dict(self.class_row, symbol_kind="class"),
            dict(self.helper_row, symbol_kind="function"),
        ]
        backend = RecordingBackend(["    return value.lower()\n"])
        result = FIMPatchGenerator(
            backend, candidates=1, research_mode=True,
            target_policy="callable_candidates_top5").generate(
                self.ticket, location, self.repo)
        self.assertTrue(result["callable_pool_used"])
        self.assertEqual(result["patch_status"], "generated", result["candidates"])
        self.assertEqual(result["bug_location"]["function"], "helper")

    def test_ranked_list_is_kept_when_there_is_no_pool(self) -> None:
        location = self.localization([dict(self.helper_row, symbol_kind="function")])
        backend = RecordingBackend(["    return value.lower()\n"])
        result = FIMPatchGenerator(
            backend, candidates=1, research_mode=True,
            target_policy="callable_candidates_top5").generate(
                self.ticket, location, self.repo)
        self.assertFalse(result["callable_pool_used"])
        self.assertEqual(result["patch_status"], "generated", result["candidates"])

    def test_policy_requires_research_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "research_mode"):
            FIMPatchGenerator(RecordingBackend([]),
                              target_policy="callable_candidates_top5")

