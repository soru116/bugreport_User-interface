"""The rewritten prompt: issue brief first, source, instruction last.

Why this layout exists. In the previous one the repair instruction sat at prompt
line 91 and 110 lines of ordinary source code followed it before the generation
point, so the most probable continuation was ordinary source code -- and that is
what came back: 42.7% of all candidates were copies of the original body, and 0
of 32 capability-probe candidates ever acted on the directive. A base (non-
instruct) Code Llama gives us recency and nothing else, so the instruction has
to be the last thing it reads.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fim_patch.generator import (  # noqa: E402
    FIMPatchGenerator, PROMPT_LAYOUTS, clean_issue_text, compact_docstring,
    current_implementation_block, fim_prompt_version, function_span, git,
    issue_brief, issue_sections, localization_guidance, repair_directive, task_block)
from tests.test_fim import FakeBackend  # noqa: E402


class CleanIssueTextTests(unittest.TestCase):
    def test_template_html_comments_are_removed(self):
        text = ("<!-- Please fill in the template below.\n"
                "     Do not delete these lines. -->\n"
                "### Description\n"
                "It crashes.\n")
        cleaned = clean_issue_text(text)
        self.assertNotIn("fill in the template", cleaned)
        self.assertIn("It crashes.", cleaned)

    def test_an_environment_section_is_dropped_up_to_the_next_heading(self):
        text = ("### Description\n"
                "It crashes.\n"
                "### Versions\n"
                "Linux-5.15, Python 3.10.9, astropy 5.2\n"
                "### Expected behavior\n"
                "It should not crash.\n")
        cleaned = clean_issue_text(text)
        self.assertNotIn("astropy 5.2", cleaned)
        self.assertIn("It crashes.", cleaned)
        self.assertIn("It should not crash.", cleaned)

    def test_runs_of_blank_lines_collapse(self):
        self.assertEqual(clean_issue_text("a\n\n\n\n\nb"), "a\n\nb")

    def test_carriage_returns_never_survive(self):
        self.assertNotIn("\r", clean_issue_text("a\r\nb\rc"))

    def test_a_shell_comment_in_a_traceback_is_not_a_heading(self):
        # "# cat file" inside a reproduction block matched the markdown heading
        # pattern, so everything after it was filed under a new section and the
        # reproduction came out empty.
        text = "### How to Reproduce\n# cat test.qdp\nread serr 1 2\n"
        self.assertEqual(
            issue_sections(clean_issue_text(text))["reproduce"].splitlines(),
            ["# cat test.qdp", "read serr 1 2"])

    def test_a_heading_inside_a_code_fence_does_not_start_a_section(self):
        text = ("### How to Reproduce\n"
                "```\n"
                "## not a heading\n"
                "run()\n"
                "```\n")
        self.assertIn("## not a heading",
                      issue_sections(clean_issue_text(text))["reproduce"])

    def test_an_environment_block_inside_a_fence_is_not_dropped(self):
        text = ("### Description\n"
                "```\n"
                "### Versions\n"
                "still code\n"
                "```\n")
        self.assertIn("still code", clean_issue_text(text))


class IssueSectionTests(unittest.TestCase):
    TEXT = ("### Description\n"
            "ascii.qdp assumes upper case.\n"
            "### Expected behavior\n"
            "It should read the file.\n"
            "### How to Reproduce\n"
            "Table.read('test.qdp')\n"
            "### Actual behavior\n"
            "ValueError: Unrecognized QDP line\n")

    def test_each_field_lands_in_its_own_bucket(self):
        got = issue_sections(self.TEXT)
        self.assertEqual(got["expected"], "It should read the file.")
        self.assertEqual(got["reproduce"], "Table.read('test.qdp')")
        self.assertEqual(got["actual"], "ValueError: Unrecognized QDP line")
        self.assertIn("assumes upper case", got["rest"])

    def test_a_bold_heading_counts_as_a_heading(self):
        got = issue_sections("**Expected behaviour**\nno crash\n")
        self.assertEqual(got["expected"], "no crash")

    def test_text_with_no_headings_is_all_rest(self):
        got = issue_sections("just a paragraph")
        self.assertEqual(got["rest"], "just a paragraph")
        self.assertEqual(got["expected"], "")


class CompactDocstringTests(unittest.TestCase):
    BODY = ('    """Read a table from a QDP file.\n'
            '\n'
            '    Parameters\n'
            '    ----------\n'
            '    qdp_file : str\n'
            '        input qdp file\n'
            '    names : list\n'
            '        column names\n'
            '\n'
            '    Returns\n'
            '    -------\n'
            '    table : Table\n'
            '    """\n'
            '    if table_id is None:\n'
            '        table_id = 0\n'
            '    return table_id\n')

    def test_the_summary_survives_and_the_parameter_table_does_not(self):
        got = compact_docstring(self.BODY)
        self.assertIn("Read a table from a QDP file.", got)
        self.assertNotIn("input qdp file", got)
        self.assertNotIn("column names", got)

    def test_the_code_after_the_docstring_is_untouched(self):
        got = compact_docstring(self.BODY)
        self.assertIn("    if table_id is None:", got)
        self.assertIn("    return table_id", got)

    def test_the_reader_is_told_how_much_was_dropped(self):
        self.assertIn("further docstring lines omitted", compact_docstring(self.BODY))

    def test_an_orphaned_numpydoc_heading_is_not_kept_as_the_summary(self):
        # max_lines=3 would otherwise keep summary, blank, "Parameters" -- a
        # section heading with its table removed.
        self.assertNotIn("Parameters", compact_docstring(self.BODY))

    def test_a_body_without_a_docstring_is_returned_unchanged(self):
        body = "    return 1\n"
        self.assertEqual(compact_docstring(body), body)

    def test_a_one_line_docstring_is_returned_unchanged(self):
        body = '    """Short."""\n    return 1\n'
        self.assertEqual(compact_docstring(body), body)

    def test_a_short_docstring_loses_nothing(self):
        body = '    """Line one.\n\n    Line two.\n    """\n    return 1\n'
        self.assertEqual(compact_docstring(body), body)

    def test_an_unterminated_docstring_is_returned_unchanged(self):
        body = '    """Never closed\n    more text\n'
        self.assertEqual(compact_docstring(body), body)


class IssueBriefTests(unittest.TestCase):
    TICKET = {
        "title": "ascii.qdp assumes QDP commands are upper case",
        "description": ("ascii.qdp assumes QDP commands are upper case\n"
                        "### Description\n"
                        "Commands must be READ SERR.\n"
                        "### Expected behavior\n"
                        "The file should read into a Table.\n"
                        "### How to Reproduce\n"
                        "Table.read('test.qdp', format='ascii.qdp')\n"
                        "### Versions\n"
                        "astropy 5.2, Python 3.10.9\n"),
    }

    def brief(self, **over):
        ticket = dict(self.TICKET)
        ticket.update(over)
        return issue_brief(ticket)

    def test_the_three_fields_a_repair_needs_are_labelled(self):
        brief = self.brief()
        self.assertIn("# --- Expected behaviour", brief)
        self.assertIn("# --- Reproduce ---", brief)
        self.assertIn("# --- Actual behaviour / error", brief)

    def test_expected_behaviour_comes_before_other_details(self):
        brief = self.brief()
        self.assertLess(brief.index("Expected behaviour"), brief.index("Other details"))

    def test_a_field_the_issue_omits_is_named_as_omitted(self):
        # 2 of the 5 development tickets state no expected behaviour at all.
        brief = self.brief()
        self.assertIn("Not stated.", brief)

    def test_environment_details_never_reach_the_prompt(self):
        self.assertNotIn("astropy 5.2", self.brief())

    def test_the_title_is_not_repeated_in_the_body(self):
        self.assertEqual(self.brief().count(self.TICKET["title"]), 1)

    def test_a_field_duplicated_across_keys_is_rendered_once(self):
        brief = self.brief(problem_statement=self.TICKET["description"])
        self.assertEqual(brief.count("Commands must be READ SERR."), 1)

    def test_ground_truth_fields_cannot_enter_through_this_block(self):
        brief = self.brief(patch="SECRET GOLD PATCH", test_patch="SECRET TESTS",
                           fail_to_pass=["test_secret"], hints_text="SECRET HINT")
        for secret in ("SECRET GOLD PATCH", "SECRET TESTS", "test_secret", "SECRET HINT"):
            self.assertNotIn(secret, brief)

    def test_an_empty_ticket_is_refused(self):
        with self.assertRaises(ValueError):
            issue_brief({})

    def test_the_budget_cuts_other_details_before_expected_behaviour(self):
        ticket = dict(self.TICKET)
        ticket["description"] = ticket["description"].replace(
            "Commands must be READ SERR.",
            "\n".join(f"filler line {i}" for i in range(400)))
        brief = issue_brief(ticket, max_chars=900)
        self.assertLessEqual(len(brief), 1100)
        self.assertIn("The file should read into a Table.", brief)

    def test_every_line_is_a_comment(self):
        for line in self.brief().splitlines():
            self.assertTrue(line.startswith("#"), line)

    def test_structured_fields_keep_their_meaning_without_markdown(self):
        brief = issue_brief({"title": "crash", "steps_to_reproduce": "run(1)",
                             "expected_behavior": "return 2", "actual_behavior": "returns 0",
                             "error_message": "ValueError: bad"})
        self.assertIn("--- Reproduce ---\n# run(1)", brief)
        self.assertIn("--- Expected behaviour ---\n# return 2", brief)
        self.assertIn("--- Actual behaviour / error ---\n# returns 0", brief)
        self.assertIn("ValueError: bad", brief)
        self.assertNotIn("Not stated.", brief)

    def test_structured_duplicate_does_not_repeat_expected_text(self):
        brief = issue_brief({"description": "### Expected behavior\nreturn 2",
                             "expected_behavior": "return 2"})
        self.assertEqual(brief.count("return 2"), 1)

    def test_reproduction_and_assertions_are_not_shortened(self):
        code = "```python\nhtml = '<!-- literal input -->'\nassert parse(html) == []\n```"
        brief = issue_brief({"description": "### Description\ncrash\n### How to reproduce\n" + code})
        for line in code.splitlines():
            self.assertIn("# " + line, brief)
        self.assertNotIn("### Description", brief)


class BlockRenderingTests(unittest.TestCase):
    BODY = "    out = []\n    for row in rows:\n        out.append(row)\n    return out\n"

    def test_the_current_implementation_is_commented_at_the_body_indent(self):
        block = current_implementation_block(self.BODY, "    ", "render")
        for line in block.splitlines():
            self.assertTrue(line.startswith("    #"), line)
        self.assertIn("out.append(row)", block)

    def test_a_fim_marker_in_the_body_is_refused(self):
        with self.assertRaises(ValueError):
            current_implementation_block("    return '<MID>'\n", "    ", "render")

    def test_an_empty_body_renders_nothing(self):
        self.assertEqual(current_implementation_block("", "    ", "render"), "")

    def test_the_task_block_ends_by_saying_what_to_write(self):
        block = task_block("# do the thing\n", "    ", "render")
        self.assertTrue(block.rstrip().endswith(
            "# Write the corrected body of render on the next line and nothing else."))

    def test_the_task_block_names_a_statement_in_narrowed_scope(self):
        block = task_block("# do the thing\n", "        ", "render",
                           replacement_scope="issue_statement")
        self.assertIn("one replacement statement or branch", block)

    def test_the_task_block_is_all_comments_at_the_given_indent(self):
        for line in task_block("# a\n\n# b\n", "        ", "f").splitlines():
            self.assertTrue(line.startswith("        #"), line)


class GuidanceTests(unittest.TestCase):
    ROW = {"file_path": "app.py", "symbol_qualified_name": "render"}

    def test_a_single_symbol_drops_the_delegation_sentence(self):
        # strict_top1 lists exactly one symbol, and the sentence then asked the
        # model to compare it against an empty set of alternatives.
        got = localization_guidance([self.ROW], 1)
        self.assertNotIn("delegates", got)
        self.assertIn("# 1. app.py::render [selected repair target]", got)

    def test_two_symbols_keep_it(self):
        other = {"file_path": "helpers.py", "symbol_qualified_name": "clean"}
        self.assertIn("delegates", localization_guidance([self.ROW, other], 1))


class PromptVersionTests(unittest.TestCase):
    def test_each_layout_and_scope_has_its_own_label(self):
        labels = {
            fim_prompt_version(prompt_layout=layout,
                               generation_scope=scope,
                               include_original_body=body)
            for layout in PROMPT_LAYOUTS
            for scope in ("function_body", "issue_statement")
            for body in (False, True)
        }
        # 3 per layout: statement scope collapses the include_original_body pair.
        self.assertEqual(len(labels), 9)

    def test_the_legacy_layout_has_a_distinct_contract_fix_label(self):
        self.assertEqual(
            fim_prompt_version(prompt_layout="legacy", include_original_body=True),
            "issue-plus-before-after-localization-psm-v5-scope-contract-v1")
        self.assertEqual(
            fim_prompt_version(prompt_layout="legacy"),
            "issue-plus-localization-psm-v3-scope-contract-v1")
        self.assertEqual(
            fim_prompt_version(prompt_layout="legacy",
                               generation_scope="issue_statement"),
            "issue-plus-local-statement-psm-v6-scope-contract-v1")


class AssembledPromptTests(unittest.TestCase):
    SOURCE = (
        "import re\n"
        "\n"
        "\n"
        "def unrelated_neighbour():\n"
        "    return re.compile('x')\n"
        "\n"
        "\n"
        "def render(rows, sep=' '):\n"
        '    """Join rows.\n'
        "\n"
        "    Parameters\n"
        "    ----------\n"
        "    rows : list\n"
        "        the rows\n"
        '    """\n'
        "    out = []\n"
        "    for row in rows:\n"
        "        out.append(str(row))\n"
        "    return sep.join(out)\n"
        "\n"
        "\n"
        "def trailing():\n"
        "    return 'suffix side'\n"
    )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        (self.repo / "app.py").write_bytes(self.SOURCE.encode("utf-8"))
        git(self.repo, "init")
        git(self.repo, "config", "core.autocrlf", "false")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=T", "-c", "user.email=t@example.invalid",
            "commit", "-m", "base")
        base = git(self.repo, "rev-parse", "HEAD").decode().strip()
        self.row = {"file_path": "app.py", "symbol_qualified_name": "render",
                    "start_line": 8, "end_line": 19}
        self.loc = {"base_commit": base, "confidence_level": "high",
                    "should_manual_review": False, "recommend_patch_generation": True,
                    "stage3_ranked_symbols": [self.row]}
        self.ticket = {
            "base_commit": base,
            "title": "render keeps blank rows",
            "description": ("### Description\n"
                            "Blank rows are joined too.\n"
                            "### Expected behavior\n"
                            "render should drop blank rows.\n"
                            "### Versions\n"
                            "Python 3.10.9\n"),
            "patch": "SECRET GOLD PATCH", "fail_to_pass": ["test_secret"],
            "hints_text": "SECRET HINT",
        }

    def prompt_for(self, **kwargs):
        seen = {}

        class Recorder(FakeBackend):
            def infill(self, prefix, suffix, *, seed, max_new_tokens=None):
                seen["prefix"], seen["suffix"] = prefix, suffix
                return super().infill(prefix, suffix, seed=seed,
                                      max_new_tokens=max_new_tokens)

        # A replacement that keeps the body it replaces: an answer short enough
        # to trip check_body_retention would be rejected for that reason, which
        # tells us nothing about the prompt.
        backend = Recorder([
            '    """Join rows.\n'
            "\n"
            "    Parameters\n"
            "    ----------\n"
            "    rows : list\n"
            "        the rows\n"
            '    """\n'
            "    out = []\n"
            "    for row in rows:\n"
            "        if not row:\n"
            "            continue\n"
            "        out.append(str(row))\n"
            "    return sep.join(out)\n"
        ] * 4)
        result = FIMPatchGenerator(backend, candidates=1, **kwargs).generate(
            self.ticket, self.loc, self.repo)
        return seen, result

    def test_the_instruction_is_the_last_thing_before_the_generation_point(self):
        seen, _ = self.prompt_for(include_original_body=True)
        prompt = seen["prefix"]
        self.assertTrue(prompt.rstrip().endswith(
            "# Write the corrected body of render on the next line and nothing else."),
            prompt[-300:])

    def test_signature_hole_and_instruction_agree_in_both_layouts(self):
        for layout in PROMPT_LAYOUTS:
            with self.subTest(layout=layout):
                seen, result = self.prompt_for(
                    include_original_body=True, research_mode=True,
                    target_policy="related_targets_top5", prompt_layout=layout)
                self.assertTrue(result["span"]["include_signature"])
                self.assertIn("including its signature and body", seen["prefix"])
                self.assertNotIn("no signature line", seen["prefix"])
                if layout == "task_last":
                    self.assertIn("Write the complete corrected function render", seen["prefix"])

    def test_body_hole_still_forbids_a_signature(self):
        seen, result = self.prompt_for(include_original_body=True)
        self.assertFalse(result["span"]["include_signature"])
        self.assertIn("no signature line", seen["prefix"])

    def test_statement_scope_takes_precedence_over_signature_flag(self):
        directive = repair_directive({}, {}, replacement_scope="issue_statement",
                                     include_signature=True)
        tail = task_block(directive, "    ", "render",
                          replacement_scope="issue_statement", include_signature=True)
        self.assertIn("one replacement statement or branch", tail)
        self.assertNotIn("including its signature", tail)

    def test_no_source_code_sits_between_the_instruction_and_the_hole(self):
        # This is the defect the rewrite exists to fix: 110 lines of ordinary
        # code used to follow the directive.
        seen, _ = self.prompt_for(include_original_body=True)
        tail = seen["prefix"].split("# ===== WHAT TO WRITE BELOW =====")[1]
        for line in tail.splitlines():
            self.assertTrue(not line.strip() or line.lstrip().startswith("#"), line)

    def test_the_prompt_still_ends_the_function_signature_before_the_hole(self):
        seen, _ = self.prompt_for(include_original_body=True)
        self.assertIn("def render(rows, sep=' '):", seen["prefix"])

    def test_the_issue_brief_comes_before_the_source(self):
        seen, _ = self.prompt_for(include_original_body=True)
        prompt = seen["prefix"]
        self.assertLess(prompt.index("===== ISSUE REPORT"),
                        prompt.index("def unrelated_neighbour"))

    def test_the_body_being_replaced_is_shown_as_comments_only(self):
        seen, _ = self.prompt_for(include_original_body=True)
        for line in seen["prefix"].splitlines():
            if "out.append(str(row))" in line:
                self.assertTrue(line.lstrip().startswith("#"), line)

    def test_the_docstring_parameter_table_is_dropped_from_the_shown_body(self):
        seen, _ = self.prompt_for(include_original_body=True)
        tail = seen["prefix"].split("===== CURRENT IMPLEMENTATION")[1]
        self.assertNotIn("the rows", tail)
        self.assertIn("Join rows.", tail)

    def test_the_saving_comes_from_boilerplate_not_from_the_layout_itself(self):
        # Honest accounting: the task block costs about 380 characters, so on a
        # short issue with no docstring the new prompt is LONGER. The saving is
        # in what gets deleted -- template comments, environment sections, and
        # numpydoc parameter tables. On the real astropy-14365 prompt that is
        # 7541 -> 6615 characters; on a toy fixture it is not.
        self.ticket["description"] += (
            "### Versions\n" + "\n".join(f"package-{i}==1.0.{i}" for i in range(80)) + "\n")
        new, _ = self.prompt_for(include_original_body=True)
        old, _ = self.prompt_for(include_original_body=True, prompt_layout="legacy")
        self.assertLess(len(new["prefix"]), len(old["prefix"]))
        self.assertNotIn("package-40==1.0.40", new["prefix"])
        self.assertIn("package-40==1.0.40", old["prefix"])

    def test_ground_truth_stays_out_of_both_layouts(self):
        for layout in PROMPT_LAYOUTS:
            seen, _ = self.prompt_for(include_original_body=True, prompt_layout=layout)
            for secret in ("SECRET GOLD PATCH", "test_secret", "SECRET HINT"):
                self.assertNotIn(secret, seen["prefix"] + seen["suffix"])

    def test_environment_details_stay_out_of_the_new_layout(self):
        seen, _ = self.prompt_for(include_original_body=True)
        self.assertNotIn("Python 3.10.9", seen["prefix"])

    def test_the_layout_is_recorded_on_the_result_and_in_the_prompt_version(self):
        _, result = self.prompt_for(include_original_body=True)
        self.assertEqual(result["prompt_layout"], "task_last")
        self.assertEqual(result["prompt_version"],
                         "issue-brief-before-task-last-psm-v7-scope-contract-v1-issue-compact-v1")

    def test_legacy_reproduces_the_published_label(self):
        _, result = self.prompt_for(include_original_body=True, prompt_layout="legacy")
        self.assertEqual(result["prompt_layout"], "legacy")
        self.assertEqual(result["prompt_version"],
                         "issue-plus-before-after-localization-psm-v5-scope-contract-v1")

    def test_legacy_still_puts_the_directive_ahead_of_the_source(self):
        seen, _ = self.prompt_for(include_original_body=True, prompt_layout="legacy")
        prompt = seen["prefix"]
        self.assertIn("# AFTER (fixed):", prompt)
        self.assertLess(prompt.index("# AFTER (fixed):"),
                        prompt.index("def unrelated_neighbour"))

    def test_the_patch_is_still_built_from_the_real_source_not_the_prompt(self):
        # The comment tail exists only in the prompt. If it leaked into
        # reconstruction the patch would insert comments into the repository.
        _, result = self.prompt_for(include_original_body=True)
        self.assertEqual(result["patch_status"], "generated")
        self.assertNotIn("WHAT TO WRITE BELOW", result["patch"])
        self.assertNotIn("CURRENT IMPLEMENTATION", result["patch"])

    def test_an_unknown_layout_is_refused(self):
        with self.assertRaises(ValueError):
            FIMPatchGenerator(FakeBackend([]), prompt_layout="whatever")

    def test_the_local_context_crop_keeps_the_instruction_last(self):
        seen, _ = self.prompt_for(include_original_body=True, local_context=True)
        self.assertTrue(seen["prefix"].rstrip().endswith("nothing else."))


if __name__ == "__main__":
    unittest.main()
