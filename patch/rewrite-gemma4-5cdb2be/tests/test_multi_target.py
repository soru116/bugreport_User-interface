"""Scanning several targets instead of stopping at the first supported one."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fim_patch.generator import git
from fim_patch.multi_target import TARGET_POLICY_NAME, MultiTargetGenerator

SOURCE = (
    b"class Widget:\n"                 # 1
    b"    def render(self):\n"         # 2
    b'        return "x"\n'            # 3
    b"\n"                              # 4
    b"\n"                              # 5
    b"def first(value):\n"             # 6
    b"    value = value.strip()\n"     # 7
    b"    return value\n"              # 8
    b"\n"                              # 9
    b"\n"                              # 10
    b"def second(value):\n"            # 11
    b"    value = value.upper()\n"     # 12
    b"    return value\n"              # 13
)


class ScriptedBackend:
    """Returns a body per call and records which prompts it was given."""

    max_new_tokens = 2048

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0
        self.seeds = []

    def describe(self):
        return {"model": "test-double"}

    def infill(self, prefix, suffix, *, seed, max_new_tokens=None):
        middle = self.outputs[min(self.calls, len(self.outputs) - 1)]
        self.calls += 1
        self.seeds.append(seed)
        return {"middle": middle, "seed": seed, "stop_reason": "visible_eot"}


class MultiTargetTests(unittest.TestCase):
    def setUp(self):
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
        self.widget = {"file_path": "app.py", "symbol_qualified_name": "Widget",
                       "symbol_kind": "class", "start_line": 1, "end_line": 3}
        self.first = {"file_path": "app.py", "symbol_qualified_name": "first",
                      "symbol_kind": "function", "start_line": 6, "end_line": 8}
        self.second = {"file_path": "app.py", "symbol_qualified_name": "second",
                       "symbol_kind": "function", "start_line": 11, "end_line": 13}
        self.ticket = {"base_commit": self.base, "description": "Lowercase the value"}

    def localization(self, ranked, pool=None):
        location = {"base_commit": self.base, "confidence_level": "high",
                    "should_manual_review": False, "recommend_patch_generation": True,
                    "stage3_ranked_symbols": ranked}
        if pool is not None:
            location["stage3_candidate_symbols"] = pool
        return location

    def run_with(self, outputs, ranked, pool=None, **kwargs):
        backend = ScriptedBackend(outputs)
        gen = MultiTargetGenerator(backend, candidates=1, research_mode=True, **kwargs)
        return backend, gen.generate(self.ticket, self.localization(ranked, pool), self.repo)

    # ---- the behaviour this class exists for ---------------------------

    def test_a_later_target_is_reached_when_the_first_one_yields_nothing(self):
        """The single-target path stops at `first`; here `second` still runs."""

        backend, result = self.run_with(
            ["    value = value.strip()\n    return value\n",   # first: no_op
             "    return value.lower()\n"],                     # second: a real edit
            ranked=[self.first], pool=[self.widget, self.first, self.second])
        self.assertEqual(backend.calls, 2)
        self.assertEqual(result["patch_status"], "generated")
        self.assertEqual(result["bug_location"]["function"], "second")
        self.assertEqual(result["target_rank"], 2)
        self.assertTrue(result["target_fallback_used"])

    def test_every_attempt_is_recorded_with_its_outcome(self):
        backend, result = self.run_with(
            ["    value = value.strip()\n    return value\n",
             "    return value.lower()\n"],
            ranked=[self.first], pool=[self.first, self.second])
        attempts = result["target_attempts"]
        self.assertEqual([a["function"] for a in attempts], ["first", "second"])
        self.assertEqual([a["patch_status"] for a in attempts],
                         ["no_valid_candidate", "generated"])
        self.assertEqual([a["selected"] for a in attempts], [False, True])
        self.assertEqual(attempts[0]["candidate_reasons"], ["no_op"])
        self.assertEqual(result["targets_attempted"], 2)
        self.assertEqual(result["targets_generated"], 1)

    def test_classes_are_not_offered_as_targets(self):
        backend, result = self.run_with(
            ["    return value.lower()\n"],
            ranked=[self.widget], pool=[self.widget, self.first])
        self.assertTrue(result["callable_pool_used"])
        self.assertEqual([a["function"] for a in result["target_attempts"]], ["first"])
        self.assertEqual(backend.calls, 1)

    #: first: both body lines replaced by one -> 2 removed + 1 added = 3
    #: second: one line inserted, rest kept   -> 1 added = 1
    #: So the better-ranked target produces the larger patch and the two
    #: selection rules disagree -- the baseline-T08 situation in miniature.
    DISAGREEING_OUTPUTS = [
        "    return value.lower()\n",
        "    value = value.upper()\n    value = value.lower()\n    return value\n",
    ]

    def test_rank_first_keeps_the_better_ranked_target(self):
        """The opt-in rule: a bigger edit on the better-ranked function wins.

        It is opt-in rather than the default because replaying baseline-T08 under
        both rules scored it 0 better, 1 worse. Kept because the comparison is
        worth being able to run, not because it won.
        """

        backend, result = self.run_with(
            self.DISAGREEING_OUTPUTS, ranked=[self.first], pool=[self.first, self.second],
            selection_rule="rank_first")
        self.assertEqual(result["targets_generated"], 2)
        self.assertEqual(result["selection_rule"], "rank_first")
        self.assertEqual(result["bug_location"]["function"], "first")
        self.assertEqual(result["target_rank"], 1)
        self.assertEqual([a["selected"] for a in result["target_attempts"]], [True, False])

    def test_the_default_rule_is_the_one_every_recorded_run_used(self):
        _, result = self.run_with(
            self.DISAGREEING_OUTPUTS, ranked=[self.first], pool=[self.first, self.second])
        self.assertEqual(result["selection_rule"], "smallest_diff")
        self.assertEqual(result["target_rank"], 2)

    def test_smallest_patch_wins_when_several_targets_generate(self):
        """The pre-2026-10-02 rule, kept so recorded runs stay reproducible."""

        backend, result = self.run_with(
            self.DISAGREEING_OUTPUTS, ranked=[self.first], pool=[self.first, self.second],
            selection_rule="smallest_diff")
        self.assertEqual(result["targets_generated"], 2)
        self.assertEqual(result["bug_location"]["function"], "second")
        self.assertEqual(result["target_rank"], 2)
        self.assertEqual([a["selected"] for a in result["target_attempts"]], [False, True])

    def test_what_the_other_rule_would_have_chosen_is_recorded(self):
        """So "did the rule matter" never needs a second run to answer."""

        for rule, expected in (("rank_first", 1), ("smallest_diff", 2)):
            _, result = self.run_with(
                self.DISAGREEING_OUTPUTS, ranked=[self.first],
                pool=[self.first, self.second], selection_rule=rule)
            self.assertEqual(result["selection_alternatives"],
                             {"rank_first": 1, "smallest_diff": 2})
            self.assertEqual(result["target_rank"], expected)

    def test_earlier_target_wins_a_tie(self):
        backend, result = self.run_with(
            ["    return value.lower()\n", "    return value.lower()\n"],
            ranked=[self.first], pool=[self.first, self.second])
        self.assertEqual(result["targets_generated"], 2)
        self.assertEqual(result["target_rank"], 1)

    def test_candidates_from_all_targets_are_aggregated(self):
        backend, result = self.run_with(
            ["    value = value.strip()\n    return value\n",
             "    return value.lower()\n"],
            ranked=[self.first], pool=[self.first, self.second])
        ranks = [c.get("target_rank") for c in result["candidates"]]
        self.assertEqual(ranks, [1, 2])

    def test_seeds_differ_between_targets(self):
        backend, _ = self.run_with(
            ["    return value.lower()\n"],
            ranked=[self.first], pool=[self.first, self.second])
        self.assertEqual(len(set(backend.seeds)), 2)

    def test_max_targets_caps_the_scan(self):
        backend, result = self.run_with(
            ["    value = value.strip()\n    return value\n"],
            ranked=[self.first], pool=[self.first, self.second], max_targets=1)
        self.assertEqual(backend.calls, 1)
        self.assertEqual(result["targets_attempted"], 1)

    def test_ranked_list_is_used_when_there_is_no_candidate_pool(self):
        backend, result = self.run_with(
            ["    return value.lower()\n"], ranked=[self.first, self.second])
        self.assertFalse(result["callable_pool_used"])
        self.assertEqual(result["targets_attempted"], 2)

    def test_result_reports_its_own_policy(self):
        _, result = self.run_with(
            ["    return value.lower()\n"], ranked=[self.first], pool=[self.first])
        self.assertEqual(result["target_policy"], TARGET_POLICY_NAME)
        self.assertEqual(result["max_targets"], 5)

    def test_no_target_generates_leaves_a_clean_failure(self):
        _, result = self.run_with(
            ["    value = value.strip()\n    return value\n"],
            ranked=[self.first], pool=[self.first])
        self.assertEqual(result["patch_status"], "no_valid_candidate")
        self.assertEqual(result["targets_generated"], 0)
        self.assertEqual(result["patch"], "")

    def test_invalid_max_targets(self):
        for value in (0, 11, True, 2.5):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "max_targets"):
                MultiTargetGenerator(ScriptedBackend([]), max_targets=value)

    def test_low_confidence_still_blocks_without_research_mode(self):
        backend = ScriptedBackend([])
        location = self.localization([self.first], [self.first])
        location["confidence_level"] = "low"
        result = MultiTargetGenerator(backend, candidates=1).generate(
            self.ticket, location, self.repo)
        self.assertEqual(result["patch_status"], "blocked_low_confidence")
        self.assertEqual(backend.calls, 0)


if __name__ == "__main__":
    unittest.main()
