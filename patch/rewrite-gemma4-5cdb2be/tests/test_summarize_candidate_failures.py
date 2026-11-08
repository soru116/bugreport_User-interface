"""Bucketing every candidate this project has generated."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import summarize_candidate_failures as summary  # noqa: E402

#: Every rejection reason the generator, the backend and the batch runner can
#: attach to a candidate. A reason that lands in "other" is a reason the table
#: cannot report, so this list is the guard against the taxonomy silently going
#: stale when a new gate is added.
KNOWN_REASONS = {
    "no_op": "copied_verbatim",
    "no_executable_change: only comments or docstrings differ": "comments_only",
    "statement_reorder_only: executable statements only changed order": "reordered_only",
    "body_shrank: 1 of 60 lines kept, below the 50% retention floor": "deleted_too_much",
    "undefined_name: units": "undefined_name",
    "unreachable_code: 1 new statement(s) after the function always returns":
        "unreachable_code",
    "statement_scope_escape: expected exactly one replacement statement":
        "escaped_its_scope",
    "Generated body changed syntax outside the target function": "escaped_its_scope",
    "incomplete_statement: generated statement does not parse": "incomplete_output",
    "incomplete_body: generation produced no body lines": "incomplete_output",
    "missing_visible_eot": "incomplete_output",
    "truncated_or_incomplete": "incomplete_output",
    "rewrite_truncated: reply hit the token cap": "incomplete_output",
    "Middle does not join suffix at a line boundary": "did_not_join_the_suffix",
    "rewrite_declined: model judged the target needs no change":
        "model_declined_to_change_it",
    "missing_tolerance_aware_change": "missed_the_required_change",
    "non_code_output": "non_code_output",
    "duplicate": "duplicate_of_another_candidate",
    "validation_failed": "failed_validation",
    "context_budget_exceeded: conservative UTF-8 byte bound": "prompt_too_large",
    "timed out": "timed_out",
    "HTTP Error 500: Internal Server Error": "backend_error",
}


class BucketTests(unittest.TestCase):
    def test_every_known_reason_has_its_own_bucket(self):
        for reason, expected in KNOWN_REASONS.items():
            self.assertEqual(summary.bucket_for({"status": "rejected", "reason": reason}),
                             expected, reason)

    def test_no_known_reason_falls_through_to_other(self):
        for reason in KNOWN_REASONS:
            self.assertNotEqual(
                summary.bucket_for({"status": "rejected", "reason": reason}), "other")

    def test_a_candidate_that_cleared_the_gates_is_not_bucketed_by_reason(self):
        self.assertEqual(
            summary.bucket_for({"status": "generated", "reason": ""}), "passed_gates")

    def test_an_unrecognised_reason_is_visible_rather_than_dropped(self):
        self.assertEqual(
            summary.bucket_for({"status": "rejected", "reason": "brand new gate"}), "other")

    def test_every_bucket_appears_in_the_display_order(self):
        for bucket in set(KNOWN_REASONS.values()) | {"passed_gates", "other"}:
            self.assertIn(bucket, summary.BUCKET_ORDER, bucket)

    def test_the_no_output_buckets_are_exactly_the_three_without_candidate_text(self):
        self.assertEqual(summary.NO_MODEL_OUTPUT,
                         {"prompt_too_large", "timed_out", "backend_error"})
        for bucket in summary.NO_MODEL_OUTPUT:
            self.assertIn(bucket, summary.BUCKET_ORDER)


def write_run(root: Path, name: str, *, config: dict, tickets: dict) -> Path:
    run = root / name
    (run / "tickets").mkdir(parents=True)
    (run / "run_config.json").write_text(json.dumps(config), encoding="utf-8")
    for ticket, candidates in tickets.items():
        ticket_dir = run / "tickets" / ticket
        ticket_dir.mkdir()
        (ticket_dir / "candidates.json").write_text(json.dumps(candidates), encoding="utf-8")
        (ticket_dir / "result.json").write_text(
            json.dumps({"patch_status": "no_valid_candidate"}), encoding="utf-8")
    return run


def candidate(reason="", status="rejected", stop_reason="visible_eot", **extra):
    return dict({"status": status, "reason": reason, "stop_reason": stop_reason}, **extra)


class ScanRunTests(unittest.TestCase):
    def test_a_run_is_counted_across_all_of_its_tickets(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = write_run(Path(tmp), "r1", config={"model": "m", "temperature": 0.8},
                            tickets={"t1": [candidate("no_op"), candidate(status="generated")],
                                     "t2": [candidate("timed out")]})
            scanned = summary.scan_run(run)
        self.assertEqual(scanned["tickets"], 2)
        self.assertEqual(scanned["candidates"], 3)
        self.assertEqual(scanned["buckets"],
                         {"copied_verbatim": 1, "passed_gates": 1, "timed_out": 1})
        self.assertEqual(scanned["config"]["temperature"], 0.8)

    def test_a_run_config_without_a_temperature_says_so_rather_than_guessing_silently(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = write_run(Path(tmp), "r1", config={"model": "m"},
                            tickets={"t1": [candidate("no_op")]})
            scanned = summary.scan_run(run)
        self.assertIn("unrecorded", str(scanned["config"]["temperature"]))

    def test_a_directory_without_tickets_is_skipped_not_counted_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "not-a-run").mkdir()
            self.assertIsNone(summary.scan_run(Path(tmp) / "not-a-run"))

    def test_stop_reasons_are_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = write_run(Path(tmp), "r1", config={},
                            tickets={"t1": [candidate(stop_reason="length"),
                                            candidate(stop_reason="length"),
                                            candidate(stop_reason="visible_eot")]})
            scanned = summary.scan_run(run)
        self.assertEqual(scanned["stop_reasons"], {"length": 2, "visible_eot": 1})

    def test_trimming_and_overrun_flags_are_carried_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = write_run(Path(tmp), "r1", config={},
                            tickets={"t1": [candidate(body_trimmed=True,
                                                      overran_past_stop=True),
                                            candidate()]})
            scanned = summary.scan_run(run)
        self.assertEqual(scanned["overran_past_stop"], 1)
        self.assertEqual(scanned["body_trimmed"], 1)


class ScanProbeTests(unittest.TestCase):
    def probe_file(self, root: Path) -> Path:
        (root / "probe_config.json").write_text(json.dumps(
            {"model": "codellama:7b-code", "temperature": 0.8, "candidates": 2,
             "num_ctx": 16384, "ticket_id": "demo-1"}), encoding="utf-8")
        (root / "probe_arms.json").write_text(json.dumps([
            {"arm": "control", "patch_status": "generated",
             "settings": {"large_function_strategy": "full_body", "max_body_lines": 160},
             "analysis": {"per_candidate": [candidate(status="generated"),
                                            candidate("no_op")]}},
            {"arm": "narrowed", "patch_status": "no_valid_candidate",
             "settings": {"large_function_strategy": "issue_statement"},
             "analysis": {"per_candidate": [candidate("incomplete_statement: nope")]}},
        ]), encoding="utf-8")
        return root / "probe_arms.json"

    def test_each_arm_becomes_its_own_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = summary.scan_probe(self.probe_file(Path(tmp)))
        self.assertEqual([r["run"].split(":")[-1] for r in rows], ["control", "narrowed"])
        self.assertEqual(rows[0]["buckets"], {"passed_gates": 1, "copied_verbatim": 1})

    def test_probe_rows_are_marked_as_oracle_localization(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = summary.scan_probe(self.probe_file(Path(tmp)))
        for row in rows:
            self.assertTrue(row["oracle_localization"])
            self.assertIn("oracle", row["config"]["target_policy"])

    def test_a_missing_probe_file_yields_nothing_rather_than_raising(self):
        self.assertEqual(summary.scan_probe(Path("/nonexistent/probe_arms.json")), [])


class TableTests(unittest.TestCase):
    def build(self):
        runs = [{
            "run": "r1", "config": {k: "-" for k in summary.CONFIG_COLUMNS},
            "tickets": 1, "candidates": 5,
            "buckets": {"passed_gates": 1, "copied_verbatim": 2,
                        "timed_out": 1, "prompt_too_large": 1},
            "stop_reasons": {"length": 5}, "overran_past_stop": 0, "body_trimmed": 0,
            "per_ticket": [],
        }]
        totals = {"runs": 1, "tickets": 1, "candidates": 5,
                  "buckets": dict(runs[0]["buckets"]),
                  "stop_reasons": {"length": 5},
                  "overran_past_stop": 0, "body_trimmed": 0,
                  "no_model_output": 2, "model_candidates": 3}
        return runs, totals

    def test_percentages_exclude_the_slots_with_no_model_output(self):
        runs, totals = self.build()
        table = summary.markdown(runs, totals, summary.used_buckets(runs))
        # 1 of 3, not 1 of 5
        self.assertIn("33.3%", table)
        self.assertNotIn("20.0%", table)

    def test_the_no_output_rows_are_labelled_instead_of_given_a_share(self):
        runs, totals = self.build()
        table = summary.markdown(runs, totals, summary.used_buckets(runs))
        for line in table.splitlines():
            if line.startswith("| timed_out") or line.startswith("| prompt_too_large"):
                self.assertIn("未取得模型輸出", line)

    def test_the_table_states_that_passing_gates_is_not_resolved(self):
        runs, totals = self.build()
        table = summary.markdown(runs, totals, summary.used_buckets(runs))
        self.assertIn("resolved", table)

    def test_oracle_rows_are_flagged_in_the_table(self):
        runs, totals = self.build()
        runs[0]["oracle_localization"] = True
        table = summary.markdown(runs, totals, summary.used_buckets(runs))
        self.assertIn("oracle", table)

    def test_only_buckets_that_occurred_are_shown(self):
        runs, _ = self.build()
        self.assertNotIn("unreachable_code", summary.used_buckets(runs))
        self.assertEqual(summary.used_buckets(runs)[0], "passed_gates")


class DeterministicOutputTests(unittest.TestCase):
    """The same inputs must produce the same bytes on either platform."""

    def test_the_markdown_report_is_written_crlf_like_its_siblings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out" / "table.md"
            summary.write_text(path, "a\nb\n", newline="\r\n")
            self.assertEqual(path.read_bytes(), b"a\r\nb\r\n")

    def test_the_json_detail_is_written_lf_because_tools_read_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "detail.json"
            summary.write_text(path, '{"a": 1}\n', newline="\n")
            self.assertEqual(path.read_bytes(), b'{"a": 1}\n')

    def test_a_missing_parent_directory_is_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deep" / "deeper" / "x.md"
            summary.write_text(path, "x", newline="\n")
            self.assertTrue(path.is_file())


if __name__ == "__main__":
    unittest.main()
