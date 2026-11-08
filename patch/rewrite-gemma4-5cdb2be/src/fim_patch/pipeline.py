"""Apply an externally computed localization to one exact repository snapshot."""
from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath

from .generator import FIMPatchGenerator, git, sha


FROZEN_RETRIEVAL_METHOD = {
    "embedding_backend": "tfidf",
    "candidate_file_k": 20,
    "top_k": 5,
    "domain_path_routing": True,
    "file_aggregation": True,
    "advanced_file_aggregation": False,
    "llm_rerank": False,
    "symbol_llm_rerank_used": False,
}


def validate_localization(ticket: dict, location: dict, repo: str | Path) -> str:
    """Validate identity, frozen retrieval settings, and every ranked source hash."""
    root = Path(repo).resolve()
    if not isinstance(ticket, dict) or not isinstance(location, dict):
        raise ValueError("Ticket and localization must be JSON objects")

    ticket_base = str(ticket.get("base_commit") or "").strip()
    location_base = str(location.get("base_commit") or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", ticket_base):
        raise ValueError("Ticket requires a valid base_commit SHA")
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", location_base):
        raise ValueError("Localization requires its own base_commit SHA")
    ticket_commit = git(root, "rev-parse", "--verify", ticket_base + "^{commit}").decode().strip()
    location_commit = git(root, "rev-parse", "--verify", location_base + "^{commit}").decode().strip()
    if ticket_commit != location_commit:
        raise ValueError("Ticket and localization base_commit disagree")
    if git(root, "rev-parse", "HEAD").decode().strip() != ticket_commit:
        raise ValueError("Repository HEAD does not match ticket/localization base_commit")
    if git(root, "status", "--porcelain"):
        raise ValueError("Pipeline requires a clean repository")

    expected_repo = str(ticket.get("repo") or ticket.get("repository") or root.name).strip()
    localized_repo = str(location.get("repo") or location.get("repository") or "").strip()
    if not localized_repo or localized_repo.casefold() != expected_repo.casefold():
        raise ValueError(
            f"Localization repo {localized_repo!r} does not match ticket/repository {expected_repo!r}"
        )

    method = location.get("method")
    if not isinstance(method, dict):
        raise ValueError("Localization is missing frozen retrieval method metadata")
    for key, expected in FROZEN_RETRIEVAL_METHOD.items():
        actual = method.get(key)
        matches = actual is expected if isinstance(expected, bool) else actual == expected
        if not matches:
            raise ValueError(f"Localization method.{key} must be {expected!r} for the frozen TF-IDF baseline")
    for key in (
        "supervised_reranker_used", "stage3_supervised_reranker_used",
        "supervised_reranker_checkpoint", "reranker_checkpoint", "ml_checkpoint",
        "checkpoint_path",
    ):
        if method.get(key):
            raise ValueError(f"Localization must not reference a supervised checkpoint ({key})")

    rows = location.get("stage3_ranked_symbols")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Localization requires non-empty stage3_ranked_symbols")
    if not isinstance(location.get("confidence_level"), str):
        raise ValueError("Localization requires confidence_level")
    if not isinstance(location.get("should_manual_review"), bool):
        raise ValueError("Localization requires boolean should_manual_review")
    if not isinstance(location.get("recommend_patch_generation"), bool):
        raise ValueError("Localization requires boolean recommend_patch_generation")
    fingerprints = location.get("source_file_sha256")
    if not isinstance(fingerprints, dict):
        raise ValueError("Localization requires source_file_sha256")

    seen_paths = set()
    for row in rows:
        if not isinstance(row, dict) or not all(
            key in row for key in ("file_path", "symbol_qualified_name", "start_line", "end_line")
        ):
            raise ValueError("Malformed Stage-3 symbol row")
        raw_path = str(row["file_path"]).replace("\\", "/")
        relative = PurePosixPath(raw_path)
        if (not raw_path or relative.is_absolute() or ".." in relative.parts
                or any(char in raw_path for char in (":", "\n", "\r", "\t", '"'))):
            raise ValueError(f"Invalid Stage-3 source path: {raw_path!r}")
        digest = fingerprints.get(raw_path)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise ValueError(f"Localization is missing a valid SHA-256 for {raw_path!r}")

        source = root.joinpath(*relative.parts)
        if any(part.is_symlink() for part in [source, *source.parents] if part != root.parent):
            raise ValueError(f"Stage-3 source path contains a symlink: {raw_path!r}")
        resolved = source.resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ValueError(f"Stage-3 source is missing or outside repository: {raw_path!r}")
        source_bytes = resolved.read_bytes()
        committed_bytes = git(root, "show", f"{ticket_commit}:{raw_path}")
        if source_bytes != committed_bytes:
            raise ValueError(f"Stage-3 source differs from base_commit: {raw_path!r}")
        if sha(source_bytes).casefold() != digest.casefold():
            raise ValueError(f"source_file_sha256 mismatch for {raw_path!r}")
        seen_paths.add(raw_path)

    return ticket_commit


class RepairPipeline:
    """Pass a validated external localization directly to the patch generator."""

    def __init__(self, generator=None):
        self.generator = generator or FIMPatchGenerator(
            target_policy="first_supported_ast", include_original_body=True)

    def run(self, ticket, repo, localization):
        validate_localization(ticket, localization, repo)
        result = self.generator.generate(ticket, localization, str(repo))
        return localization, result


def write_run(output, ticket, location, result):
    from .generation_records import annotate_generation
    result = annotate_generation(result)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    chosen = next((c for c in result["candidates"]
                   if c["candidate_id"] == result.get("selected_candidate_id")), {})
    evaluation = {
        "patch_status": result["patch_status"],
        "validation_status": result["validation_status"],
        "generation_status": result["generation_status"],
        "generated_before_validation": result["generated_before_validation"],
        "static_candidate_count": result["static_candidate_count"],
        "logical_static_candidate_count": result['logical_static_candidate_count'],
        "logic_unclassified_candidate_count": result['logic_unclassified_candidate_count'],
        "logic_accounting_version": result['logic_accounting_version'],
        "logic_accounting_complete": result['logic_accounting_complete'],
        "interpretation": ("Passed supplied tests; requires review" if result["validation_status"] == "plausible"
                           else "Static/apply-valid candidate generated; tests failed or remain unverified" if result["generated_before_validation"]
                           else result.get("explanation", "No patch")),
        "validation": chosen.get("validation"), "official_resolved": result.get('official_resolved'),
    }
    if result.get('operation_contract_preview'):
        evaluation.update(operation_contract_diagnostic=chosen.get('operation_contract_diagnostic'),
            operation_contract_preview={k:v for k,v in (chosen.get('operation_contract_preview') or {}).items()
                                        if k not in ('prefix','suffix')})
    for name, value in [("ticket", ticket), ("localization", location), ("result", result),
                        ("candidates", result["candidates"]), ("evaluation", evaluation)]:
        (output / (name + ".json")).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "selected.diff").write_bytes(result["patch"].encode("utf-8"))
    prediction = {"instance_id": ticket.get("instance_id") or ticket.get("ticket_id"),
                  "model_name_or_path": result.get("model", {}).get("model", "unknown"),
                  "model_patch": result["patch"]}
    (output / "predictions.jsonl").write_text(json.dumps(prediction) + "\n", encoding="utf-8")

    # Failed tests do not promote a patch to selected.diff/predictions.jsonl.
    # Keep every unique static/apply-valid proposal in a separate review folder.
    candidate_dir = output/'generated_candidates'
    candidate_dir.mkdir()
    manifest=[]
    for index,candidate in enumerate(result['generated_candidates'],1):
        name=f'{index:03d}.diff'
        (candidate_dir/name).write_bytes(candidate['patch'].encode('utf-8'))
        manifest.append({k:v for k,v in dict(candidate,diff_file=name).items() if k!='patch'})
    (candidate_dir/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    if result.get('operation_contract_preview'):
        contract_dir = output/'operation_contract'
        contract_dir.mkdir()
        contract_manifest = []
        for round_index, attempt in enumerate(result.get('repair_history') or [result],1):
            for index, candidate in enumerate(attempt.get('candidates',[]),1):
                diagnostic = candidate.get('operation_contract_diagnostic')
                if diagnostic is None:
                    continue
                stem = f'round{round_index:03d}-candidate{index:03d}'
                preview = candidate.get('operation_contract_preview') or {}
                (contract_dir/(stem+'-diagnostic.json')).write_text(json.dumps(diagnostic,ensure_ascii=False,indent=2),encoding='utf8')
                (contract_dir/(stem+'-preview.json')).write_text(json.dumps({k:v for k,v in preview.items() if k not in ('prefix','suffix')},ensure_ascii=False,indent=2),encoding='utf8')
                if preview.get('status')=='ready':
                    for key in ('prefix','suffix','feedback'):
                        (contract_dir/(stem+'-'+key+'.txt')).write_text(preview[key],encoding='utf8')
                contract_manifest.append(dict(round=round_index,candidate_id=candidate.get('candidate_id'),
                    diagnostic_status=diagnostic.get('status'),preview_status=preview.get('status'),
                    diagnostic_file=stem+'-diagnostic.json',preview_file=stem+'-preview.json',not_dispatched=True))
        (contract_dir/'manifest.json').write_text(json.dumps(contract_manifest,ensure_ascii=False,indent=2),encoding='utf8')
