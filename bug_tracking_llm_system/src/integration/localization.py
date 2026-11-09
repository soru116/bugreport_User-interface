"""Stage 5: fault localization with the main system's BugLocalizer (Stage-3 selected default).

``modules/bug_localizer.py`` imports ``utils/fault_localization_stage3.py``, which is byte-for-byte
the same file as ``fault_localization_feature_files/src/utils/fault_localization.py``.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

from integration.common import stage_result
from integration.config import IntegrationConfig, ProjectSettings
from integration.duplicate import parse_time
from modules.bug_localizer import BugLocalizer
from utils.fault_localization import format_user_facing_localization_result
from utils.fault_localization_stage3 import build_code_index, load_code_index


MAX_SNIPPET_LINES = 80
SYMBOL_KEYS = (
    "rank",
    "file_path",
    "symbol_qualified_name",
    "symbol_name",
    "symbol_kind",
    "start_line",
    "end_line",
    "score",
    "retrieval_score",
    "llm_score",
    "reason",
)


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失敗：{done.stderr.strip()[-300:]}")
    return done.stdout.strip()


def commit_at(repo: Path, created_at: str) -> str:
    """The last commit of the clone's checked-out branch made before ``created_at`` (or its tip)."""

    when = parse_time(created_at)
    from datetime import timezone

    stamp = when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S +0000") if when else ""
    args = ["rev-list", "-1"] + ([f"--before={stamp}"] if stamp else []) + ["HEAD"]
    commit = _git(repo, *args)
    if not commit:
        raise RuntimeError(f"{repo} 在 {created_at} 之前沒有任何 commit")
    return commit


def export_snapshot(repo: Path, commit: str, snapshot: Path) -> None:
    """Source tree at ``commit`` without .git (same as scripts/build_swebench_demo.py)."""

    if snapshot.is_dir() and any(snapshot.iterdir()):
        return
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "snapshot.zip"
        _git(repo, "archive", "--format=zip", "-o", str(archive), commit)
        partial = snapshot.with_name(snapshot.name + ".partial")
        if partial.exists():
            shutil.rmtree(partial)
        with zipfile.ZipFile(archive) as handle:
            handle.extractall(partial)
        partial.rename(snapshot)


def _trim_code(text: Any, limit: int = MAX_SNIPPET_LINES) -> str:
    lines = str(text or "").splitlines()
    if len(lines) <= limit:
        return "\n".join(lines)
    return "\n".join(lines[:limit] + [f"# …（其餘 {len(lines) - limit} 行省略）"])


class ProjectLocalizer:
    MODULE_LABEL = "錯誤定位：Stage-3 B1 structured + coverage-aware-v1"
    IMPLEMENTATION = "bug_tracking_llm_system/src/modules/bug_localizer.py → utils/fault_localization_stage3.py"

    def __init__(self, config: IntegrationConfig) -> None:
        self.config = config
        self._localizers: dict[str, BugLocalizer] = {}
        self._lock = threading.Lock()

    def _localizer(self, project: ProjectSettings) -> BugLocalizer:
        key = f"{project.name}|{project.repo_path}|{project.code_index_path or ''}"
        with self._lock:
            localizer = self._localizers.get(key)
            if localizer is None:
                if len(self._localizers) >= 16:  # snapshots of versioned repos: keep memory bounded
                    self._localizers.pop(next(iter(self._localizers)))
                localizer = BugLocalizer(
                    code_index_path=project.code_index_path,
                    top_k=int(self.config.localization.top_k),
                    embedding_backend=self.config.localization.embedding_backend,
                )
                self._localizers[key] = localizer
            return localizer

    def snapshot_project(self, project: ProjectSettings, base_commit: str) -> ProjectSettings | str:
        """The project as it was at ``base_commit`` (SWE-bench style), or an error message.

        The snapshot is a plain source tree made by scripts/build_swebench_demo.py; its code index is
        built once with the same Stage-3 builder and cached next to it."""

        folder = Path(project.snapshots_dir) / base_commit  # type: ignore[arg-type]
        if not folder.is_dir():
            return f"找不到 {project.name} 在版本 {base_commit[:12]} 的程式碼快照（{folder}）。請先執行 scripts/build_swebench_demo.py 準備這個版本。"
        index_path = Path(project.snapshots_dir) / f"{base_commit}.index.json"  # type: ignore[arg-type]
        if not index_path.is_file():
            index = build_code_index(folder, repository_name=project.name)
            index.save(index_path)
        return ProjectSettings(name=project.name, repo_path=folder, code_index_path=index_path, description=project.description)

    def index_status(self, project: ProjectSettings) -> dict[str, Any]:
        status: dict[str, Any] = {
            "project": project.name,
            "repo_path": str(project.repo_path),
            "repo_exists": Path(project.repo_path).is_dir(),
            "code_index_path": str(project.code_index_path or ""),
            "index_exists": bool(project.code_index_path and Path(project.code_index_path).is_file()),
        }
        if status["index_exists"]:
            stat = Path(project.code_index_path).stat()  # type: ignore[arg-type]
            status["index_updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime))
            status["index_size_kb"] = round(stat.st_size / 1024, 1)
        return status

    def build_index(self, project: ProjectSettings) -> dict[str, Any]:
        """Pre-build the code index (same builder as scripts/build_code_index.py)."""

        started = time.perf_counter()
        if not Path(project.repo_path).is_dir():
            raise ValueError(f"Repository path does not exist: {project.repo_path}")
        previous = None
        if project.code_index_path and Path(project.code_index_path).is_file():
            try:
                previous = load_code_index(project.code_index_path)
            except Exception:
                previous = None
        index = build_code_index(project.repo_path, repository_name=project.name, previous_index=previous)
        if project.code_index_path:
            Path(project.code_index_path).parent.mkdir(parents=True, exist_ok=True)
            index.save(project.code_index_path)
        with self._lock:
            for key in [key for key in self._localizers if key.startswith(f"{project.name}|")]:
                self._localizers.pop(key, None)
        return {
            **self.index_status(project),
            "files": len(index.settings.get("file_fingerprints", {})),
            "chunks": len(index.chunks),
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }

    def localize(
        self, structured_ticket: dict[str, Any], project: ProjectSettings | None, *, project_name: str = ""
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        started = time.perf_counter()
        if project is None:
            reason = (
                f"「{project_name}」在設定檔裡沒有程式碼庫（integration_config.json 的 projects 沒有這個專案的 repo_path），所以略過錯誤定位。"
                if project_name
                else "這張 Ticket 沒有指定專案，所以略過錯誤定位。"
            )
            return (
                stage_result(
                    stage="localization",
                    status="skipped",
                    module=self.MODULE_LABEL,
                    implementation=self.IMPLEMENTATION,
                    warnings=[reason],
                    details={"skip_reason": "project_without_repository" if project_name else "no_project"},
                    started=started,
                ),
                None,
            )
        base_commit = str(structured_ticket.get("base_commit") or "").strip()
        snapshot_used = False
        version_note = ""
        if project.snapshots_dir and not base_commit and (Path(project.repo_path) / ".git").exists():
            # A versioned project (SWE-bench style) whose ticket names no commit, e.g. a GitHub issue:
            # use the code as it was when the ticket was reported.  The clone itself is only the
            # source of the snapshots; its folder may hold nothing but .git.
            reported = str(structured_ticket.get("created_at") or structured_ticket.get("creation_time") or "")
            try:
                base_commit = commit_at(Path(project.repo_path), reported)
                export_snapshot(Path(project.repo_path), base_commit, Path(project.snapshots_dir) / base_commit)
            except Exception as exc:
                return (
                    stage_result(stage="localization", status="failed", module=self.MODULE_LABEL, implementation=self.IMPLEMENTATION,
                                 error=f"這張 Ticket 沒有指定程式碼版本，改用回報日期的版本時失敗：{exc}", started=started),
                    None,
                )
            day = reported[:10] if parse_time(reported) else ""
            version_note = (f"這張 Ticket 沒有指定程式碼版本，改用回報日期 {day} 當時的程式碼（commit {base_commit[:12]}）。"
                            if day else f"這張 Ticket 沒有指定程式碼版本，也沒有回報日期，改用最新的程式碼（commit {base_commit[:12]}）。")
        if project.snapshots_dir and base_commit:
            snapshot = self.snapshot_project(project, base_commit)
            if isinstance(snapshot, str):
                return (
                    stage_result(stage="localization", status="failed", module=self.MODULE_LABEL, implementation=self.IMPLEMENTATION, error=snapshot, started=started),
                    None,
                )
            project = snapshot
            snapshot_used = True
        if (Path(project.repo_path).is_dir() and not (project.code_index_path and Path(project.code_index_path).is_file())
                and not any(item.name != ".git" for item in Path(project.repo_path).iterdir())):
            return (
                stage_result(stage="localization", status="failed", module=self.MODULE_LABEL, implementation=self.IMPLEMENTATION,
                             error=f"程式碼資料夾是空的（只有 .git）：{project.repo_path}。請確認 integration_config.json 的 repo_path，"
                                   "或在 Ticket 裡提供 base_commit。", started=started),
                None,
            )
        if not Path(project.repo_path).is_dir() and not (project.code_index_path and Path(project.code_index_path).is_file()):
            return (
                stage_result(
                    stage="localization",
                    status="failed",
                    module=self.MODULE_LABEL,
                    implementation=self.IMPLEMENTATION,
                    error=f"找不到 Repository：{project.repo_path}",
                    started=started,
                ),
                None,
            )

        ticket = dict(structured_ticket)
        ticket.setdefault("repo", project.name)
        raw = self._localizer(project).localize(ticket, str(project.repo_path))
        if project_snapshot_commit := (base_commit if snapshot_used else ""):
            # A snapshot is a plain source tree without .git, so the localizer's own `git rev-parse`
            # reports whatever repository happens to contain the folder.  The code it read is the
            # ticket's commit.
            raw["base_commit"] = project_snapshot_commit
        user_facing = format_user_facing_localization_result(
            raw,
            include_code_preview=bool(self.config.localization.include_code_preview),
            top_k=int(self.config.localization.top_k),
        )
        symbols = []
        for row in raw.get("stage3_ranked_symbols", []) or []:
            item = {key: row.get(key) for key in SYMBOL_KEYS if key in row}
            item["code"] = _trim_code(row.get("code_text"))
            symbols.append(item)
        confidence = raw.get("confidence") if isinstance(raw.get("confidence"), dict) else {}
        output = {
            "project": project.name,
            "repository": raw.get("repo") or project.name,
            "base_commit": base_commit or raw.get("base_commit", ""),
            "base_commit_source": "ticket" if structured_ticket.get("base_commit") else ("reported_date" if version_note else ""),
            "status": user_facing.get("status"),
            "summary": user_facing.get("summary", {}),
            "confidence_level": raw.get("confidence_level"),
            "confidence": confidence,
            "should_manual_review": raw.get("should_manual_review"),
            "recommend_patch_generation": raw.get("recommend_patch_generation"),
            "ranked_symbols": symbols,
            "suspicious_files": user_facing.get("top_k_suspicious_files", []),
            "engineering_guidance": user_facing.get("engineering_guidance", []),
            "input_validation": raw.get("input_validation", {}),
        }
        warnings = ([version_note] if version_note else []) + list(user_facing.get("warnings") or [])
        if raw.get("should_manual_review"):
            warnings.append("定位信心未達自動化門檻，請工程師以候選清單為參考人工確認。")
        return (
            stage_result(
                stage="localization",
                status="done",
                module=self.MODULE_LABEL,
                implementation=self.IMPLEMENTATION,
                output=output,
                warnings=warnings,
                details={
                    "method": user_facing.get("method", {}),
                    "reference_metrics": "SWE-bench Lite 300 題 file-level：Top-1 0.5500、Top-3 0.7567、Top-5 0.8133、MRR 0.6559",
                },
                started=started,
            ),
            raw,
        )
