"""Stage 4: assignee recommendation with the main system's AssigneeTriager (unchanged)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from config import PipelineConfig
from integration.common import stage_result
from integration.config import IntegrationConfig
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

    def _ensure_history_source(self) -> None:
        """Use the workspace's imported history only when no configured history file exists."""

        configured = self.pipeline_config.assignee_dataset_path
        if configured is not None and Path(configured).exists():
            return
        if self.extra_history_path is not None and self.extra_history_path.exists():
            if self.pipeline_config.assignee_dataset_path != self.extra_history_path:
                self.pipeline_config.assignee_dataset_path = self.extra_history_path
                self.triager = AssigneeTriager(config=self.pipeline_config)
            self.uses_workspace_history = True

    def recommend(self, structured_ticket: dict[str, Any], priority_output: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        self._ensure_history_source()
        result = self.triager.assign(structured_ticket, priority_output)
        history_path = self.pipeline_config.assignee_dataset_path
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
                "history_source": "工作台匯入的歷史資料" if self.uses_workspace_history else "設定檔指定的歷史資料",
            },
            started=started,
        )
