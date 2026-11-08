"""The oracle-localization capability probe, and the prompt hook it needs."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from fim_patch.generator import FIMPatchGenerator, git  # noqa: E402

import fim_capability_probe as probe  # noqa: E402

SOURCE = (
    b"import re\n"                                  # 1
    b"\n"                                           # 2
    b"\n"                                           # 3
    b"def classify(line):\n"                        # 4
    b'    """Say what kind of line this is."""\n'    # 5
    b"    pattern = re.compile(r'READ')\n"           # 6
    b"    if pattern.match(line):\n"                 # 7
    b"        return 'command'\n"                    # 8
    b"    return 'data'\n"                           # 9
)

ROW = {"file_path": "pkg/reader.py", "symbol_qualified_name": "classify",
       "symbol_name": "classify", "symbol_kind": "function",
       "start_line": 4, "end_line": 9, "score": 0.35}

TICKET = {"ticket_id": "demo-1", "title": "classify is case sensitive",
          "description": "classify('read') should be a command, not data.",
          "repo": "demo/demo", "base_commit": "HEAD"}


def localization(**overrides):
    data = {
        "bug_location": {"recommend_patch_generation": False,
                         "should_manual_review": True,
                         "confidence_level": "medium",
                         "stage3_ranked_symbols": [
                             {"rank": 1, "file_path": "pkg/other.py",
                              "symbol_qualified_name": "unrelated",
                              "symbol_kind": "function",
                              "start_line": 1, "end_line": 2}],
                         "stage3_candidate_symbols": []},
        "stage3_ranked_symbols": [{"rank": 1, "file_path": "pkg/other.py",
                                   "symbol_qualified_name": "unrelated"}],
        "stage3_candidate_symbols": [
            {"rank": 3, "file_path": "pkg/x.py", "symbol_qualified_name": "noise"},
            dict(ROW, rank=7),
        ],
        "stage3_retrieval_symbols": [],
    }
    data.update(overrides)
    return data


class ScriptedBackend:
    """Returns one body per candidate and keeps every prompt it was given."""

    max_new_tokens = 2048

    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.prompts = []

    def describe(self):
        return {"model": "scripted", "backend": "scripted-v0", "num_ctx": 8192}

    def infill(self, prefix, suffix, *, seed, max_new_tokens=None):
        self.prompts.append(prefix)
        body = self.bodies[min(len(self.prompts) - 1, len(self.bodies) - 1)]
        return {"middle": body, "raw_response": body + "<EOT>", "seed": seed,
                "stop_reason": "visible_eot", "token_budget": 256,
                "prompt": prefix, "suffix": suffix, "transport": "ollama_suffix",
                "metrics": {"done_reason": "stop"}}


def make_repo(tmp):
    repo = Path(tmp)
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "reader.py").write_bytes(SOURCE)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    return repo


# --------------------------------------------------------------------------
# picking the target the repair actually belongs in
# --------------------------------------------------------------------------

class TargetSelectionTests(unittest.TestCase):
    def test_a_symbol_below_the_ranked_top_five_is_still_reachable(self):
        row = probe.select_target(localization(), "classify")
        self.assertEqual(row["file_path"], "pkg/reader.py")
        self.assertEqual(row["probe_source_pool"], "stage3_candidate_symbols")
        self.assertEqual(row["probe_pool_position"], 2)

    def test_the_ranked_list_is_searched_before_the_candidate_pool(self):
        row = probe.select_target(localization(), "unrelated")
        self.assertEqual(row["probe_source_pool"], "stage3_ranked_symbols")

    def test_a_symbol_this_run_never_retrieved_names_what_it_did(self):
        with self.assertRaises(SystemExit) as caught:
            probe.select_target(localization(), "absent")
        message = str(caught.exception)
        self.assertIn("absent", message)
        self.assertIn("classify", message)

    def test_the_pinned_target_becomes_rank_one_and_nothing_else_moves(self):
        data = localization()
        row = probe.select_target(data, "classify")
        location = probe.oracle_location(data, row)
        ranked = location["stage3_ranked_symbols"]
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0]["symbol_qualified_name"], "classify")
        self.assertEqual(ranked[0]["rank"], 1)
        self.assertEqual(len(location["stage3_candidate_symbols"]), 2)

    def test_probe_bookkeeping_never_reaches_the_generator(self):
        data = localization()
        row = probe.select_target(data, "classify")
        pinned = probe.oracle_location(data, row)["stage3_ranked_symbols"][0]
        self.assertFalse([k for k in pinned if k.startswith("probe_")])

    def test_pinning_does_not_mutate_the_recorded_localization(self):
        data = localization()
        probe.oracle_location(data, probe.select_target(data, "classify"))
        self.assertEqual(
            [r.get("symbol_qualified_name") for r in data["bug_location"]["stage3_ranked_symbols"]],
            ["unrelated"])


# --------------------------------------------------------------------------
# counting attempts, which is the only thing the probe claims to measure
# --------------------------------------------------------------------------

BEFORE = ("    pattern = re.compile(r'READ')\n"
          "    if pattern.match(line):\n"
          "        return 'command'\n"
          "    return 'data'\n")


def result_with(bodies, statuses=None, reasons=None):
    rows = []
    for i, body in enumerate(bodies):
        rows.append({"candidate_id": i + 1, "middle": body, "seed": 42 + i,
                     "status": (statuses or ["rejected"] * len(bodies))[i],
                     "reason": (reasons or [""] * len(bodies))[i],
                     "stop_reason": "visible_eot"})
    return {"candidates": rows}


class AttemptCountingTests(unittest.TestCase):
    def test_a_verbatim_copy_is_counted_as_a_copy_not_an_edit(self):
        analysis = probe.analyze(result_with([BEFORE, BEFORE]), BEFORE, [])
        self.assertEqual(analysis["identical_to_original"], 2)
        self.assertEqual(analysis["changed"], 0)

    def test_trailing_whitespace_and_line_endings_do_not_pass_for_an_edit(self):
        analysis = probe.analyze(
            result_with([BEFORE.replace("\n", "\r\n"), BEFORE + "\n\n  "]), BEFORE, [])
        self.assertEqual(analysis["identical_to_original"], 2)

    def test_an_edit_is_counted_and_its_markers_reported(self):
        fixed = BEFORE.replace("r'READ')", "r'READ', re.IGNORECASE)")
        analysis = probe.analyze(result_with([fixed, BEFORE]), BEFORE,
                                 ["IGNORECASE", ".lower()"])
        self.assertEqual(analysis["changed"], 1)
        self.assertEqual(analysis["identical_to_original"], 1)
        self.assertEqual(analysis["with_any_marker"], 1)
        self.assertEqual(analysis["per_candidate"][0]["markers_present"], ["IGNORECASE"])

    def test_only_a_candidate_that_cleared_every_gate_counts_as_passed(self):
        fixed = BEFORE.replace("'data'", "'data,1'")
        analysis = probe.analyze(
            result_with([fixed, fixed], statuses=["generated", "rejected"],
                        reasons=["", "duplicate"]), BEFORE, [])
        self.assertEqual(analysis["changed"], 2)
        self.assertEqual(analysis["changed_and_passed_gates"], 1)
        self.assertEqual(analysis["rejection_reasons"], ["duplicate"])

    def test_an_empty_generation_is_neither_a_copy_nor_an_edit(self):
        analysis = probe.analyze(result_with(["", "   \n"]), BEFORE, [])
        self.assertEqual(analysis["empty"], 2)
        self.assertEqual(analysis["changed"], 0)
        self.assertEqual(analysis["identical_to_original"], 0)

    def test_a_blank_baseline_is_refused_rather_than_read_as_every_edit(self):
        with self.assertRaises(ValueError):
            probe.analyze(result_with([BEFORE]), "", [])

    def test_a_blank_baseline_is_allowed_when_nothing_was_generated(self):
        self.assertEqual(probe.analyze({"candidates": []}, "", [])["candidates"], 0)


class BaselineReadingTests(unittest.TestCase):
    def test_the_baseline_comes_from_the_span_not_from_modified_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            result = {"span": {"start": 45, "end": 60}, "modified_files": []}
            body = probe.original_body(repo, "pkg/reader.py", result)
            self.assertEqual(body, SOURCE.decode()[45:60])

    def test_a_missing_file_yields_a_blank_the_guard_will_catch(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = {"span": {"start": 0, "end": 10}}
            self.assertEqual(probe.original_body(Path(tmp), "nope.py", result), "")


# --------------------------------------------------------------------------
# the demonstration hook the fewshot arms use
# --------------------------------------------------------------------------

class ExtraDirectiveTests(unittest.TestCase):
    def generator(self, backend, **kwargs):
        return FIMPatchGenerator(backend, candidates=1, research_mode=True,
                                 include_original_body=True, **kwargs)

    def run_once(self, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            base = git(repo, "rev-parse", "HEAD").decode().strip()
            backend = ScriptedBackend(["    return 'command'\n"])
            location = {"base_commit": base,
                        "stage3_ranked_symbols": [dict(ROW, rank=1)],
                        "stage3_candidate_symbols": [dict(ROW, rank=1)]}
            result = self.generator(backend, **kwargs).generate(
                dict(TICKET, base_commit=base), location, str(repo))
            # A blocked run captures no prompt, which would make every
            # assertion below pass for the wrong reason.
            if not backend.prompts:
                raise AssertionError(
                    f"generation never reached the backend: "
                    f"{result.get('patch_status')}: {result.get('explanation')}")
            return backend.prompts

    def test_a_demonstration_reaches_the_prompt(self):
        prompts = self.run_once(extra_directive=probe.FEW_SHOT_DEMONSTRATION)
        self.assertTrue(prompts)
        self.assertIn("Worked example", prompts[0])
        self.assertIn("split_setting", prompts[0])

    def test_the_control_arm_prompt_carries_no_demonstration(self):
        self.assertNotIn("Worked example", self.run_once()[0])

    def test_the_demonstration_sits_with_the_instructions_not_in_the_code(self):
        """It rides with the instruction block, wherever that block is.

        It used to be asserted to come before the target's ``def`` line, because
        the instructions came before the whole source prefix. The task block now
        sits immediately above the generation point, so the demonstration lands
        after the source -- the invariant that matters is that it is inside the
        instruction block and still entirely commented out, never read as code.
        """

        prompt = self.run_once(extra_directive=probe.FEW_SHOT_DEMONSTRATION)[0]
        task = prompt.index("===== WHAT TO WRITE BELOW =====")
        demo = prompt.index("Worked example")
        self.assertGreater(demo, task)
        # From the start of the block's own line: ``task`` points into the
        # middle of one, past its "# ".
        for line in prompt[prompt.rindex("\n", 0, task) + 1:].splitlines():
            self.assertTrue(not line.strip() or line.lstrip().startswith("#"), line)

    def test_under_the_legacy_layout_the_demonstration_precedes_the_code(self):
        prompt = self.run_once(extra_directive=probe.FEW_SHOT_DEMONSTRATION,
                               prompt_layout="legacy")[0]
        self.assertLess(prompt.index("Worked example"), prompt.index("def classify"))

    def test_the_shipped_demonstration_is_only_comments(self):
        body = probe.FEW_SHOT_DEMONSTRATION.strip("\n").splitlines()
        self.assertTrue(body)
        for line in body:
            self.assertTrue(line.lstrip().startswith("#"), line)

    def test_the_shipped_demonstration_does_not_name_the_repair_it_probes(self):
        # A demonstration containing the answer would measure leakage.
        lowered = probe.FEW_SHOT_DEMONSTRATION.casefold()
        for forbidden in ("ignorecase", "(?i)", ".lower()", "re.compile", "qdp"):
            self.assertNotIn(forbidden, lowered)

    def test_code_in_a_demonstration_is_refused(self):
        with self.assertRaises(ValueError):
            self.generator(ScriptedBackend([""]),
                           extra_directive="# fine\nreturn 'command'\n")

    def test_fim_markers_in_a_demonstration_are_refused(self):
        with self.assertRaises(ValueError):
            self.generator(ScriptedBackend([""]), extra_directive="# <MID> here\n")

    def test_a_non_string_demonstration_is_refused(self):
        with self.assertRaises(ValueError):
            self.generator(ScriptedBackend([""]), extra_directive=["# no"])

    def test_the_default_is_no_demonstration_at_all(self):
        self.assertEqual(FIMPatchGenerator(ScriptedBackend([""])).extra_directive, "")

    def test_blank_lines_around_a_demonstration_are_tolerated(self):
        generator = self.generator(ScriptedBackend([""]),
                                   extra_directive="\n\n# one line\n\n")
        self.assertEqual(generator.extra_directive, "# one line\n")


# --------------------------------------------------------------------------
# the arms themselves
# --------------------------------------------------------------------------

class ArmTests(unittest.TestCase):
    def test_every_arm_named_in_the_default_list_exists(self):
        args = probe.parse_args(["--run-dir", ".", "--symbol", "x",
                                 "--output", "out", "--repo-cache-dir", "."])
        for name in args.arms.split(","):
            self.assertIn(name, probe.ARMS)

    def test_the_control_arm_changes_nothing_about_the_prompt(self):
        self.assertEqual(probe.ARMS["control"], {})

    def test_only_the_fewshot_arms_carry_a_demonstration(self):
        for name, overrides in probe.ARMS.items():
            self.assertEqual("extra_directive" in overrides, "fewshot" in name, name)

    def test_the_narrowed_arms_lower_the_narrowing_threshold(self):
        for name in ("narrowed", "narrowed-fewshot"):
            self.assertEqual(probe.ARMS[name]["large_function_strategy"],
                             "issue_statement")
            self.assertLess(probe.ARMS[name]["large_function_line_threshold"], 80)

    def test_an_unknown_arm_is_rejected_before_any_model_call(self):
        with self.assertRaises(SystemExit):
            probe.parse_args(["--run-dir", ".", "--symbol", "x", "--output", "o",
                              "--repo-cache-dir", ".", "--arms", "control,invented"])

    def test_the_probe_widens_sampling_by_default(self):
        args = probe.parse_args(["--run-dir", ".", "--symbol", "x", "--output", "o",
                                 "--repo-cache-dir", "."])
        self.assertGreater(args.temperature, 0.2)
        self.assertGreater(args.candidates, 2)

    def test_a_candidate_count_the_generator_would_refuse_is_caught_here(self):
        with self.assertRaises(SystemExit):
            probe.parse_args(["--run-dir", ".", "--symbol", "x", "--output", "o",
                              "--repo-cache-dir", ".", "--candidates", "13"])


class GroundTruthIsolationTests(unittest.TestCase):
    def test_the_probe_strips_reference_fields_from_the_ticket_it_reads(self):
        loaded = dict(TICKET, patch="diff --git a b", test_patch="tests",
                      fail_to_pass='["t"]', pass_to_pass='["u"]', hints_text="hint")
        clean = probe.sanitize_ticket(loaded)
        for field in ("patch", "test_patch", "fail_to_pass", "pass_to_pass", "hints_text"):
            self.assertNotIn(field, clean)
        self.assertEqual(clean["title"], TICKET["title"])

    def test_success_markers_are_recorded_but_never_put_in_a_prompt(self):
        prompts = ExtraDirectiveTests().run_once()
        self.assertNotIn("IGNORECASE", prompts[0])


if __name__ == "__main__":
    unittest.main()
