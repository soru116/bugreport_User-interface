"""Stage 5: fault localization with the main system's BugLocalizer (Stage-3 selected default).

``modules/bug_localizer.py`` imports ``utils/fault_localization_stage3.py``, which is byte-for-byte
the same file as ``fault_localization_feature_files/src/utils/fault_localization.py``.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from integration.common import stage_result
from integration.config import IntegrationConfig, ProjectSettings
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
        with self._lock:
            localizer = self._localizers.get(project.name)
            if localizer is None:
                localizer = BugLocalizer(
                    code_index_path=project.code_index_path,
                    top_k=int(self.config.localization.top_k),
                    embedding_backend=self.config.localization.embedding_backend,
                )
                self._localizers[project.name] = localizer
            return localizer

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
            self._localizers.pop(project.name, None)
        return {
            **self.index_status(project),
            "files": len(index.settings.get("file_fingerprints", {})),
            "chunks": len(index.chunks),
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }

    def localize(self, structured_ticket: dict[str, Any], project: ProjectSettings | None) -> tuple[dict[str, Any], dict[str, Any] | None]:
        started = time.perf_counter()
        if project is None:
            return (
                stage_result(
                    stage="localization",
                    status="skipped",
                    module=self.MODULE_LABEL,
                    implementation=self.IMPLEMENTATION,
                    warnings=["此 Ticket 沒有指定專案／Repository，無法執行錯誤定位。請在設定檔 projects 加入 repo_path。"],
                    started=started,
                ),
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
            "base_commit": raw.get("base_commit", ""),
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
        warnings = list(user_facing.get("warnings") or [])
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
