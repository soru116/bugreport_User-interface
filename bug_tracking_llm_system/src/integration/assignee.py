"""Stage 4: assignee recommendation with the main system's AssigneeTriager (unchanged)."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from config import PipelineConfig
from integration.common import stage_result
from integration.config import IntegrationConfig
from integration.duplicate import parse_time, row_time
from modules.assignee_triager import AssigneeTriager


ASSIGNEE_CONFIG_KEYS = {
    name
    for name in PipelineConfig.__dataclass_fields__  # type: ignore[attr-defined]
    if name.startswith("assignee_") or name == "component_owner_mapping"
}


class TeamAssigneeRecommender:
    MODULE_LABEL = "主系統 AssigneeTriager：歷史／ownership／roster hybrid routing"
    IMPLEMENTATION = "bug_tracking_llm_system/src/modules/assignee_triager.py"

    def __init__(self, config: IntegrationConfig, *, extra_history_path: Path | None = None) -> None:
        self.config = config
        overrides = {key: value for key, value in config.assignee.items() if key in ASSIGNEE_CONFIG_KEYS}
        self.pipeline_config = PipelineConfig(**overrides)
        self.extra_history_path = Path(extra_history_path) if extra_history_path else None
        self.uses_workspace_history = False
        self.triager = AssigneeTriager(config=self.pipeline_config)
        self._project_triagers: dict[str, tuple[Path, AssigneeTriager]] = {}

    def _ensure_history_source(self) -> None:
        """Keep configured history as the general fallback, but enable project-scoped workspace
        history whenever it exists.  Imported project history is more relevant than a configured
        cross-project corpus (for example Astropy tickets versus the BMO training history)."""

        configured = self.pipeline_config.assignee_dataset_path
        if self.extra_history_path is not None and self.extra_history_path.exists():
            self.uses_workspace_history = True
            if configured is None or not Path(configured).exists():
                self.pipeline_config.assignee_dataset_path = self.extra_history_path
                self.triager = AssigneeTriager(config=self.pipeline_config)

    MAX_CUTOFF_FILES = 8

    def _project_history(self, project: str, cutoff: Any = None) -> tuple[Path, AssigneeTriager, int] | None:
        """Workspace history limited to one project (plus rows without a project), so a ticket is
        only routed to people who handled that project's tickets.  With a ``cutoff`` (the ticket's
        ``created_at``) only rows created before it are kept, the same rule as duplicate detection:
        a 2017 ticket is not routed with what happened in 2022.  The filtered copy is rebuilt when the
        workspace history changes; AssigneeTriager itself is unchanged.  Returns the path, the triager
        and how many later rows were left out."""

        source = self.extra_history_path
        if not project or source is None or not source.exists():
            return None
        query_time = parse_time(cutoff)
        base = re.sub(r"[^A-Za-z0-9_.-]+", "_", project).strip("._") or "project"
        suffix = f"__before_{query_time.strftime('%Y%m%dT%H%M%S')}" if query_time else ""
        target = source.parent / "by_project" / f"{base}{suffix}.jsonl"
        meta = target.with_suffix(".later")
        if not target.exists() or target.stat().st_mtime < source.stat().st_mtime:
            rows, matched_project, later = [], False, 0
            with source.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    row_project = str(row.get("project") or "").strip()
                    if row_project and row_project != project:
                        continue
                    matched_project = matched_project or row_project == project
                    created = row_time(row) if query_time else None
                    if created is not None and created >= query_time:
                        later += 1
                        continue
                    rows.append(line)
            if not matched_project:
                return None  # no imported history for this project: keep the configured/shared fallback
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.tmp")
            tmp.write_text("".join(row + "\n" for row in rows), encoding="utf-8")
            os.replace(tmp, target)
            meta.write_text(str(later), encoding="utf-8")
            if suffix:  # keep only the most recent per-ticket copies
                copies = sorted(target.parent.glob(f"{base}__before_*.jsonl"), key=lambda path: path.stat().st_mtime)
                for old in copies[: max(0, len(copies) - self.MAX_CUTOFF_FILES)]:
                    old.unlink(missing_ok=True)
                    old.with_suffix(".later").unlink(missing_ok=True)
                    self._project_triagers.pop(str(old), None)
        try:
            later = int(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            later = 0
        cached = self._project_triagers.get(str(target))
        if cached is None:
            overrides = {key: value for key, value in self.config.assignee.items() if key in ASSIGNEE_CONFIG_KEYS}
            config = PipelineConfig(**overrides)
            config.assignee_dataset_path = target
            cached = (target, AssigneeTriager(config=config))
            self._project_triagers[str(target)] = cached
        return cached[0], cached[1], later

    def recommend(self, structured_ticket: dict[str, Any], priority_output: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        self._ensure_history_source()
        triager, history_path = self.triager, self.pipeline_config.assignee_dataset_path
        project = str(structured_ticket.get("project") or "").strip()
        cutoff = structured_ticket.get("created_at") or structured_ticket.get("creation_time")
        scoped = self._project_history(project, cutoff) if self.uses_workspace_history else None
        later_rows = 0
        if scoped is not None:
            history_path, triager, later_rows = scoped
        result = triager.assign(structured_ticket, priority_output)
        history_rows = int((result.get("profile_stats") or {}).get("history_rows", 0))
        warnings = []
        status = "done"
        fallback_reason = ""
        if history_rows == 0:
            status = "fallback"
            fallback_reason = f"找不到負責人歷史資料：{history_path}"
            warnings.append("沒有負責人歷史資料，只能依 component-owner mapping 或轉人工分派。")
        elif result.get("needs_manual_triage"):
            warnings.append("模型信心不足或未通過 routing gate，建議人工分派；候選名單仍可參考。")
        return stage_result(
            stage="assignee",
            status=status,
            module=self.MODULE_LABEL,
            implementation=self.IMPLEMENTATION,
            output=result,
            warnings=warnings,
            fallback_reason=fallback_reason,
            details={
                "history_path": str(history_path or ""),
                "history_rows": history_rows,
                "later_rows_skipped": later_rows,
                "history_source": (f"工作台匯入的歷史資料（只用「{project}」及未標專案、比這張早建立的紀錄）" if scoped is not None
                                   else "工作台匯入的歷史資料" if self.uses_workspace_history else "設定檔指定的歷史資料"),
            },
            started=started,
        )
