"""Stage 6 (on demand): patch generation with the team's rewrite + gemma4:12b method.

The generator is the vendored copy in ``patch/rewrite-gemma4-5cdb2be`` (fixed-patch commit 5cdb2be),
the version that produced 21 candidate patches and 6 official SWE-bench fixes on the 36-ticket
development set (``experiments/eval36_v7/results/rewrite-gemma4-c3-20261106``).  This module only
adapts the workspace's data to that code:

* the ticket text comes from the imported raw ticket (the same public fields the 36-ticket run used);
* the localization is the workspace's own fault-localization output for this ticket
  (``bug_location_raw.json``), never a gold/oracle location;
* the repository is a git checkout of the project's clone at the ticket's ``base_commit``.

Every method setting is frozen to that run's ``run_config.json`` (see ``FROZEN_RUN_CONFIG``); only the
model service, candidates, seed and sampling come from ``integration_config.json``.

The generator does not run tests.  A generated patch passed the syntax/AST/``git apply`` gates only.
An official verdict is shown only when the generated patch is byte-identical (same sha256) to a patch
that the official SWE-bench harness has judged.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import re
import json
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from integration.common import stage_result
from integration.config import IntegrationConfig, ProjectSettings


MODULE = "補丁生成（對話式改寫 + gemma4:12b，fixed-patch 5cdb2be）"
IMPLEMENTATION = "patch/rewrite-gemma4-5cdb2be/src/fim_patch/generator.py"
EXPERIMENT = "rewrite-gemma4-c3-20261106"
EXPERIMENT_DIR = Path("experiments") / "eval36_v7" / "results" / EXPERIMENT

# Method settings of the 6/36 run (experiments/eval36_v7/results/rewrite-gemma4-c3-20261106/run_config.json).
# tests/test_integration_workflow.py checks these against the vendored run_config.json.
FROZEN_RUN_CONFIG: dict[str, Any] = {
    "generation_mode": "rewrite",
    "target_policy": "first_supported_ast",
    "expand_truncated_end": None,
    "max_body_lines": 0,
    "large_function_strategy": "full_body",
    "large_function_line_threshold": 80,
    "include_original_body": False,
    "min_body_retention": 0.5,
    "max_targets": 1,
    "max_related_targets": 3,
    "explicit_argument_edit": False,
    "prompt_layout": "task_last",
    "local_context": False,
}

# Fields that must never reach the prompt (same list as scripts/run_stage4_batch.py, plus upper-case
# SWE-bench spellings).
GROUND_TRUTH_FIELDS = frozenset({
    "patch", "test_patch", "gold_patch", "fail_to_pass", "pass_to_pass", "hints_text",
    "FAIL_TO_PASS", "PASS_TO_PASS", "fixed_files", "fix_pull_request",
})
STRUCTURED_FALLBACK_FIELDS = ("expected_behavior", "actual_behavior", "error_message", "steps_to_reproduce")

_IMPORT_LOCK = threading.Lock()


class PatchUnavailable(RuntimeError):
    """The patch stage cannot run for this ticket; the message is shown to the engineer."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git(cwd: Path, *args: str) -> bytes:
    completed = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=False)
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", "replace").strip() or completed.stdout.decode("utf-8", "replace").strip()
        raise PatchUnavailable(f"git {' '.join(args)} 失敗：{message}")
    return completed.stdout


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _safe_cache_name(value: str) -> str:
    normalized = value.replace("/", "__").replace("\\", "__").replace(":", "_")
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in normalized)


def materialize_source_snapshot_git(source: Path, *, repo: str, revision: str, cache_dir: Path) -> tuple[Path, str]:
    """Wrap an immutable source-only snapshot in a cached local Git repository.

    Synthetic evaluation fixtures identify versions by a SHA-256 tree digest rather than a Git
    commit.  The vendored patch generator intentionally requires Git for its clean-tree and
    ``git apply`` gates, so copy that exact public snapshot into the workspace cache and create one
    local baseline commit.  The fixture itself is never modified; callers keep reporting the public
    SHA-256 revision while using the returned Git commit internally.
    """

    source = Path(source).resolve()
    if not source.is_dir():
        raise PatchUnavailable(f"找不到程式碼快照：{source}")
    destination = (Path(cache_dir) / f"{_safe_cache_name(repo)}__source__{_safe_cache_name(revision)[:16]}").resolve()
    marker_name = "source-snapshot-revision"
    if destination.exists():
        marker = destination / ".git" / marker_name
        if not marker.is_file() or marker.read_text(encoding="utf-8").strip() != revision:
            raise PatchUnavailable(f"快取路徑已存在，但不是要求的程式碼版本：{destination}")
        if _git(destination, "status", "--porcelain").decode().strip():
            raise PatchUnavailable(f"程式碼快取已被修改，請刪除後重試：{destination}")
        return destination, _git(destination, "rev-parse", "HEAD").decode().strip()

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)
    try:
        _git(destination, "init", "-q")
        _git(destination, "config", "core.autocrlf", "false")
        _git(destination, "config", "core.eol", "lf")
        _git(destination, "add", "-A")
        _git(destination, "-c", "user.name=workspace", "-c", "user.email=workspace@example.invalid",
             "commit", "-qm", f"source snapshot {revision}")
        commit = _git(destination, "rev-parse", "HEAD").decode().strip()
        (destination / ".git" / marker_name).write_text(revision + "\n", encoding="utf-8")
        return destination, commit
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


class TeamPatchGenerator:
    """Runs the vendored RepairPipeline for one ticket."""

    def __init__(self, config: IntegrationConfig, *, backend_factory: Callable[[], Any] | None = None) -> None:
        self.config = config
        self.backend_factory = backend_factory
        self._modules: dict[str, Any] | None = None
        self._experiment: dict[str, Any] | None = None

    # ------------------------------------------------------------ code
    @property
    def code_dir(self) -> Path:
        return self.config.patch_code_dir()

    def _load(self) -> dict[str, Any]:
        if self._modules is not None:
            return self._modules
        src = self.code_dir / "src"
        if not (src / "fim_patch" / "generator.py").is_file():
            raise PatchUnavailable(f"找不到補丁生成程式：{src / 'fim_patch'}。請確認已 git pull，或在設定檔 patch.code_dir 指定位置。")
        with _IMPORT_LOCK:
            loaded = sys.modules.get("fim_patch")
            if loaded is not None and Path(getattr(loaded, "__file__", "")).resolve().parent != (src / "fim_patch").resolve():
                raise PatchUnavailable(f"已載入另一份 fim_patch（{loaded.__file__}），請重新啟動伺服器。")
            # Appended, not prepended: the vendored src/ also has utils/ and modules/ packages, and the
            # main system's packages of the same names must keep winning.
            if str(src) not in sys.path:
                sys.path.append(str(src))
            from fim_patch import backend as backend_module  # noqa: PLC0415
            from fim_patch import generator as generator_module  # noqa: PLC0415
            from fim_patch import pipeline as pipeline_module  # noqa: PLC0415

            spec = importlib.util.spec_from_file_location("_patch_repo_snapshot", src / "utils" / "repo_snapshot.py")
            if spec is None or spec.loader is None:
                raise PatchUnavailable("找不到 utils/repo_snapshot.py")
            snapshot_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(snapshot_module)
        self._modules = {
            "backend": backend_module,
            "generator": generator_module,
            "pipeline": pipeline_module,
            "snapshot": snapshot_module,
        }
        return self._modules

    def readiness(self) -> dict[str, Any]:
        src = self.code_dir / "src" / "fim_patch" / "generator.py"
        return {"code": src.is_file(), "code_path": str(src.parent), "verdicts": self.config.patch_verdicts_path().is_file()}

    # ------------------------------------------------------- experiment
    def experiment_record(self, ticket_id: str) -> dict[str, Any] | None:
        """What the 6/36 run produced for this ticket, with its official verdict (if any)."""

        if self._experiment is None:
            base = self.code_dir / EXPERIMENT_DIR
            tickets = {str(row.get("ticket_id") or row.get("instance_id")) for row in _read_jsonl(self.code_dir / "experiments" / "eval36_v7" / "tickets.jsonl")}
            predictions = {str(row["instance_id"]): row for row in _read_jsonl(base / "predictions.jsonl")}
            self._experiment = {"tickets": tickets, "predictions": predictions, "verdicts": self._verdicts()}
        data = self._experiment
        if ticket_id not in data["tickets"]:
            return None
        row = data["predictions"].get(ticket_id)
        if row is None:
            return {"experiment": EXPERIMENT, "generated": False}
        patch = str(row.get("model_patch") or "")
        sha = _sha256(patch.encode("utf-8"))
        verdict = data["verdicts"].get(sha)
        return {
            "experiment": EXPERIMENT,
            "generated": True,
            "patch": patch,
            "patch_sha256": sha,
            "official": verdict,
        }

    def _verdicts(self) -> dict[str, dict[str, Any]]:
        verdicts: dict[str, dict[str, Any]] = {}
        for row in _read_jsonl(self.config.patch_verdicts_path()):
            sha = str(row.get("patch_sha256") or "")
            if sha and isinstance(row.get("official_resolved"), bool):
                verdicts[sha] = {
                    "official_resolved": row["official_resolved"],
                    "status": row.get("status"),
                    "run_id": row.get("run_id"),
                    "experiment": row.get("experiment"),
                    "instance_id": row.get("instance_id"),
                }
        return verdicts

    def official_verdict(self, patch: str) -> dict[str, Any] | None:
        if not patch:
            return None
        if self._experiment is None:
            self.experiment_record("")
        return (self._experiment or {}).get("verdicts", {}).get(_sha256(patch.encode("utf-8")))

    # ----------------------------------------------------------- inputs
    @staticmethod
    def build_ticket(record: dict[str, Any], localization: dict[str, Any]) -> dict[str, Any]:
        """The public ticket text, shaped like experiments/eval36_v7/tickets.jsonl."""

        raw = record.get("raw") or {}
        structured = record.get("structured_ticket") or {}
        ticket = {key: value for key, value in raw.items() if key not in GROUND_TRUTH_FIELDS and not str(key).startswith("_")}
        ticket["ticket_id"] = str(record.get("ticket_id") or raw.get("ticket_id") or record.get("key"))
        title = str(raw.get("title") or structured.get("title") or record.get("title") or "").strip()
        description = str(raw.get("description") or raw.get("problem_statement") or raw.get("body") or "").strip()
        if description:
            # SWE-bench's problem statement starts with the issue title; the workspace demo files split it off.
            if title and not description.startswith(title):
                description = f"{title}\n{description}"
        else:
            description = str(structured.get("description") or "").strip()
            for key in STRUCTURED_FALLBACK_FIELDS:  # hand-written tickets: use the extracted fields
                value = structured.get(key)
                if value not in (None, "", "unknown", []) and not ticket.get(key):
                    ticket[key] = value
        ticket["title"] = title
        ticket["description"] = description
        ticket.setdefault("bug_report", description)
        ticket["repo"] = str(raw.get("repo") or localization.get("repo") or record.get("project") or "")
        ticket["base_commit"] = str(raw.get("base_commit") or localization.get("base_commit") or "")
        return ticket

    @staticmethod
    def align_line_endings(localization: dict[str, Any], snapshot: Path, commit: str) -> list[str]:
        """The localization hashes the files it read; the workspace's version snapshots come from
        ``git archive``, which may convert line endings (core.autocrlf on Windows).  The patch
        generator reads the exact committed bytes.  When the only difference is LF vs CRLF, use the
        committed bytes' hash; any other difference still fails the generator's own check."""

        fingerprints = localization.get("source_file_sha256") or {}
        aligned = []
        for path, digest in list(fingerprints.items()):
            try:
                blob = _git(snapshot, "show", f"{commit}:{path}")
            except PatchUnavailable:
                continue
            blob_sha = _sha256(blob)
            if str(digest).lower() == blob_sha:
                continue
            lf = blob.replace(b"\r\n", b"\n")
            if str(digest).lower() in {_sha256(lf), _sha256(lf.replace(b"\n", b"\r\n"))}:
                fingerprints[path] = blob_sha
                aligned.append(path)
        return aligned

    def _backend(self, modules: dict[str, Any]) -> Any:
        if self.backend_factory is not None:
            return self.backend_factory()
        settings = self.config.patch
        return modules["backend"].OllamaRewrite(
            model=settings.model,
            url=settings.ollama_url,
            num_ctx=int(settings.num_ctx),
            max_new_tokens=int(settings.max_new_tokens),
            timeout=int(settings.timeout_seconds),
            temperature=float(settings.temperature),
        )

    def _generator(self, modules: dict[str, Any], backend: Any) -> Any:
        settings = self.config.patch
        frozen = {key: value for key, value in FROZEN_RUN_CONFIG.items() if key != "max_targets"}
        return modules["generator"].FIMPatchGenerator(
            backend,
            candidates=int(settings.candidates),
            seed=int(settings.seed),
            research_mode=bool(settings.research_mode),
            **frozen,
        )

    # -------------------------------------------------------------- run
    def generate(
        self,
        record: dict[str, Any],
        project: ProjectSettings | None,
        localization: dict[str, Any] | None,
        *,
        output_dir: Path,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        settings = self.config.patch
        try:
            if not settings.enabled:
                raise PatchUnavailable("設定檔已關閉補丁生成（patch.enabled = false）。")
            if not localization or not localization.get("stage3_ranked_symbols"):
                raise PatchUnavailable("這張 Ticket 還沒有錯誤定位結果，請先完成錯誤定位。")
            if project is None:
                raise PatchUnavailable("這張 Ticket 沒有對應的專案程式碼（設定檔 projects）。")
            modules = self._load()
            location = json.loads(json.dumps(localization))  # private copy
            ticket = self.build_ticket(record, location)
            public_base_commit = ticket["base_commit"]
            source_repo = Path(project.repo_path)
            if not (source_repo / ".git").exists():
                source_only = Path(project.snapshots_dir) / public_base_commit if project.snapshots_dir and public_base_commit else None
                if source_only is None or not source_only.is_dir():
                    raise PatchUnavailable(
                        f"補丁生成需要 git checkout，但「{project.name}」的 repo_path 不是 git repository：{source_repo}"
                    )
                source_repo, internal_commit = materialize_source_snapshot_git(
                    source_only,
                    repo=ticket["repo"] or project.name,
                    revision=public_base_commit,
                    cache_dir=self.config.store_dir / "source_snapshot_git",
                )
                ticket["base_commit"] = internal_commit
                location["base_commit"] = internal_commit
            if not ticket["base_commit"]:
                ticket["base_commit"] = _git(source_repo, "rev-parse", "HEAD").decode().strip()
            if project.snapshots_dir and public_base_commit and (Path(project.repo_path) / ".git").exists():
                # Localized in the ticket's own version snapshot and patched from the project's git clone.
                # Results saved before the localization fix carry the workspace repository's HEAD here
                # instead (the snapshot has no .git).  When there is no clone, the snapshot wrapper above
                # already set both to its internal commit.
                location["base_commit"] = ticket["base_commit"]
            if not location.get("base_commit"):
                location["base_commit"] = ticket["base_commit"]
            if not location.get("repo"):
                location["repo"] = ticket["repo"]
            if not ticket["repo"]:
                ticket["repo"] = location["repo"]

            snapshot_module = modules["snapshot"]
            try:
                snapshot = snapshot_module.materialize_commit_snapshot(
                    source_repo,
                    repo=ticket["repo"],
                    base_commit=ticket["base_commit"],
                    snapshot_cache_dir=self.config.store_dir / "patch_snapshots",
                )
            except snapshot_module.RepoSnapshotError as exc:
                raise PatchUnavailable(f"無法建立 base_commit 的程式碼快照：{exc}") from exc
            commit = _git(Path(snapshot), "rev-parse", "HEAD").decode().strip()
            aligned = self.align_line_endings(location, Path(snapshot), commit)

            backend = self._backend(modules)
            try:
                model_info = backend.describe()
            except Exception as exc:  # Ollama not running, model not pulled, ...
                raise PatchUnavailable(
                    f"無法使用模型 {settings.model}（{type(exc).__name__}: {exc}）。請確認 Ollama 正在執行，並已 ollama pull {settings.model}。"
                ) from exc

            pipeline = modules["pipeline"].RepairPipeline(self._generator(modules, backend))
            try:
                location, result = pipeline.run(ticket, str(snapshot), location)
            except ValueError as exc:
                raise PatchUnavailable(f"錯誤定位結果無法交給補丁生成：{exc}") from exc
            modules["pipeline"].write_run(output_dir, ticket, location, result)
        except PatchUnavailable as exc:
            return stage_result(stage="patch", status="failed", module=MODULE, implementation=IMPLEMENTATION, error=str(exc), started=started)

        output = self._summarize(result, location, ticket, output_dir, model_info, aligned)
        if public_base_commit:
            output["base_commit"] = public_base_commit
        warnings = self._warnings(output)
        return stage_result(
            stage="patch",
            status="done",
            module=MODULE,
            implementation=IMPLEMENTATION,
            output=output,
            warnings=warnings,
            started=started,
            details={"run_dir": str(output_dir), "frozen_run_config": dict(FROZEN_RUN_CONFIG)},
        )

    def _summarize(self, result: dict[str, Any], location: dict[str, Any], ticket: dict[str, Any], output_dir: Path,
                   model_info: dict[str, Any], aligned: list[str]) -> dict[str, Any]:
        patch = str(result.get("patch") or "")
        sha = _sha256(patch.encode("utf-8")) if patch else ""
        candidates = []
        for item in result.get("candidates") or []:
            candidates.append({
                "candidate_id": item.get("candidate_id"),
                "seed": item.get("seed"),
                "status": item.get("status"),
                "reason": item.get("reason") or "",
                "stop_reason": item.get("stop_reason") or "",
                "modified_files": item.get("modified_files") or [],
                "patch": item.get("patch") or "",
            })
        targets = []
        for item in result.get("target_resolution") or []:
            source = item.get("input") or {}
            targets.append({
                "rank": item.get("rank") or source.get("rank"),
                "file_path": source.get("file_path"),
                "symbol": source.get("symbol_qualified_name") or source.get("symbol_name"),
                "status": item.get("status") or ("used" if item.get("rank") == result.get("target_rank") else ""),
                "reason": item.get("reason") or item.get("rejection") or "",
            })
        settings = self.config.patch
        experiment = self.experiment_record(str(ticket.get("ticket_id") or ""))
        if experiment and experiment.get("generated"):
            experiment["same_as_this_run"] = bool(sha) and experiment["patch_sha256"] == sha
        return {
            "patch_status": result.get("patch_status"),
            "patch": patch,
            "patch_sha256": sha,
            "modified_files": result.get("modified_files") or [],
            "selected_candidate_id": result.get("selected_candidate_id"),
            "explanation": result.get("explanation") or "",
            "apply_status": result.get("apply_status"),
            "validation_status": result.get("validation_status"),
            "bug_location": result.get("bug_location") or {},
            "target_rank": result.get("target_rank"),
            "target_fallback_used": bool(result.get("target_fallback_used")),
            "targets": targets,
            "candidates": candidates,
            "generated_candidate_count": len(result.get("generated_candidates") or []),
            "localization_confidence": location.get("confidence_level"),
            "recommend_patch_generation": bool(location.get("recommend_patch_generation")),
            "research_mode": bool(settings.research_mode),
            "repo": ticket.get("repo"),
            "base_commit": ticket.get("base_commit"),
            "model": {key: model_info.get(key) for key in ("model", "digest", "num_ctx", "max_new_tokens", "temperature") if key in model_info},
            "settings": {"candidates": settings.candidates, "seed": settings.seed, "temperature": settings.temperature},
            "official": self.official_verdict(patch),
            "experiment": experiment,
            "line_endings_aligned": aligned,
            "run_dir": str(output_dir),
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    @staticmethod
    def _warnings(output: dict[str, Any]) -> list[str]:
        warnings = []
        if output.get("localization_confidence") != "high" or not output.get("recommend_patch_generation"):
            level = {"medium": "中", "low": "低"}.get(str(output.get("localization_confidence")), str(output.get("localization_confidence") or "未知"))
            warnings.append(f"錯誤定位信心為「{level}」，正式流程不會自動產生補丁；這裡照 6/36 實驗的研究模式產生，補丁一定要人工審查。")
        official = output.get("official")
        if output.get("patch_status") == "generated" and not official:
            warnings.append("這個補丁只通過語法、AST 與 git apply 檢查，沒有跑過測試。「產生補丁」不等於「修好」。")
        if official and official.get("official_resolved") is False:
            warnings.append("這個補丁和一個已經送過官方測試的補丁完全相同，官方判定：沒有修好。")
        return warnings


# --------------------------------------------------------------------------------------------------
# "Copy the fixed code": apply a unified diff to the file at base_commit and return the whole
# functions it touches, so an engineer can paste working code instead of a diff.

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_unified_diff(patch: str) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    hunk: dict[str, Any] | None = None
    for line in patch.splitlines():
        if line.startswith("--- "):
            current = {"old": line[4:].strip(), "path": "", "hunks": []}
            files.append(current)
            hunk = None
        elif line.startswith("+++ ") and current is not None and hunk is None:
            path = line[4:].strip()
            current["path"] = path[2:] if path.startswith("b/") else path
        elif (match := _HUNK.match(line)) and current is not None:
            hunk = {"old_start": int(match.group(1)), "new_start": int(match.group(3)), "lines": []}
            current["hunks"].append(hunk)
        elif hunk is not None and line[:1] in {" ", "+", "-", ""} and not line.startswith("\\"):
            hunk["lines"].append(line if line else " ")
    return [item for item in files if item["path"]]


def apply_hunks(old_text: str, hunks: list[dict[str, Any]]) -> tuple[str, set[int]]:
    """Apply hunks to ``old_text`` (LF line endings). Returns the new text and the changed new line numbers."""

    old = old_text.split("\n")
    new: list[str] = []
    changed: set[int] = set()
    cursor = 0  # index into old
    for hunk in hunks:
        start = hunk["old_start"] - 1 if hunk["old_start"] > 0 else 0
        if start < cursor:
            raise ValueError("補丁的區塊順序不正確")
        new.extend(old[cursor:start])
        cursor = start
        for line in hunk["lines"]:
            tag, text = line[:1], line[1:]
            if tag in (" ", "-"):
                if cursor >= len(old) or old[cursor].rstrip("\r") != text.rstrip("\r"):
                    raise ValueError(f"補丁和第 {cursor + 1} 行的程式碼對不上")
                if tag == " ":
                    new.append(old[cursor])
                else:
                    changed.add(len(new) + 1)  # deletion point, in new numbering
                cursor += 1
            else:  # "+"
                new.append(text)
                changed.add(len(new))
    new.extend(old[cursor:])
    return "\n".join(new), changed


def touched_functions(source: str, changed: set[int]) -> list[dict[str, Any]]:
    """The innermost function or method containing each changed line (whole definition)."""

    lines = source.split("\n")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        tree = None
    spans: list[tuple[int, int, str]] = []
    if tree is not None:
        def visit(node: ast.AST, prefix: str) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    name = f"{prefix}{child.name}"
                    if not isinstance(child, ast.ClassDef):
                        start = min([child.lineno] + [d.lineno for d in child.decorator_list])
                        spans.append((start, child.end_lineno or child.lineno, name))
                    visit(child, f"{name}.")
                else:
                    visit(child, prefix)
        visit(tree, "")
    picked: dict[tuple[int, int], str] = {}
    for line_no in sorted(changed):
        line_no = min(max(line_no, 1), len(lines))
        containing = [span for span in spans if span[0] <= line_no <= span[1]]
        if containing:
            start, end, name = min(containing, key=lambda span: span[1] - span[0])
        else:  # module-level change: a few lines around it
            start, end, name = max(1, line_no - 3), min(len(lines), line_no + 3), "（模組層級）"
        picked[(start, end)] = name
    merged = []
    for (start, end), name in sorted(picked.items()):
        if merged and start <= merged[-1]["end_line"]:
            continue  # nested in a function already returned
        merged.append({"name": name, "start_line": start, "end_line": end, "code": "\n".join(lines[start - 1:end]) + "\n"})
    return merged


def read_at_commit(project: ProjectSettings, base_commit: str, path: str) -> str:
    """File text at ``base_commit`` with LF line endings (git first, then the version snapshot)."""

    repo = Path(project.repo_path)
    if base_commit and (repo / ".git").exists():
        try:
            return _git(repo, "show", f"{base_commit}:{path}").decode("utf-8").replace("\r\n", "\n")
        except PatchUnavailable:
            pass
    candidates = []
    if project.snapshots_dir and base_commit:
        candidates.append(Path(project.snapshots_dir) / base_commit / path)
    candidates.append(repo / path)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_bytes().decode("utf-8").replace("\r\n", "\n")
    where = f"版本 {base_commit[:12]}" if base_commit else f"專案「{project.name}」"
    raise PatchUnavailable(f"找不到 {path} 在{where}的內容，無法還原修改後的程式碼。")


def patched_code(project: ProjectSettings, base_commit: str, patch: str) -> list[dict[str, Any]]:
    results = []
    for item in parse_unified_diff(patch):
        old_text = read_at_commit(project, base_commit, item["path"])
        try:
            new_text, changed = apply_hunks(old_text, item["hunks"])
        except ValueError as exc:
            raise PatchUnavailable(f"{item['path']}：{exc}") from exc
        results.append({"path": item["path"], "functions": touched_functions(new_text, changed), "file": new_text})
    return results
