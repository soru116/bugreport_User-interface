"""Tests for scripts/run_stage4_batch.py.

Fully offline: the pipeline is a double, so no model, no Ollama and no SWE-bench
repositories are needed. The three properties worth testing are the three the
script is responsible for -- ground-truth isolation, one failure not ending the
run, and a summary that never overstates what was measured.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for candidate in (REPO_ROOT, REPO_ROOT / "src"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from fim_patch.backend import OllamaFIM, OllamaRewrite  # noqa: E402
from scripts.run_stage4_batch import (  # noqa: E402
    DEFAULT_TEMPERATURE,
    HISTORICAL_DEFAULTS,
    TICKET_GROUND_TRUTH_FIELDS,
    build_parser,
    build_record,
    build_run_config,
    classify_rejection,
    load_previous_records,
    make_generator,
    run_one_ticket,
    sanitize_ticket,
    select_tickets,
    summarize,
    validate_resume_config,
    write_predictions,
)


def generated_result(*, rank=1, fallback=False, body_lines=12):
    return {
        "patch_status": "generated",
        "apply_status": "passed",
        "validation_status": "not_run",
        "generation_mode": "fim",
        "generation_strategy": "fim_psm",
        "prompt_version": "issue-plus-localization-psm-v3",
        "backend_kind": "ollama-fim-v3",
        "target_policy": "first_supported_ast",
        "target_rank": rank,
        "target_fallback_used": fallback,
        "expand_truncated_end": True,
        "max_body_lines": 120,
        "research_mode": False,
        "span": {"body_lines": body_lines},
        "bug_location": {"file": "app.py", "function": "Widget.render"},
        "target_resolution": [
            {"rank": 1, "status": "rejected", "reason": "Unsupported or ambiguous function symbol"},
            {"rank": 2, "status": "selected"},
        ] if fallback else [{"rank": 1, "status": "selected"}],
        "candidates": [{"candidate_id": 1, "status": "generated"},
                       {"candidate_id": 2, "status": "rejected"}],
        "patch": "--- a/app.py\n+++ b/app.py\n@@\n-old\n+new\n",
        "model": {"model": "codellama:7b-instruct"},
        "explanation": "FIM body rebuilt",
    }


class FakePipeline:
    """Records the ticket it was handed, so isolation can be asserted."""

    def __init__(self, result=None, raises=None):
        self.result = result or generated_result()
        self.raises = raises
        self.seen_tickets = []
        self.seen_repos = []
        self.seen_localizations = []

    def run(self, ticket, repo, localization):
        self.seen_tickets.append(ticket)
        self.seen_repos.append(repo)
        self.seen_localizations.append(localization)
        if self.raises:
            raise self.raises
        return {"confidence_level": "high", "stage3_ranked_symbols": []}, self.result


class SanitizationTests(unittest.TestCase):
    def test_every_ground_truth_field_is_removed(self) -> None:
        ticket = {"ticket_id": "a__b-1", "repo": "a/b", "base_commit": "abc",
                  "problem_statement": "it breaks", "description": "d",
                  "patch": "GOLD", "test_patch": "T", "gold_patch": "G",
                  "fail_to_pass": ["t1"], "pass_to_pass": ["t2"], "hints_text": "h"}
        clean = sanitize_ticket(ticket)
        for field in TICKET_GROUND_TRUTH_FIELDS:
            self.assertNotIn(field, clean)
        self.assertEqual(clean["problem_statement"], "it breaks")
        self.assertEqual(clean["base_commit"], "abc")

    def test_original_ticket_is_not_mutated(self) -> None:
        ticket = {"ticket_id": "x", "patch": "GOLD"}
        sanitize_ticket(ticket)
        self.assertEqual(ticket["patch"], "GOLD")

    def test_fields_are_removed_not_blanked(self) -> None:
        """A blanked key is still a key the localizer could branch on."""

        clean = sanitize_ticket({"ticket_id": "x", "patch": "GOLD"})
        self.assertEqual(list(clean), ["ticket_id"])


class RejectionBucketTests(unittest.TestCase):
    def test_the_three_predicted_failure_modes_each_get_a_bucket(self) -> None:
        self.assertEqual(classify_rejection("Unsupported or ambiguous function symbol"),
                         "unsupported_symbol")
        self.assertEqual(classify_rejection("Localization coordinates disagree with AST"),
                         "coordinates_disagree")
        self.assertEqual(classify_rejection("Body spans 300 lines, above max_body_lines=120"),
                         "body_too_large")

    def test_unknown_reason_falls_into_other(self) -> None:
        self.assertEqual(classify_rejection("something new"), "other")


class BuildRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ticket = {"ticket_id": "astropy__astropy-13073", "repo": "astropy/astropy",
                       "base_commit": "43ee5806"}

    def test_generated_result_is_flattened(self) -> None:
        record = build_record(self.ticket, result=generated_result(rank=2, fallback=True),
                              location={"confidence_level": "high"}, duration_s=1.234)
        self.assertEqual(record["ticket_id"], "astropy__astropy-13073")
        self.assertEqual(record["patch_status"], "generated")
        self.assertEqual(record["apply_status"], "passed")
        self.assertEqual(record["generation_mode"], "fim")
        self.assertEqual(record["generation_strategy"], "fim_psm")
        self.assertEqual(record["prompt_version"], "issue-plus-localization-psm-v3")
        self.assertEqual(record["backend_kind"], "ollama-fim-v3")
        self.assertEqual(record["target_rank"], 2)
        self.assertTrue(record["target_fallback_used"])
        self.assertEqual(record["body_lines"], 12)
        self.assertEqual(record["target_symbol"], "Widget.render")
        self.assertEqual(record["rejection_buckets"], ["unsupported_symbol"])
        self.assertEqual(record["candidate_generated_count"], 1)
        self.assertEqual(record["patch_line_count"], 2)
        self.assertEqual(record["model"], "codellama:7b-instruct")
        self.assertEqual(record["confidence_level"], "high")
        self.assertEqual(record["duration_s"], 1.234)

    def test_patch_line_count_ignores_file_headers(self) -> None:
        result = generated_result()
        result["patch"] = "--- a/x.py\n+++ b/x.py\n@@\n-a\n+b\n+c\n"
        self.assertEqual(build_record(self.ticket, result=result)["patch_line_count"], 3)

    def test_error_record_carries_the_message_and_no_status(self) -> None:
        record = build_record(self.ticket, error="snapshot: base_commit is unavailable")
        self.assertIn("base_commit is unavailable", record["error"])
        self.assertEqual(record["patch_status"], "")
        self.assertIsNone(record["target_rank"])


class SummarizeTests(unittest.TestCase):
    def make(self, **kw):
        record = build_record({"ticket_id": kw.pop("tid", "t")}, **kw)
        return record

    def test_counts_split_errors_from_attempts(self) -> None:
        records = [
            self.make(tid="a", result=generated_result(rank=1)),
            self.make(tid="b", result=generated_result(rank=3, fallback=True)),
            self.make(tid="c", result={"patch_status": "blocked_invalid_context",
                                       "target_resolution": [
                                           {"status": "rejected",
                                            "reason": "Localization coordinates disagree with AST"}]}),
            self.make(tid="d", error="boom"),
        ]
        summary = summarize(records)
        self.assertEqual(summary["total_tickets"], 4)
        self.assertEqual(summary["errored"], 1)
        self.assertEqual(summary["attempted"], 3)
        self.assertEqual(summary["generated"], 2)
        self.assertEqual(summary["generated_at_rank_1"], 1)
        self.assertEqual(summary["generated_via_fallback"], 1)
        self.assertEqual(summary["patch_status_counts"]["blocked_invalid_context"], 1)
        self.assertEqual(summary["target_rejection_buckets"]["coordinates_disagree"], 1)
        self.assertAlmostEqual(summary["generated_rate"], round(2 / 3, 4))

    def test_official_resolved_is_always_none(self) -> None:
        summary = summarize([self.make(result=generated_result())])
        self.assertIsNone(summary["official_resolved"])
        self.assertIn("NOT a repair rate", summary["note"])

    def test_empty_input_does_not_divide_by_zero(self) -> None:
        self.assertEqual(summarize([])["generated_rate"], 0.0)

    def test_validation_plausible_counted_separately_from_generated(self) -> None:
        plausible = generated_result()
        plausible["validation_status"] = "plausible"
        summary = summarize([self.make(tid="a", result=plausible),
                             self.make(tid="b", result=generated_result())])
        self.assertEqual(summary["generated"], 2)
        self.assertEqual(summary["validation_plausible"], 1)

    def test_rewrite_modes_and_contract_failures_are_counted_separately(self) -> None:
        rewrite = generated_result()
        rewrite.update(generation_mode="rewrite", generation_strategy="rewrite_function",
                       backend_kind="ollama-rewrite-v1")
        rewrite["candidates"] = [
            {"status": "rejected", "reason": "rewrite_no_change"},
            {"status": "rejected",
             "reason": "rewrite_contract_violation: markdown_fence"},
            {"status": "rejected", "reason": "signature_changed: arguments"},
            {"status": "generated"},
        ]
        summary = summarize([
            self.make(tid="rewrite", result=rewrite),
            self.make(tid="fim", result=generated_result()),
        ])
        self.assertEqual(summary["generation_mode_counts"], {"fim": 1, "rewrite": 1})
        self.assertEqual(summary["rewrite_no_change"], 1)
        self.assertEqual(summary["rewrite_contract_violation"], 1)
        self.assertEqual(summary["signature_changed"], 1)
        self.assertEqual(summary["generated"], 2)
        self.assertEqual(summary["applied"], 2)
        self.assertIsNone(summary["official_resolved"])


class GenerationModeTests(unittest.TestCase):
    def parse(self, *extra):
        parser = build_parser()
        args = parser.parse_args([
            "--tickets", "tickets.jsonl", "--localizations", "localizations.jsonl",
            "--output", "run", *extra
        ])
        from fim_patch.cli import validate_generation_args
        return validate_generation_args(args, parser)

    def test_batch_factory_uses_the_selected_backend(self) -> None:
        fim = make_generator(self.parse())
        rewrite = make_generator(self.parse(
            "--generation-mode", "rewrite", "--model", "codellama:7b-instruct"))
        self.assertIsInstance(fim.backend, OllamaFIM)
        self.assertEqual(fim.generation_mode, "fim")
        self.assertIsInstance(rewrite.backend, OllamaRewrite)
        self.assertEqual(rewrite.generation_mode, "rewrite")

    def test_local_fim_factory_and_resume_keep_mode_distinct(self):
        args = self.parse('--local-fim', '--prompt-layout', 'compact_fim', '--repair-rounds', '2', '--candidates', '1')
        repair = make_generator(args)
        self.assertTrue(repair.generator.local_fim)
        self.assertTrue(repair.generator.fixed_signature_body)
        self.assertTrue(repair.generator.helper_contracts)
        config = build_run_config(args, [])
        self.assertEqual(config['local_fim'], 'local-fim-v1')
        self.assertTrue(config['prompt_version'].endswith('-local-fim-v1'))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'run_config.json'
            path.write_text(json.dumps(config), encoding='utf-8')
            with self.assertRaises(ValueError):
                validate_resume_config(path, build_run_config(self.parse('--prompt-layout', 'compact_fim'), []))

    def test_local_fim_rejects_conflicting_options_before_backend(self):
        for options in ([], ['--adaptive-repair'], ['--multi-function-repair'],
                        ['--fixed-body-extraction'], ['--max-targets','2'],
                        ['--target-policy','related_targets_top5']):
            with self.assertRaises(SystemExit):
                self.parse('--local-fim', '--prompt-layout', 'compact_fim' if options else 'task_last', *options)

    def test_multi_function_factory_records_implicit_helpers_and_call_cap(self):
        from fim_patch.multi_function import MultiFunctionRepairGenerator
        args = self.parse('--multi-function-repair', '--fixed-signature-body',
                          '--prompt-layout', 'compact_fim', '--candidates', '1', '--repair-rounds', '3')
        repair = make_generator(args)
        self.assertIsInstance(repair, MultiFunctionRepairGenerator)
        self.assertTrue(repair.generator.helper_implementations)
        config = build_run_config(args, [])
        self.assertEqual(config['generation_call_limit'], 3)
        self.assertEqual(config['helper_contracts'], 'same-file-helper-contract-v1')
        self.assertTrue(config['prompt_version'].endswith('-multi-function-fim-v1'))
        self.assertNotIn('adaptive_repair', config)

    def test_fixed_body_extraction_wired_and_recorded_for_resume(self):
        args = self.parse('--multi-function-repair', '--fixed-signature-body', '--fixed-body-extraction',
                          '--prompt-layout', 'compact_fim', '--candidates', '1', '--repair-rounds', '3')
        self.assertTrue(make_generator(args).generator.fixed_body_extraction)
        config = build_run_config(args, [])
        self.assertEqual(config['fixed_body_extraction'], 'fixed-body-boundary-v1')
        self.assertEqual(config['multi_function_validation_schedule'], 'after_plan')
        with self.assertRaises(SystemExit):
            self.parse('--fixed-body-extraction')

    def test_multi_function_rejects_incompatible_cli_before_generation(self):
        with self.assertRaises(SystemExit):
            self.parse('--multi-function-repair')
        with self.assertRaises(SystemExit):
            self.parse('--multi-function-repair', '--adaptive-repair', '--fixed-signature-body',
                       '--prompt-layout', 'compact_fim', '--candidates', '1')

    def test_resume_rejects_a_generation_mode_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "run_config.json"
            path.write_text(json.dumps({"generation_mode": "fim", "model": "base"}),
                            encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "generation_mode"):
                validate_resume_config(
                    path, {"generation_mode": "rewrite", "model": "instruct"})

    def test_run_config_omits_fim_transport_meaning_in_rewrite_mode(self) -> None:
        args = self.parse("--generation-mode", "rewrite",
                          "--model", "codellama:7b-instruct")
        args.tickets = Path("tickets.jsonl")
        config = build_run_config(args, [{"ticket_id": "t"}])
        self.assertEqual(config["generation_mode"], "rewrite")
        self.assertEqual(config["generation_strategy"], "rewrite_function")
        self.assertEqual(config["prompt_version"], "issue-plus-full-function-rewrite-v2")
        self.assertEqual(config["backend_kind"], "ollama-rewrite-v1")
        self.assertIsNone(config["fim_transport"])


def localization(*, confidence="high", manual_review=False, recommend=True, rows=None):
    return {"confidence_level": confidence, "should_manual_review": manual_review,
            "recommend_patch_generation": recommend,
            "stage3_ranked_symbols": rows if rows is not None else []}


class ProductionGateRecordTests(unittest.TestCase):
    """The generator blocks unless all three gate fields agree, so the record
    must carry all three -- reading confidence alone hides two thirds of it."""

    def record(self, **kw):
        return build_record({"ticket_id": "t"}, location=localization(**kw))

    def test_all_three_conditions_are_captured(self) -> None:
        record = self.record()
        self.assertEqual(record["confidence_level"], "high")
        self.assertFalse(record["should_manual_review"])
        self.assertTrue(record["recommend_patch_generation"])
        self.assertTrue(record["production_gate_eligible"])

    def test_each_condition_alone_can_fail_the_gate(self) -> None:
        self.assertFalse(self.record(confidence="medium")["production_gate_eligible"])
        self.assertFalse(self.record(manual_review=True)["production_gate_eligible"])
        self.assertFalse(self.record(recommend=False)["production_gate_eligible"])

    def test_rank_one_shape_is_recorded_even_when_the_gate_blocks(self) -> None:
        """The blocked run still tells us what Stage-3 ranked first."""

        record = build_record({"ticket_id": "t"}, location=localization(
            confidence="medium",
            rows=[{"symbol_kind": "class", "start_line": 143, "end_line": 222}]))
        self.assertEqual(record["rank1_symbol_kind"], "class")
        self.assertEqual(record["rank1_line_span"], 80)
        self.assertFalse(record["production_gate_eligible"])

    def test_missing_stage3_rows_leave_the_shape_blank(self) -> None:
        record = build_record({"ticket_id": "t"}, location=localization())
        self.assertEqual(record["rank1_symbol_kind"], "")
        self.assertIsNone(record["rank1_line_span"])


class GateSummaryTests(unittest.TestCase):
    def rows(self, kind="class", span=(143, 222)):
        return [{"symbol_kind": kind, "start_line": span[0], "end_line": span[1]}]

    def test_summary_reports_how_many_would_pass_the_production_gate(self) -> None:
        records = [
            build_record({"ticket_id": "a"}, location=localization(confidence="medium",
                                                                  manual_review=True, recommend=False,
                                                                  rows=self.rows())),
            build_record({"ticket_id": "b"}, location=localization(confidence="low",
                                                                   manual_review=True, recommend=False,
                                                                   rows=self.rows("function", (252, 331)))),
            build_record({"ticket_id": "c"}, location=localization(rows=self.rows("function", (10, 20)))),
        ]
        summary = summarize(records)
        self.assertEqual(summary["production_gate_eligible"], 1)
        self.assertEqual(summary["confidence_level_counts"],
                         {"high": 1, "low": 1, "medium": 1})
        self.assertEqual(summary["rank1_symbol_kind_counts"], {"class": 1, "function": 2})
        self.assertEqual(summary["rank1_line_span_median"], 80)

    def test_research_mode_is_surfaced_in_the_summary(self) -> None:
        """A research-mode number must never be readable without that label."""

        result = generated_result()
        result["research_mode"] = True
        summary = summarize([build_record({"ticket_id": "a"}, result=result)])
        self.assertIs(summary["research_mode"], True)

    def test_mixed_research_flags_are_reported_as_a_list_not_collapsed(self) -> None:
        strict, relaxed = generated_result(), generated_result()
        strict["research_mode"], relaxed["research_mode"] = False, True
        summary = summarize([build_record({"ticket_id": "a"}, result=strict),
                             build_record({"ticket_id": "b"}, result=relaxed)])
        self.assertEqual(summary["research_mode"], [False, True])


class RunOneTicketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        self._git("init")
        self._git("config", "core.autocrlf", "false")
        self._git("add", ".")
        self._git("-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-m", "base")
        self.base = subprocess.run(["git", "-C", str(self.source), "rev-parse", "HEAD"],
                                   capture_output=True, text=True, check=True).stdout.strip()
        self.ticket = {"ticket_id": "org__proj-7", "repo": "org/proj",
                       "base_commit": self.base, "problem_statement": "broken",
                       "local_repo_path": str(self.source),
                       "patch": "GOLD", "fail_to_pass": ["t"]}
        self.localization = {"ticket_id": "org__proj-7", "repo": "org/proj",
                             "base_commit": self.base, "source_file_sha256": {},
                             "stage3_ranked_symbols": []}

    def _git(self, *args):
        subprocess.run(["git", "-C", str(self.source), *args], capture_output=True, check=True)

    def call(self, pipeline, name="out"):
        return run_one_ticket(
            self.ticket, pipeline=pipeline, localization=self.localization,
            repo_cache_dir=self.root / "cache",
            snapshot_cache_dir=self.root / "snapshots",
            ticket_output_dir=self.root / name,
            clone_missing=False, fetch_missing_commits=False,
        )

    def test_pipeline_never_receives_ground_truth(self) -> None:
        pipeline = FakePipeline()
        self.call(pipeline)
        self.assertEqual(len(pipeline.seen_tickets), 1)
        for field in TICKET_GROUND_TRUTH_FIELDS:
            self.assertNotIn(field, pipeline.seen_tickets[0])
        self.assertEqual(pipeline.seen_tickets[0]["problem_statement"], "broken")

    def test_pipeline_receives_the_external_localization_record(self) -> None:
        pipeline = FakePipeline()
        self.call(pipeline)
        self.assertEqual(pipeline.seen_localizations, [self.localization])

    def test_snapshot_is_detached_at_base_commit_and_clean(self) -> None:
        pipeline = FakePipeline()
        self.call(pipeline)
        snapshot = Path(pipeline.seen_repos[0])
        head = subprocess.run(["git", "-C", str(snapshot), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(head, self.base)
        status = subprocess.run(["git", "-C", str(snapshot), "status", "--porcelain"],
                                capture_output=True, text=True, check=True).stdout
        self.assertEqual(status.strip(), "")
        self.assertNotEqual(snapshot.resolve(), self.source.resolve())

    def test_successful_run_writes_the_output_directory(self) -> None:
        record = self.call(FakePipeline())
        out = self.root / "out"
        self.assertEqual(record["patch_status"], "generated")
        self.assertEqual(record["output_dir"], str(out))
        for name in ("ticket.json", "localization.json", "result.json", "evaluation.json",
                     "selected.diff", "predictions.jsonl"):
            self.assertTrue((out / name).exists(), name)

    def test_written_ticket_json_has_no_ground_truth(self) -> None:
        """The run directory is shared with teammates; it must not carry the answer."""

        self.call(FakePipeline())
        written = json.loads((self.root / "out" / "ticket.json").read_text(encoding="utf-8"))
        for field in TICKET_GROUND_TRUTH_FIELDS:
            self.assertNotIn(field, written)

    def test_pipeline_failure_becomes_a_record_not_an_exception(self) -> None:
        record = self.call(FakePipeline(raises=RuntimeError("ollama is down")))
        self.assertIn("ollama is down", record["error"])
        self.assertEqual(record["patch_status"], "")
        self.assertFalse((self.root / "out").exists())

    def test_missing_base_commit_is_reported_as_a_snapshot_error(self) -> None:
        self.ticket["base_commit"] = "0" * 40
        record = self.call(FakePipeline())
        self.assertTrue(record["error"].startswith("snapshot: "))

    def test_missing_repository_is_reported_not_raised(self) -> None:
        del self.ticket["local_repo_path"]
        record = self.call(FakePipeline())
        self.assertTrue(record["error"].startswith("snapshot: "))

    def test_snapshot_is_reused_across_tickets_at_the_same_commit(self) -> None:
        first = FakePipeline()
        self.call(first, name="out1")
        second = FakePipeline()
        self.call(second, name="out2")
        self.assertEqual(first.seen_repos[0], second.seen_repos[0])


class SelectionAndResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = [{"ticket_id": f"t{i}"} for i in range(5)] + [{"no_id": 1}]

    def test_rows_without_an_id_are_dropped(self) -> None:
        self.assertEqual(len(select_tickets(self.rows, allowlist=None, limit=None)), 5)

    def test_allowlist_and_limit_combine(self) -> None:
        selected = select_tickets(self.rows, allowlist={"t1", "t3", "t4"}, limit=2)
        self.assertEqual([r["ticket_id"] for r in selected], ["t1", "t3"])

    def test_instance_id_is_accepted_as_the_identifier(self) -> None:
        selected = select_tickets([{"instance_id": "x-1"}], allowlist={"x-1"}, limit=None)
        self.assertEqual(len(selected), 1)

    def test_previous_records_are_keyed_by_ticket_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "records.jsonl"
            path.write_text('{"ticket_id": "a", "patch_status": "generated"}\n'
                            '{"ticket_id": "b", "error": "x"}\n', encoding="utf-8")
            previous = load_previous_records(path)
            self.assertEqual(set(previous), {"a", "b"})
            self.assertEqual(previous["a"]["patch_status"], "generated")

    def test_missing_records_file_is_empty_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(load_previous_records(Path(temp) / "absent.jsonl"), {})


class WritePredictionsTests(unittest.TestCase):
    def test_only_generated_tickets_enter_the_harness_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            good = root / "good"
            good.mkdir()
            (good / "predictions.jsonl").write_text(
                json.dumps({"instance_id": "a", "model_name_or_path": "model",
                            "model_patch": "diff"}) + "\n", encoding="utf-8")
            blocked = root / "blocked"
            blocked.mkdir()
            (blocked / "predictions.jsonl").write_text(
                json.dumps({"instance_id": "b", "model_patch": ""}) + "\n", encoding="utf-8")
            records = [
                {"patch_status": "generated", "output_dir": str(good)},
                {"patch_status": "blocked_invalid_context", "output_dir": str(blocked)},
                {"patch_status": "generated", "output_dir": ""},
            ]
            out = root / "predictions.jsonl"
            self.assertEqual(write_predictions(out, records, root), 1)
            rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([r["instance_id"] for r in rows], ["a"])
            self.assertEqual(set(rows[0]), {"instance_id", "model_name_or_path", "model_patch"})


class TemperatureTests(unittest.TestCase):
    """The setting four batches turned on and none of them wrote down."""

    def parse(self, *extra):
        parser = build_parser()
        args = parser.parse_args(["--tickets", "tickets.jsonl",
                                  "--localizations", "localizations.jsonl",
                                  "--output", "run", *extra])
        from fim_patch.cli import validate_generation_args
        return validate_generation_args(args, parser)

    def test_the_default_is_the_backend_default_so_past_runs_keep_their_meaning(self):
        self.assertEqual(self.parse().temperature, DEFAULT_TEMPERATURE)
        self.assertEqual(DEFAULT_TEMPERATURE, OllamaFIM.temperature)

    def test_the_chosen_temperature_reaches_the_fim_backend(self):
        generator = make_generator(self.parse("--temperature", "0.8"))
        self.assertEqual(generator.backend.temperature, 0.8)

    def test_the_chosen_temperature_reaches_the_rewrite_backend(self):
        generator = make_generator(self.parse(
            "--generation-mode", "rewrite", "--model", "codellama:7b-instruct",
            "--temperature", "0.8"))
        self.assertIsInstance(generator.backend, OllamaRewrite)
        self.assertEqual(generator.backend.temperature, 0.8)

    def test_the_chosen_temperature_reaches_a_multi_target_run(self):
        generator = make_generator(self.parse(
            "--max-targets", "3", "--target-policy", "strict_top1",
            "--research-mode", "--temperature", "0.8"))
        self.assertEqual(generator.inner.backend.temperature, 0.8)

    def test_run_config_records_it(self):
        config = build_run_config(self.parse("--temperature", "0.8"), [])
        self.assertEqual(config["temperature"], 0.8)

    def test_a_negative_temperature_is_refused_before_any_ticket_runs(self):
        with self.assertRaises(ValueError):
            make_generator(self.parse("--temperature", "-0.1"))

    def test_resume_rejects_a_temperature_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "run_config.json"
            path.write_text(json.dumps(build_run_config(self.parse("--temperature", "0.8"), [])),
                            encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                validate_resume_config(path, build_run_config(self.parse(), []))
            self.assertIn("temperature", str(caught.exception))

    def test_a_run_config_written_before_the_key_existed_still_resumes(self):
        # The 20 runs already on disk have no temperature key. Comparing the whole
        # config would make every one of them refuse to resume over a value they
        # could not have written.
        current = build_run_config(self.parse(), [])
        legacy = {k: v for k, v in current.items() if k != "temperature"}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "run_config.json"
            path.write_text(json.dumps(legacy), encoding="utf-8")
            validate_resume_config(path, current)

    def test_the_historical_default_matches_what_those_runs_actually_used(self):
        self.assertEqual(HISTORICAL_DEFAULTS["temperature"], OllamaFIM.temperature)


if __name__ == "__main__":
    unittest.main()
