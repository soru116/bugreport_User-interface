"""Run the FL -> patch-generation pipeline over a ticket set and aggregate results.

``fim_patch.cli`` runs exactly one ticket against one already-checked-out
repository. This script is the batch layer around it: for every ticket it
materializes a detached snapshot at ``base_commit``, runs the same
``RepairPipeline``, and appends one flat record per ticket to a JSONL file so a
46-ticket run can be counted instead of read by hand.

Three properties this script is responsible for, all of them stated in
``STAGE4_PATCH_GENERATION_IMPLEMENTATION_PLAN_ZH.md``:

1. **Ground-truth isolation (plan section 4.1).** The developer ``patch``,
    ``test_patch``, ``fail_to_pass``, ``pass_to_pass`` and ``hints_text`` are
    stripped from every ticket before it reaches patch generation.
   ``fim_patch.generator.evidence()`` already uses an allowlist; this is the
   second, independent barrier, so neither layer alone is load-bearing.
2. **One failure does not lose the run.** A ticket that raises is recorded with
   its error and the batch continues. ``--resume`` re-reads the JSONL and skips
   tickets already finished.
3. **No number in the summary is a repair rate.** ``patch_status=generated``
   counts generation, not repair. ``official_resolved`` stays ``None`` until the
   official SWE-bench Docker harness has judged the predictions file.

Usage::

    python scripts/run_stage4_batch.py --tickets TICKETS.jsonl --output runs/batch-001 \
        --localizations LOCALIZATIONS.jsonl \
        --target-policy first_supported_ast --local-context --max-body-lines 120
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import sys
import time
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from fim_patch.backend import TRANSPORTS, OllamaFIM  # noqa: E402
from fim_patch.cli import (  # noqa: E402
    DEFAULT_FIM_MODEL,
    DEFAULT_FIM_TRANSPORT,
    make_backend,
    validate_generation_args,
)
from fim_patch.generator import (  # noqa: E402
    GENERATION_MODES,
    PROMPT_LAYOUTS,
    fim_prompt_version,
    LARGE_FUNCTION_STRATEGIES,
    REWRITE_PROMPT_VERSION,
    RESEARCH_ONLY_TARGET_POLICIES,
    TARGET_POLICY_DEPTH,
    FIMPatchGenerator,
)
from fim_patch.multi_target import (  # noqa: E402
    SELECTION_RULES,
    MultiTargetGenerator,
)
from fim_patch.pipeline import RepairPipeline, write_run  # noqa: E402
from fim_patch.localization_handoff import load_localization_map  # noqa: E402
from utils.repo_snapshot import (  # noqa: E402
    RepoSnapshotError,
    materialize_commit_snapshot,
    resolve_repository_path,
)

#: Fields that would leak the answer into localization or the model prompt.
#: Mirrors ``scripts/generate_stage4_patches.py`` on the WP2 branch.
TICKET_GROUND_TRUTH_FIELDS = frozenset(
    {"patch", "test_patch", "gold_patch", "fail_to_pass", "pass_to_pass", "hints_text"}
)

#: Maps a target-resolution rejection message to a countable bucket. Order
#: matters: the first matching fragment wins.
REJECTION_BUCKETS = (
    ("Unsupported or ambiguous function symbol", "unsupported_symbol"),
    ("Localization coordinates disagree with AST", "coordinates_disagree"),
    ("max_body_lines", "body_too_large"),
    ("Inline function bodies", "inline_body"),
    ("Python source files only", "not_python_source"),
    ("Source is missing or outside repository", "source_missing"),
    ("Invalid source path", "source_missing"),
    ("Source hash differs", "source_mismatch"),
    ("Source differs from base_commit", "source_mismatch"),
    ("line endings", "line_endings"),
    ("Source exceeds", "source_too_large"),
    ("Stage-3", "no_stage3_symbols"),
)

#: Every run before 2026-09-29 took the backend default and never wrote it
#: down. Temperature turned out to be the single most consequential setting
#: in the whole experiment series -- four batches concluded "the model only
#: copies" at 0.2, and the capability probe got 1 copy in 16 at 0.8 -- so it
#: is now recorded in run_config.json rather than left to the backend.
DEFAULT_TEMPERATURE = OllamaFIM.temperature

#: What an absent run_config key meant before that key existed. Resume
#: compares the whole config, so without this every pre-existing run would
#: refuse to resume citing a key it could not have written.
HISTORICAL_DEFAULTS = {"temperature": DEFAULT_TEMPERATURE}

DEFAULT_RECORDS_NAME = "stage4_batch_records.jsonl"
DEFAULT_SNAPSHOT_SUBDIR = "_stage4_snapshots"


# --------------------------------------------------------------------------
# input
# --------------------------------------------------------------------------

def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with Path(path).open("r", encoding="utf-8-sig") as handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{number}: invalid JSON: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{number}: expected a JSON object")
            rows.append(row)
    return rows


def load_ticket_id_allowlist(path: Path) -> set[str]:
    text = Path(path).read_text(encoding="utf-8-sig")
    return {line.strip() for line in text.splitlines() if line.strip()}


def ticket_identifier(ticket: dict) -> str:
    return str(ticket.get("ticket_id") or ticket.get("instance_id") or "").strip()


def sanitize_ticket(ticket: dict) -> dict:
    """Return the ticket with every ground-truth field removed.

    Removal, not masking: a placeholder value would still be a value the
    localization stage could key on.
    """

    return {k: v for k, v in ticket.items() if k not in TICKET_GROUND_TRUTH_FIELDS}


def safe_dir_name(value: str) -> str:
    cleaned = "".join(ch if (ch.isalnum() or ch in "-._") else "_" for ch in value)
    return cleaned or "unnamed"


# --------------------------------------------------------------------------
# per-ticket record
# --------------------------------------------------------------------------

def classify_rejection(reason: str) -> str:
    for fragment, bucket in REJECTION_BUCKETS:
        if fragment in reason:
            return bucket
    return "other"


def build_record(ticket: dict, *, result=None, location=None, error: str = "",
                 duration_s: float = 0.0, output_dir: str = "", skipped: str = "") -> dict:
    """Flatten one ticket's outcome into a single countable row."""

    record = {
        "ticket_id": ticket_identifier(ticket),
        "repo": ticket.get("repo", ""),
        "base_commit": ticket.get("base_commit", ""),
        "output_dir": output_dir,
        "duration_s": round(duration_s, 3),
        "error": error,
        "skipped": skipped,
        "patch_status": "",
        "apply_status": "",
        "validation_status": "",
        "generation_mode": "",
        "generation_strategy": "",
        "prompt_version": "",
        "backend_kind": "",
        "target_policy": "",
        "target_rank": None,
        "target_fallback_used": None,
        "expand_truncated_end": None,
        "max_body_lines": None,
        "research_mode": None,
        "body_lines": None,
        "target_file": "",
        "target_symbol": "",
        "rejection_buckets": [],
        "rejection_reasons": [],
        "candidate_count": 0,
        "candidate_generated_count": 0,
        "patch_line_count": 0,
        "model": "",
        "confidence_level": "",
        "should_manual_review": None,
        "recommend_patch_generation": None,
        "production_gate_eligible": None,
        "rank1_symbol_kind": "",
        "rank1_line_span": None,
        # How each candidate's generation ended, and whether its body had to be
        # cut structurally. A body the model closed itself and a body we cut are
        # different evidence and are never counted as one population.
        "candidate_stop_reasons": [],
        "candidate_body_trimmed": 0,
        # Trimmed even though the model said it had finished: it stopped only
        # after running into the next function.
        "candidate_overran_past_stop": 0,
        # Why individual candidates were thrown away. Without this the summary
        # showed only which TARGET was rejected, so a gate firing on a candidate
        # could only be seen by opening that ticket's candidates.json by hand.
        "candidate_reasons": [],
        # One entry per target when --max-targets scans several; empty otherwise.
        "target_attempts": [],
        "targets_attempted": 0,
        "targets_generated": 0,
    }
    if location:
        record["confidence_level"] = location.get("confidence_level", "")
        record["should_manual_review"] = location.get("should_manual_review")
        record["recommend_patch_generation"] = location.get("recommend_patch_generation")
        # The generator's production gate is all three conditions at once.
        record["production_gate_eligible"] = (
            location.get("confidence_level") == "high"
            and location.get("should_manual_review") is False
            and location.get("recommend_patch_generation") is True)
        rows = location.get("stage3_ranked_symbols") or []
        if rows and isinstance(rows[0], dict):
            record["rank1_symbol_kind"] = str(rows[0].get("symbol_kind", ""))
            start, end = rows[0].get("start_line"), rows[0].get("end_line")
            if isinstance(start, int) and isinstance(end, int):
                record["rank1_line_span"] = end - start + 1
    if not result:
        return record

    record.update(
        official_resolved=result.get('official_resolved'),
        repair_rounds_used=result.get('repair_rounds_used', 1),
        repair_stop_reason=result.get('repair_stop_reason'),
        patch_status=result.get("patch_status", ""),
        apply_status=result.get("apply_status", ""),
        validation_status=result.get("validation_status", ""),
        generation_mode=result.get("generation_mode", ""),
        generation_strategy=result.get("generation_strategy", ""),
        prompt_version=result.get("prompt_version", ""),
        backend_kind=result.get("backend_kind", ""),
        target_policy=result.get("target_policy", ""),
        target_rank=result.get("target_rank"),
        target_fallback_used=result.get("target_fallback_used"),
        targets_attempted=result.get("targets_attempted") or 0,
        targets_generated=result.get("targets_generated") or 0,
        expand_truncated_end=result.get("expand_truncated_end"),
        max_body_lines=result.get("max_body_lines"),
        research_mode=result.get("research_mode"),
    )
    span = result.get("span") or {}
    record["body_lines"] = span.get("body_lines")
    bug_location = result.get("bug_location") or {}
    record["target_file"] = bug_location.get("file", "")
    record["target_symbol"] = bug_location.get("function", "")

    reasons = [str(entry.get("reason", "")) for entry in result.get("target_resolution") or []
               if entry.get("status") == "rejected" and entry.get("reason")]
    record["rejection_reasons"] = reasons
    record["rejection_buckets"] = [classify_rejection(reason) for reason in reasons]

    candidates = result.get("candidates") or []
    if result.get('fixed_body_extraction'):
        record['fixed_body_extraction'] = result['fixed_body_extraction']
        record['body_extractions'] = [c.get('body_extraction') for h in result.get('repair_history', [result])
                                      for c in h.get('candidates', []) if c.get('body_extraction')]
    if result.get('local_fim'):
        record['local_fim']=result['local_fim']
        record['local_fim_selection']=result.get('local_fim_selection')
        if result.get('local_fim_expressions'):
            record['local_fim_expressions']=result['local_fim_expressions']
            record['expression_fim']=result.get('expression_fim')
        if result.get('fim_scope'):
            for key in ('fim_scope','fim_scope_version','fim_output_ceiling','native_output_budget'):
                record[key]=result.get(key)
        if result.get('safe_body_feedback'):
            record['safe_body_feedback']=result['safe_body_feedback']
            record['source_contract_probe']=result.get('source_contract_probe')
            record['source_contract_checks']=[c['source_contract_check'] for h in result.get('repair_history',[result])
                for c in h.get('candidates',[]) if c.get('source_contract_check')]
        record['prompt_context']=result.get('prompt_context')
    if result.get('multi_function_repair'):
        record['multi_function_repair'] = result['multi_function_repair']
        record['repair_plan'] = result.get('repair_plan', [])
        record['changed_functions'] = result.get('changed_functions', [])
        record['generation_call_limit'] = result.get('generation_call_limit')
        record['validation_schedule'] = result.get('validation_schedule')
    if result.get('adaptive_repair'):
        record['adaptive_repair'] = result['adaptive_repair']
        record['adaptive_decisions'] = result.get('adaptive_decisions', [])
        record['adaptive_generation_limit'] = result.get('adaptive_generation_limit')
    if result.get('operation_contract_preview'):
        record['operation_contract_preview'] = result['operation_contract_preview']
        record['operation_contract_diagnostic_version'] = result['operation_contract_diagnostic_version']
        contract_candidates = [c for attempt in (result.get('repair_history') or [result])
                               for c in attempt.get('candidates',[])]
        record['operation_contract_statuses'] = [c['operation_contract_diagnostic']['status']
            for c in contract_candidates if c.get('operation_contract_diagnostic')]
        record['operation_contract_previews_ready'] = sum(
            (c.get('operation_contract_preview') or {}).get('status')=='ready' for c in contract_candidates)
    if result.get('contract_feedback'):
        record['contract_feedback']=result['contract_feedback']
        record['contract_retry']=result['contract_retry']
    from fim_patch.generation_records import annotate_generation
    annotate_generation(result)
    for key in ('generation_status','generated_before_validation','static_candidate_count','generation_accounting_version',
                'logic_accounting_version','logical_static_candidate_count','logic_unclassified_candidate_count',
                'logic_accounting_complete','repair_progress_version','repair_progress'):
        if key not in result:
            continue
        record[key]=result[key]
    record["candidate_count"] = len(candidates)
    record["candidate_accepted_count"] = sum(
        1 for c in candidates if c.get("status") == "generated")
    record["candidate_generated_count"] = result["static_candidate_count"]
    record["candidate_stop_reasons"] = [str(c.get("stop_reason")) for c in candidates
                                        if c.get("stop_reason")]
    record["candidate_body_trimmed"] = sum(1 for c in candidates if c.get("body_trimmed"))
    record["candidate_overran_past_stop"] = sum(
        1 for c in candidates if c.get("overran_past_stop"))
    record["candidate_reasons"] = [str(c.get("reason")) for c in candidates if c.get("reason")]
    record["target_attempts"] = [
        {key: attempt.get(key) for key in
         ("rank", "file", "function", "symbol_kind", "patch_status", "selected")}
        for attempt in (result.get("target_attempts") or [])
        if isinstance(attempt, dict)]
    patch = result.get("patch") or ""
    record["patch_line_count"] = sum(
        1 for line in patch.splitlines() if line.startswith(("+", "-")) and not line.startswith(("+++", "---")))
    model = result.get("model")
    if isinstance(model, dict):
        record["model"] = str(model.get("model", ""))
    elif model:
        record["model"] = str(model)
    if not record["error"] and result.get("explanation") and result.get("patch_status") != "generated":
        record["error"] = ""  # an explained block is not an error; keep them distinguishable
    record["explanation"] = str(result.get("explanation", ""))
    return record


# --------------------------------------------------------------------------
# summary
# --------------------------------------------------------------------------

def summarize(records: list[dict]) -> dict:
    done = [r for r in records if not r.get("skipped")]
    errored = [r for r in done if r.get("error")]
    attempted = [r for r in done if not r.get("error")]

    status_counts: dict[str, int] = {}
    for record in attempted:
        status = record.get("patch_status") or "unknown"
        status_counts[status] = status_counts.get(status, 0) + 1

    bucket_counts: dict[str, int] = {}
    for record in attempted:
        for bucket in record.get("rejection_buckets") or []:
            bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1

    rank_counts: dict[str, int] = {}
    for record in attempted:
        rank = record.get("target_rank")
        if rank is not None:
            rank_counts[str(rank)] = rank_counts.get(str(rank), 0) + 1

    confidence_counts: dict[str, int] = {}
    for record in attempted:
        level = record.get("confidence_level") or "unknown"
        confidence_counts[level] = confidence_counts.get(level, 0) + 1

    kind_counts: dict[str, int] = {}
    for record in attempted:
        kind = record.get("rank1_symbol_kind") or "unknown"
        kind_counts[kind] = kind_counts.get(kind, 0) + 1

    candidate_reason_counts: dict[str, int] = {}
    for record in attempted:
        for reason in record.get("candidate_reasons") or []:
            # Reasons carry measured detail ("12 of 62 lines kept"); bucket on
            # the code before the colon so the counts stay readable.
            key = reason.split(":")[0].strip()
            candidate_reason_counts[key] = candidate_reason_counts.get(key, 0) + 1

    stop_counts: dict[str, int] = {}
    for record in attempted:
        for reason in record.get("candidate_stop_reasons") or []:
            stop_counts[reason] = stop_counts.get(reason, 0) + 1

    generation_mode_counts: dict[str, int] = {}
    for record in attempted:
        mode = record.get("generation_mode") or "unknown"
        generation_mode_counts[mode] = generation_mode_counts.get(mode, 0) + 1

    spans = sorted(r["rank1_line_span"] for r in attempted if isinstance(r.get("rank1_line_span"), int))
    research_modes = sorted({bool(r.get("research_mode")) for r in attempted
                             if r.get("research_mode") is not None})

    generated = [r for r in attempted if r.get("generated_before_validation",
                  r.get("patch_status") == "generated")]
    accepted = [r for r in attempted if r.get("patch_status") == "generated"]
    fallback = [r for r in generated if r.get("target_fallback_used")]
    body_lines = sorted(r["body_lines"] for r in attempted if isinstance(r.get("body_lines"), int))

    total_attempted = len(attempted)
    return {
        "total_tickets": len(records),
        "skipped": len(records) - len(done),
        "errored": len(errored),
        "attempted": total_attempted,
        # True only when research_mode bypassed the generator's production gate.
        # Any number below was produced under these flags; do not quote them without it.
        "research_mode": research_modes[0] if len(research_modes) == 1 else research_modes,
        "confidence_level_counts": dict(sorted(confidence_counts.items())),
        "production_gate_eligible": sum(1 for r in attempted if r.get("production_gate_eligible")),
        "rank1_symbol_kind_counts": dict(sorted(kind_counts.items())),
        "rank1_line_span_median": spans[len(spans) // 2] if spans else None,
        "patch_status_counts": dict(sorted(status_counts.items())),
        "generation_mode_counts": dict(sorted(generation_mode_counts.items())),
        # Scanning several targets: how many were tried, and how many of those
        # produced a patch. A ticket counts once in "generated" however many of
        # its targets succeeded, so these two are reported separately.
        "targets_attempted_total": sum(r.get("targets_attempted") or 0 for r in attempted),
        "targets_generated_total": sum(r.get("targets_generated") or 0 for r in attempted),
        "target_rejection_buckets": dict(sorted(bucket_counts.items())),
        # visible_eot: the model closed the infill itself.
        # length: it ran to the token cap and the body was cut at its own
        # structural end. Report the two separately; a run whose bodies were all
        # cut by us is weaker evidence than one the model finished.
        "candidate_stop_reasons": dict(sorted(stop_counts.items())),
        "candidate_bodies_trimmed": sum(r.get("candidate_body_trimmed") or 0 for r in attempted),
        "candidate_overran_past_stop": sum(
            r.get("candidate_overran_past_stop") or 0 for r in attempted),
        "candidate_rejection_reasons": dict(sorted(candidate_reason_counts.items())),
        "rewrite_no_change": candidate_reason_counts.get("rewrite_no_change", 0),
        "rewrite_contract_violation": candidate_reason_counts.get(
            "rewrite_contract_violation", 0),
        "signature_changed": candidate_reason_counts.get("signature_changed", 0),
        "target_rank_counts": dict(sorted(rank_counts.items(), key=lambda kv: int(kv[0]))),
        # Generation rate. NOT a repair rate -- see the note below.
        "generation_accounting_version": "static-candidate-accounting-v1",
        "generated": len(generated),
        "accepted": len(accepted),
        "static_candidates_total": sum(r.get('static_candidate_count',0) for r in attempted),
        "logic_accounting_version": "python-ast-candidate-v1",
        "logical_static_candidates_total": sum(r.get('logical_static_candidate_count',0) for r in attempted),
        "operation_contract_diagnostic_counts": dict(sorted(Counter(
            status for r in attempted for status in r.get('operation_contract_statuses',[])).items())),
        "operation_contract_previews_ready": sum(r.get('operation_contract_previews_ready',0) for r in attempted),
        "contract_review_stop_counts": dict(sorted(Counter(
            r.get('repair_stop_reason','unknown') for r in attempted if r.get('contract_feedback')).items())),
        "logic_unclassified_candidates_total": sum(r.get('logic_unclassified_candidate_count',r.get('static_candidate_count',0)) for r in attempted),
        "applied": sum(1 for r in attempted if r.get("apply_status") == "passed"),
        "generated_rate": round(len(generated) / total_attempted, 4) if total_attempted else 0.0,
        "generated_at_rank_1": len(generated) - len(fallback),
        "generated_via_fallback": len(fallback),
        "validation_plausible": sum(1 for r in generated if r.get("validation_status") == "plausible"),
        "body_lines_median": body_lines[len(body_lines) // 2] if body_lines else None,
        "body_lines_max": body_lines[-1] if body_lines else None,
        "official_resolved": (sum(r.get('official_resolved') is True for r in records)
                              if any(isinstance(r.get('official_resolved'), bool) for r in records) else None),
        'official_evaluated': sum(isinstance(r.get('official_resolved'), bool) for r in records),
        'repair_rounds_total': sum(r.get('repair_rounds_used', 1) for r in records),
        "note": (
            "generated_rate counts patches that passed the static and apply gates. It is NOT a "
            "repair rate. validation_plausible counts configured validator passes. A repair rate "
            "requires official_resolved from the official SWE-bench Docker harness, which this "
            "script runs only when local --repair-official or --repair-cloud-repo is enabled. "
            "accepted retains the historical final-selection count; generated includes test-rejected static candidates."
        ),
    }


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------

def _make_single_generator(args) -> FIMPatchGenerator:
    if args.max_targets > 1:
        return MultiTargetGenerator(
            make_backend(args), max_targets=args.max_targets,
            candidates=args.candidates, seed=args.seed,
            generation_mode=args.generation_mode,
            research_mode=args.research_mode, local_context=args.local_context,
            expand_truncated_end=args.expand_truncated_end,
            max_body_lines=args.max_body_lines,
            large_function_strategy=args.large_function_strategy,
            large_function_line_threshold=args.large_function_line_threshold,
            include_original_body=args.include_original_body,
            min_body_retention=args.min_body_retention,
            prompt_layout=args.prompt_layout,
            safe_function_extraction=getattr(args, "safe_function_extraction", False),
            fixed_signature_body=getattr(args, "fixed_signature_body", False),
            fixed_body_extraction=getattr(args, "fixed_body_extraction", False),
            local_fim=getattr(args,"local_fim",False),
            local_fim_expressions=getattr(args,"local_fim_expressions",False),
            scope_adjustment=getattr(args,"scope_adjustment",False),
            fim_scope=getattr(args,"fim_scope",None),
            fim_output_ceiling=getattr(args,"fim_output_ceiling",None),
            safe_body_feedback=getattr(args,"safe_body_feedback",False),
            operation_contract_preview=getattr(args,"operation_contract_preview",False),
            contract_feedback=getattr(args,"contract_feedback",False),
            domain_adapters=getattr(args,'domain_adapter',()),
            helper_contracts=(getattr(args, "helper_contracts", False) or (getattr(args, "adaptive_repair", False) or getattr(args, "multi_function_repair", False))),
            helper_implementations=(getattr(args, "adaptive_repair", False) or getattr(args, "multi_function_repair", False)),
            selection_rule=args.selection_rule,
            # MultiTargetGenerator forwards **kwargs to the inner generator.
            # Without this the flag is silently inert above --max-targets 1,
            # while the run config still records it as on.
            explicit_argument_edit=args.explicit_argument_edit,
        )
    return FIMPatchGenerator(
        make_backend(args),
        candidates=args.candidates, seed=args.seed,
        generation_mode=args.generation_mode,
        research_mode=args.research_mode, local_context=args.local_context,
        target_policy=args.target_policy,
        expand_truncated_end=args.expand_truncated_end,
        max_body_lines=args.max_body_lines,
        large_function_strategy=args.large_function_strategy,
        large_function_line_threshold=args.large_function_line_threshold,
        include_original_body=args.include_original_body,
        min_body_retention=args.min_body_retention,
        max_related_targets=args.max_related_targets,
        explicit_argument_edit=args.explicit_argument_edit,
        prompt_layout=args.prompt_layout,
        safe_function_extraction=getattr(args, "safe_function_extraction", False),
        fixed_signature_body=getattr(args, "fixed_signature_body", False),
        fixed_body_extraction=getattr(args, "fixed_body_extraction", False),
        local_fim=getattr(args,"local_fim",False),
        local_fim_expressions=getattr(args,"local_fim_expressions",False),
        scope_adjustment=getattr(args,"scope_adjustment",False),
        fim_scope=getattr(args,"fim_scope",None),
        fim_output_ceiling=getattr(args,"fim_output_ceiling",None),
        safe_body_feedback=getattr(args,"safe_body_feedback",False),
        operation_contract_preview=getattr(args,"operation_contract_preview",False),
        contract_feedback=getattr(args,"contract_feedback",False),
        domain_adapters=getattr(args,'domain_adapter',()),
        helper_contracts=(getattr(args, "helper_contracts", False) or (getattr(args, "adaptive_repair", False) or getattr(args, "multi_function_repair", False))),
        helper_implementations=(getattr(args, "adaptive_repair", False) or getattr(args, "multi_function_repair", False)),
    )


def make_generator(args):
    generator = _make_single_generator(args)
    rounds = getattr(args, 'repair_rounds', 1)
    official = getattr(args, 'repair_official', False)
    cloud = getattr(args, 'repair_cloud_repo', None)
    if official and cloud:
        raise ValueError('Choose cloud OR local official tests')
    if rounds == 1 and not official and not cloud and not (getattr(args, "adaptive_repair", False) or getattr(args, "multi_function_repair", False)):
        return generator
    if args.max_targets != 1:
        raise ValueError('iterative repair requires --max-targets 1')
    from fim_patch.iterative import IterativeRepairGenerator, OfficialRepairValidator, require_official_environment
    if official:
        require_official_environment()
    factory = (lambda ticket: OfficialRepairValidator(
        ticket_identifier(ticket), args.output/'official_repair',
        timeout=getattr(args, 'repair_test_timeout', 1800))) if official else None
    if cloud:
        from fim_patch.cloud_official import cloud_factory
        factory = cloud_factory(args, args.output/'official_cloud', timeout=getattr(args, 'repair_test_timeout',1800))
    if getattr(args, 'multi_function_repair', False):
        if getattr(args, 'adaptive_repair', False):
            raise ValueError('Choose either --adaptive-repair or --multi-function-repair')
        from fim_patch.multi_function import MultiFunctionRepairGenerator
        return MultiFunctionRepairGenerator(generator, rounds, args.max_related_targets, factory)
    return IterativeRepairGenerator(generator, rounds, factory, adaptive=getattr(args, "adaptive_repair", False))


def run_one_ticket(ticket, *, pipeline, localization, repo_cache_dir: Path, snapshot_cache_dir: Path,
                   ticket_output_dir: Path, clone_missing: bool, fetch_missing_commits: bool) -> dict:
    """Materialize the snapshot, run the pipeline, write the run, return a record."""

    started = time.monotonic()
    clean = sanitize_ticket(ticket)
    try:
        source_repo = resolve_repository_path(clean, repo_cache_dir=repo_cache_dir,
                                              clone_missing=clone_missing)
        snapshot = materialize_commit_snapshot(
            source_repo, repo=str(clean.get("repo") or ""),
            base_commit=str(clean.get("base_commit") or ""),
            snapshot_cache_dir=snapshot_cache_dir,
            fetch_missing_commits=fetch_missing_commits,
        )
        if not isinstance(localization, dict):
            raise ValueError("Missing external localization record for ticket")
        location, result = pipeline.run(clean, str(snapshot), localization)
    except RepoSnapshotError as exc:
        return build_record(ticket, error=f"snapshot: {exc}", duration_s=time.monotonic() - started)
    except Exception as exc:  # noqa: BLE001 - one ticket must not end the batch
        return build_record(ticket, error=f"{type(exc).__name__}: {exc}",
                            duration_s=time.monotonic() - started)

    try:
        write_run(ticket_output_dir, clean, location, result)
    except Exception as exc:  # noqa: BLE001
        record = build_record(ticket, result=result, location=location,
                              duration_s=time.monotonic() - started)
        record["error"] = f"write_run: {type(exc).__name__}: {exc}"
        return record

    return build_record(ticket, result=result, location=location,
                        duration_s=time.monotonic() - started,
                        output_dir=str(ticket_output_dir))


def load_previous_records(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    return {r["ticket_id"]: r for r in load_jsonl(path) if r.get("ticket_id")}


def write_predictions(path: Path, records: list[dict], run_root: Path) -> int:
    """Merge each generated ticket's predictions.jsonl into one harness input."""

    written = 0
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            if record.get("patch_status") != "generated" or not record.get("output_dir"):
                continue
            source = Path(record["output_dir"]) / "predictions.jsonl"
            if not source.exists():
                continue
            for row in load_jsonl(source):
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                written += 1
    return written


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--multi-function-repair', action='store_true',
                        help='Compose source-related fixed-signature FIM bodies within one total candidate-call limit')
    parser.add_argument('--adaptive-repair', action='store_true',
                        help='Bound helper implementations and reselect public/source-derived targets within the round limit')
    parser.add_argument('--helper-contracts', action='store_true',
                        help='Provide same-file helper signatures and reject provably incompatible changed calls')
    parser.add_argument('--fixed-body-extraction', action='store_true',
                        help='Extract only witnessed complete fixed-signature bodies; preserve raw outputs and all gates')
    parser.add_argument('--local-fim', action='store_true', help='Opt-in bounded AST statement FIM')
    parser.add_argument('--local-fim-expressions', action='store_true', help='With local FIM, retain docstrings and anchor expression holes')
    parser.add_argument('--fim-scope', choices=('expression','statement','body'))
    parser.add_argument('--fim-output-ceiling', type=int)
    parser.add_argument('--safe-body-feedback', action='store_true')
    parser.add_argument('--operation-contract-preview', action='store_true',
                        help='Record contract diagnostics and an unsent FIM preview without changing selection or retries')
    parser.add_argument('--contract-feedback', action='store_true')
    parser.add_argument('--domain-adapter', action='append', choices=('tolerance','conversion'), default=[])
    parser.add_argument('--scope-adjustment', action='store_true', help='Opt-in bounded additive-signature and behavior FIM')
    parser.add_argument('--fixed-signature-body', action='store_true',
                        help='With compact_fim, preserve original signature in Prefix and generate body only')
    parser.add_argument('--safe-function-extraction', action='store_true',
                        help='Extract a complete whole-function FIM output only with a witnessed boundary; retain all gates')
    parser.add_argument("--tickets", required=True, type=Path,
                        help="Ticket JSONL with ticket_id/repo/base_commit and a public description.")
    parser.add_argument("--localizations", required=True, type=Path,
                        help="External frozen FL JSONL (or JSON array), keyed by ticket identifier.")
    parser.add_argument("--output", required=True, type=Path,
                        help="Run directory. Created if absent; reused with --resume.")
    parser.add_argument("--ticket-ids-from", type=Path, default=None,
                        help="Newline-separated ticket ids to restrict the run to.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only the first N selected tickets.")
    parser.add_argument("--resume", action="store_true",
                        help="Skip tickets already present in the records file.")
    parser.add_argument("--repo-cache-dir", type=Path, default=Path("data/repositories"))
    parser.add_argument("--snapshot-cache-dir", type=Path, default=None,
                        help=f"Defaults to <repo-cache-dir>/{DEFAULT_SNAPSHOT_SUBDIR}")
    parser.add_argument("--clone-missing", action="store_true")
    parser.add_argument("--fetch-missing-commits", action="store_true")
    # generator passthrough
    parser.add_argument("--generation-mode", choices=GENERATION_MODES, default="fim")
    parser.add_argument("--model", default=None,
                        help=f"Defaults to {DEFAULT_FIM_MODEL!r} in FIM mode; rewrite "
                             "requires an explicit instruction-model tag")
    parser.add_argument("--fim-transport", choices=TRANSPORTS, default=DEFAULT_FIM_TRANSPORT,
                        help="raw_psm reproduces the pre-fix runs whose markers were "
                             "never parsed as tokens; it does not infill")
    parser.add_argument("--url", default="http://localhost:11434")
    parser.add_argument("--tokenizer-json", type=Path, default=None,
                        help="Local Qwen or Code Llama tokenizer; verified against installed model before token budgeting")
    from fim_patch.cloud_official import add_cloud_arguments
    add_cloud_arguments(parser)
    parser.add_argument('--repair-rounds', type=int, choices=(1, 2, 3), default=1)
    parser.add_argument('--repair-official', action='store_true',
                        help='Run official Docker tests inside repair rounds; requires installed SWE-bench and Docker')
    parser.add_argument('--repair-test-timeout', type=int, default=1800)
    parser.add_argument("--candidates", type=int, default=4)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--num-ctx", type=int, default=4096)
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE,
                        help="Sampling temperature. Kept at the historical default so no past run changes meaning; the capability probe found 0.8 removes almost all verbatim copying (see reports/STAGE4_CAPABILITY_PROBE_ZH.md), so a new baseline passes it explicitly rather than inheriting it")
    parser.add_argument("--timeout", type=int, default=180,
                        help="Seconds allowed per model call. A 13B model regenerating a "
                             "60-line body exceeds the 180 s default and every candidate "
                             "is lost to a timeout with nothing recorded")
    parser.add_argument("--max-new-tokens", type=int, default=512)
    parser.add_argument("--research-mode", action="store_true")
    parser.add_argument("--local-context", action="store_true")
    parser.add_argument("--target-policy", choices=tuple(TARGET_POLICY_DEPTH), default="strict_top1")
    parser.add_argument("--expand-truncated-end", dest="expand_truncated_end",
                        action="store_true", default=None)
    parser.add_argument("--no-expand-truncated-end", dest="expand_truncated_end",
                        action="store_false")
    parser.add_argument("--selection-rule", choices=SELECTION_RULES,
                        default="smallest_diff",
                        help="With --max-targets > 1, how to choose between patches on "
                             "different targets. smallest_diff is the default and what "
                             "every recorded run used; rank_first trusts Stage-3's "
                             "ranking instead and measured WORSE on baseline-T08 "
                             "(same_file 3->2, miss 2->3) -- see "
                             "scripts/replay_multitarget_selection.py. Both rules' "
                             "choices are recorded whichever one runs.")
    parser.add_argument("--prompt-layout", choices=PROMPT_LAYOUTS, default="task_last",
                        help="task_last puts the repair instruction immediately above "
                             "the generation point (the 2026-10-02 rewrite); legacy "
                             "reproduces the layout baseline-T08 was generated with.")
    parser.add_argument("--include-original-body", action="store_true",
                        help="Show the model the implementation it is replacing, as comments "
                             "in the prefix. Without it the model rewrites a function it has "
                             "never seen and invents APIs that do not exist")
    parser.add_argument("--explicit-argument-edit", action="store_true",
                        help="Restate an explicit positional-argument correction from the "
                             "public issue when the named call appears in the selected source")
    parser.add_argument("--min-body-retention", type=float, default=0.5,
                        help="Reject a body keeping less than this share of the original "
                             "lines. 0 disables the check (the pre-3h behaviour)")
    parser.add_argument("--max-body-lines", type=int, default=0)
    parser.add_argument("--large-function-strategy", choices=LARGE_FUNCTION_STRATEGIES,
                        default="full_body",
                        help="Narrow functions of at least 80 lines to one issue-matched "
                             "top-level statement when set to issue_statement")
    parser.add_argument("--large-function-line-threshold", type=int, default=80,
                        help="Body size at or above which --large-function-strategy issue_statement narrows generation to one issue-relevant statement. Lower it to apply the same narrowing to ordinary functions")
    parser.add_argument("--max-targets", type=int, default=1,
                        help="Generate against this many callable targets instead of stopping at the first one the AST supports. Model calls scale linearly, so lower --candidates when raising this. 1 keeps the single-target behaviour")
    parser.add_argument("--max-related-targets", type=int, default=3,
                        help="Maximum total functions in a related-target repair (1-5)")
    return parser


def build_run_config(args, tickets: list[dict]) -> dict:
    from fim_patch.domain_adapters import VERSIONS as ADAPTER_VERSIONS
    if args.generation_mode == "rewrite":
        generation_strategy = "rewrite_function"
        prompt_version = REWRITE_PROMPT_VERSION
        backend_kind = "ollama-rewrite-v1"
    else:
        generation_strategy = "fim_psm"
        # The same suffix FIMPatchGenerator stamps on each PatchRecord, so a
        # run's config and its records name the same prompt.
        prompt_version = fim_prompt_version(
            generation_scope=({'expression':'python_expression','statement':'issue_statement','body':'function_body'}[args.fim_scope]
                              if getattr(args,'fim_scope',None) else "issue_statement"
                              if args.large_function_strategy == "issue_statement"
                              else "function_body"),
            include_original_body=args.include_original_body,
            prompt_layout=args.prompt_layout,
        ) + ("-explicit-argument-edit" if args.explicit_argument_edit else "") + ("-fixed-signature-body-v1" if getattr(args, "fixed_signature_body", False) else "") + ("-helper-contracts-v1" if getattr(args, "helper_contracts", False) or (getattr(args, "adaptive_repair", False) or getattr(args, "multi_function_repair", False)) else "") + ("-bounded-call-chain-v1" if (getattr(args, "adaptive_repair", False) or getattr(args, "multi_function_repair", False)) else "")
        backend_kind = "ollama-fim-v4-token-budget" if getattr(args, 'tokenizer_json', None) else "ollama-fim-v3"
    if getattr(args, 'multi_function_repair', False):
        prompt_version += '-multi-function-fim-v1'
    if getattr(args, 'local_fim', False):
        prompt_version += '-local-fim-v1'
    if getattr(args, 'local_fim_expressions', False):
        prompt_version += '-local-fim-expression-v2'
    if getattr(args,'fim_scope',None):
        prompt_version += '-explicit-fim-scope-v1-'+args.fim_scope
    if (getattr(args,'local_fim_expressions',False) or getattr(args,'fim_scope',None)) and not getattr(args,'scope_adjustment',False):
        from fim_patch.public_behavior import VERSION as public_behavior_version
        prompt_version += '-'+public_behavior_version
    if getattr(args, 'scope_adjustment', False):
        from fim_patch.scope_execution import VERSION as scope_version
        prompt_version += '-'+scope_version
    if getattr(args,'safe_body_feedback',False):
        prompt_version += '-safe-body-feedback-v1'
    return {
        **({'contract_feedback':'contract-feedback-retry-v1',
            'contract_selection_policy':'preserve_first_without_test_evidence'}
           if getattr(args,'contract_feedback',False) else {}),
        'domain_adapters':{name:ADAPTER_VERSIONS[name]
                           for name in getattr(args,'domain_adapter',[])},
        'body_retention_metric':'executable-lines-v2',
        **({'safe_body_feedback':'safe-body-feedback-v1','source_contract_probe':'source-none-probe-v2-symbolic'}
           if getattr(args,'safe_body_feedback',False) else {}),
        **({'operation_contract_preview':'operation-contract-preview-v1',
            'operation_contract_diagnostic_version':'operation-contract-diagnostic-v1'}
           if getattr(args,'operation_contract_preview',False) else {}),
        **({'fim_scope':args.fim_scope,'fim_scope_version':'explicit-fim-scope-v1',
            'fim_output_ceiling':getattr(args,'fim_output_ceiling',None)} if getattr(args,'fim_scope',None) else {}),
        **({'scope_adjustment':scope_version, 'scope_generation_call_limit':3}
           if getattr(args,'scope_adjustment',False) else {}),
        'output_normalization': ('scope-aware-expression-v2' if getattr(args,'local_fim_expressions',False)
                                 else 'trailing-newline-v1'),
        **({'fixed_body_extraction':'fixed-body-boundary-v1'}
           if getattr(args, 'fixed_body_extraction', False) else {}),
        **({'multi_function_validation_schedule':'after_each_changed_body' if (getattr(args, 'repair_official', False) or getattr(args, 'repair_cloud_repo', None)) else 'after_plan'}
           if getattr(args, 'multi_function_repair', False) and getattr(args, 'fixed_body_extraction', False) else {}),
        **({'adaptive_repair':'adaptive-repair-v1', 'helper_implementations':'bounded-call-chain-v1'}
           if getattr(args, 'adaptive_repair', False) else {}),
        **({'local_fim':'local-fim-v1','local_fim_max_lines':20,'local_fim_max_chars':2000,'local_fim_context_chars':3200}
           if getattr(args,'local_fim',False) else {}),
        **({'local_fim_expressions':'local-fim-expression-v2'}
           if getattr(args,'local_fim_expressions',False) else {}),
        **({'helper_contracts':'same-file-helper-contract-v1'}
           if getattr(args, 'helper_contracts', False) or getattr(args, 'adaptive_repair', False) or getattr(args, 'multi_function_repair', False) else {}),
        **({'fixed_signature_body':'fixed-signature-body-v1'}
           if getattr(args, 'fixed_signature_body', False) else {}),
        **({'safe_function_extraction':'safe-function-extraction-v1'}
           if getattr(args, 'safe_function_extraction', False) else {}),
        **({'cloud_official':'github-actions-v1', 'repair_cloud_repo':args.repair_cloud_repo,
            'repair_cloud_ref':args.repair_cloud_ref} if getattr(args,'repair_cloud_repo',None) else {}),
        **({'repair_rounds':getattr(args, 'repair_rounds', 1),
            'test_failure_excerpt_version':'test-failure-priority-v2',
            'repair_feedback_version':('contract-feedback-retry-v1' if getattr(args,'contract_feedback',False) else 'source-none-feedback-v1' if getattr(args,'safe_body_feedback',False) else 'compact-public-feedback-v1' if getattr(args,'local_fim_expressions',False) and not getattr(args,'scope_adjustment',False) else 'compact-feedback-v1' if args.prompt_layout == 'compact_fim' else 'target-diagnosis-v1'),
            'repair_official':getattr(args, 'repair_official', False),
            'repair_test_timeout':getattr(args, 'repair_test_timeout', 1800)}
           if getattr(args, 'repair_rounds', 1)>1 or getattr(args, 'repair_official', False) or getattr(args, 'repair_cloud_repo', None) else {}),
        **({'budget_measurement': 'verified-tokenizer-v1',
            'tokenizer_sha256': hashlib.sha256(Path(args.tokenizer_json).read_bytes()).hexdigest()}
           if getattr(args, 'tokenizer_json', None) else {}),
        **({'multi_function_repair':'multi-function-fim-v1', 'helper_implementations':'bounded-call-chain-v1',
            'generation_call_limit':getattr(args, 'repair_rounds', 1), 'max_functions':args.max_related_targets}
           if getattr(args, 'multi_function_repair', False) else {}),
        "tickets": str(args.tickets), "ticket_count": len(tickets),
        "localizations": str(args.localizations),
        "generation_accounting_version": "static-candidate-accounting-v1",
        "logic_accounting_version": "python-ast-candidate-v1",
        "repair_progress_version": "python-ast-candidate-v1",
        "generation_mode": args.generation_mode,
        "generation_strategy": generation_strategy,
        "prompt_version": prompt_version,
        "backend_kind": backend_kind,
        "target_policy": args.target_policy,
        "expand_truncated_end": args.expand_truncated_end,
        "max_body_lines": args.max_body_lines,
        "large_function_strategy": args.large_function_strategy,
        "candidates": args.candidates, "seed": args.seed,
        "model": args.model, "num_ctx": args.num_ctx,
        "temperature": args.temperature,
        "max_new_tokens": args.max_new_tokens,
        "fim_transport": (args.fim_transport if args.generation_mode == "fim" else None),
        "timeout": args.timeout,
        "include_original_body": args.include_original_body,
        "explicit_argument_edit": args.explicit_argument_edit,
        "prompt_layout": args.prompt_layout,
        "selection_rule": (args.selection_rule if args.max_targets > 1 else None),
        "min_body_retention": args.min_body_retention,
        "large_function_line_threshold": args.large_function_line_threshold,
        "max_targets": args.max_targets,
        "max_related_targets": args.max_related_targets,
        "local_context": args.local_context, "research_mode": args.research_mode,
    }


def validate_resume_config(path: Path, current: dict) -> None:
    """Refuse to append records produced under a different experiment config."""

    if not path.exists():
        raise ValueError("resume requires the existing run_config.json")
    previous = json.loads(path.read_text(encoding="utf-8-sig"))
    for key, value in HISTORICAL_DEFAULTS.items():
        previous.setdefault(key, value)
    if previous != current:
        keys = sorted(key for key in set(previous) | set(current)
                      if previous.get(key) != current.get(key))
        raise ValueError("resume run_config mismatch: " + ", ".join(keys))


def select_tickets(rows: list[dict], *, allowlist: set[str] | None, limit: int | None) -> list[dict]:
    selected = [r for r in rows if ticket_identifier(r)]
    if allowlist is not None:
        selected = [r for r in selected if ticket_identifier(r) in allowlist]
    if limit is not None:
        selected = selected[:limit]
    return selected


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_generation_args(args, parser)
    if args.target_policy in RESEARCH_ONLY_TARGET_POLICIES and not args.research_mode:
        parser.error("Alternative target selection requires --research-mode")
    if args.max_body_lines < 0:
        parser.error("--max-body-lines must be zero or positive")
    if args.large_function_line_threshold < 1:
        parser.error("--large-function-line-threshold must be positive")
    if not 1 <= args.max_targets <= 10:
        parser.error("--max-targets must be between 1 and 10")
    if args.max_targets > 1 and args.target_policy != "strict_top1":
        parser.error("--max-targets scans targets itself; use --target-policy strict_top1")
    if not 1 <= args.max_related_targets <= 5:
        parser.error("--max-related-targets must be between 1 and 5")

    run_root = args.output
    run_root.mkdir(parents=True, exist_ok=True)
    records_path = run_root / DEFAULT_RECORDS_NAME
    snapshot_cache_dir = args.snapshot_cache_dir or (args.repo_cache_dir / DEFAULT_SNAPSHOT_SUBDIR)

    rows = load_jsonl(args.tickets)
    localizations = load_localization_map(args.localizations)
    allowlist = load_ticket_id_allowlist(args.ticket_ids_from) if args.ticket_ids_from else None
    tickets = select_tickets(rows, allowlist=allowlist, limit=args.limit)
    if not tickets:
        print("No tickets selected.", file=sys.stderr)
        return 1

    config_path = run_root / "run_config.json"
    run_config = build_run_config(args, tickets)
    if args.resume:
        try:
            validate_resume_config(config_path, run_config)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            parser.error(str(exc))

    previous = load_previous_records(records_path) if args.resume else {}
    if previous:
        print(f"Resume: {len(previous)} ticket(s) already recorded in {records_path}")

    config_path.write_text(json.dumps(run_config, ensure_ascii=False, indent=2),
                           encoding="utf-8")

    pipeline = RepairPipeline(make_generator(args))
    records: list[dict] = []
    handle = records_path.open("a" if previous else "w", encoding="utf-8")
    try:
        for position, ticket in enumerate(tickets, 1):
            ticket_id = ticket_identifier(ticket)
            if ticket_id in previous:
                records.append(previous[ticket_id])
                continue
            ticket_dir = run_root / "tickets" / safe_dir_name(ticket_id)
            if ticket_dir.exists():
                record = build_record(ticket, skipped="output directory already exists",
                                      output_dir=str(ticket_dir))
            else:
                try:
                    record = run_one_ticket(
                        ticket, pipeline=pipeline,
                        localization=localizations.get(ticket_id),
                        repo_cache_dir=args.repo_cache_dir,
                        snapshot_cache_dir=snapshot_cache_dir,
                        ticket_output_dir=ticket_dir,
                        clone_missing=args.clone_missing,
                        fetch_missing_commits=args.fetch_missing_commits,
                    )
                except KeyboardInterrupt:
                    print("\nInterrupted; records so far are already on disk.", file=sys.stderr)
                    raise
                except Exception:  # noqa: BLE001 - belt and braces around the per-ticket guard
                    record = build_record(ticket, error=traceback.format_exc(limit=3))
            records.append(record)
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            print(f"[{position}/{len(tickets)}] {ticket_id}: "
                  f"{record['patch_status'] or record['error'] or record['skipped']}"
                  f" (rank={record['target_rank']}, {record['duration_s']}s)")
    finally:
        handle.close()

    summary = summarize(records)
    (run_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    count = write_predictions(run_root / "predictions.jsonl", records, run_root)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nRecords: {records_path}")
    print(f"Predictions for the official harness ({count} row(s)): {run_root / 'predictions.jsonl'}")
    return 0 if summary["attempted"] and not summary["errored"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
