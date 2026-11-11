"""Integrated ticket workflow: import -> extraction -> duplicate review -> priority -> assignee -> localization.

The duplicate step is a human gate, following the semi-automatic design of the DUP project.
Assignee confirmation does not block localization (same as the main orchestrator, where a
manual-triage routing result does not pause the pipeline).
"""

from __future__ import annotations

import json
import threading
import traceback
from datetime import datetime, timezone
from typing import Any, Callable

from integration.assignee import TeamAssigneeRecommender
from integration.common import stage_result, utc_now
from integration.commit_message import TeamCommitMessageGenerator
from integration.config import IntegrationConfig
from integration.duplicate import TeamDuplicateDetector, parse_time, row_time
from integration.extraction import ToJsonExtractor, raw_ticket_id, raw_title
from integration.localization import ProjectLocalizer
from integration.patch import TeamPatchGenerator
from integration.priority import TeamPriorityPredictor
from integration.store import TicketStore
from modules.assignee_feedback import append_assignee_feedback, build_assignee_feedback_record


STAGES = ("extraction", "duplicate", "priority", "assignee", "localization")
STAGE_LABELS = {
    "extraction": "轉換 JSON",
    "duplicate": "重複偵測",
    "priority": "優先級判定",
    "assignee": "分派負責人",
    "localization": "錯誤定位",
    "patch": "補丁生成",
}
# Patch generation runs only when an engineer asks for it, after localization; it is not part of the
# automatic flow above and is cleared whenever localization changes.
TERMINAL_STATUSES = {"duplicate", "processed", "failed"}


class IntegratedPipeline:
    def __init__(
        self,
        config: IntegrationConfig,
        *,
        store: TicketStore | None = None,
        extractor: ToJsonExtractor | None = None,
        duplicate_detector: TeamDuplicateDetector | None = None,
        priority_predictor: TeamPriorityPredictor | None = None,
        assignee_recommender: TeamAssigneeRecommender | None = None,
        localizer: ProjectLocalizer | None = None,
        patcher: TeamPatchGenerator | None = None,
        commit_messenger: TeamCommitMessageGenerator | None = None,
    ) -> None:
        self.config = config
        self.store = store or TicketStore(config.store_dir)
        self.extractor = extractor or ToJsonExtractor(config)
        self.duplicate_detector = duplicate_detector or TeamDuplicateDetector(config)
        self.priority_predictor = priority_predictor or TeamPriorityPredictor(config)
        self.assignee_recommender = assignee_recommender or TeamAssigneeRecommender(
            config, extra_history_path=self.store.history_path
        )
        self.localizer = localizer or ProjectLocalizer(config)
        self.patcher = patcher or TeamPatchGenerator(config)
        self.commit_messenger = commit_messenger or TeamCommitMessageGenerator(config)
        # Heavy model calls are serialized so concurrent UI requests do not fight for CPU/RAM.
        # Conversion + duplicate detection (Code Llama, SBERT) and the later steps (PRI, assignee,
        # localization) use different models, so the two phases may run at the same time on different
        # tickets; each phase is still one-at-a-time. All record writes go through the store's lock.
        self._run_lock = threading.Lock()
        self._after_lock = threading.Lock()
        self._patch_lock = threading.Lock()  # one model call at a time on the GPU

    # ------------------------------------------------------------------ import
    def import_raw(self, raws: list[dict[str, Any]], *, source: str, project: str = "") -> list[dict[str, Any]]:
        records = []
        project_name = project or self.config.default_project
        if not project_name and len(self.config.projects) == 1:
            project_name = next(iter(self.config.projects))
        for raw in raws:
            ticket_id = raw_ticket_id(raw)
            key = self.store.new_key(ticket_id)
            now = utc_now()
            record = {
                "key": key,
                "ticket_id": ticket_id or key,
                "title": raw_title(raw) or "(未命名 Ticket)",
                "project": str(raw.get("project") or project_name or ""),
                "source": source,
                "created_at": now,
                "raw": raw,
                "status": "new",
                "current_stage": "",
                "error": "",
                "stages": {stage: {"stage": stage, "status": "pending"} for stage in STAGES},
                "structured_ticket": {},
                "decisions": {},
                "attention": [],
                "events": [{"at": now, "message": f"由 {source} 匯入"}],
            }
            records.append(self.store.save(record))
        return records

    def import_history(self, rows: list[dict[str, Any]], *, project: str = "") -> int:
        """Add rows to the comparison history. Rows without their own ``project`` get ``project``
        so they are only compared with that project's tickets (an empty value keeps them shared)."""
        normalized = []
        for row in rows:
            ticket_id = raw_ticket_id(row)
            if not ticket_id:
                continue
            item = dict(row)
            item["ticket_id"] = ticket_id
            item.setdefault("title", raw_title(row))
            if project and not str(item.get("project") or "").strip():
                item["project"] = project
            normalized.append(item)
        return self.store.upsert_history(normalized)

    # ----------------------------------------------------------------- helpers
    def _event(self, record: dict[str, Any], message: str) -> None:
        record.setdefault("events", []).append({"at": utc_now(), "message": message})

    def _set_stage(self, key: str, stage: str, result: dict[str, Any], *, extra: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        def mutate(record: dict[str, Any]) -> None:
            record["stages"][stage] = result
            label = STAGE_LABELS[stage]
            status_text = {"done": "完成", "fallback": "完成（使用備援）", "skipped": "略過", "failed": "失敗"}.get(result["status"], result["status"])
            self._event(record, f"{label}：{status_text}" + (f"（{result['fallback_reason']}）" if result.get("fallback_reason") else ""))
            if extra:
                extra(record)

        if stage in {"duplicate", "priority", "localization", "patch"} and result.get("output"):
            self.store.write_artifact(key, f"{stage}_result.json", result)
        return self.store.update(key, mutate)

    def _mark_running(self, key: str, stage: str) -> None:
        def mutate(record: dict[str, Any]) -> None:
            record["status"] = "running"
            record["current_stage"] = stage
            record["stages"][stage] = {"stage": stage, "status": "running", "started_at": utc_now()}

        self.store.update(key, mutate)

    def _fail(self, key: str, stage: str, exc: BaseException) -> dict[str, Any]:
        error = f"{type(exc).__name__}: {exc}"

        def mutate(record: dict[str, Any]) -> None:
            record["status"] = "failed"
            record["current_stage"] = stage
            record["error"] = error
            record["stages"][stage] = stage_result(
                stage=stage,
                status="failed",
                module=record["stages"].get(stage, {}).get("module", STAGE_LABELS[stage]),
                implementation=record["stages"].get(stage, {}).get("implementation", ""),
                error=error,
                details={"traceback": traceback.format_exc()[-3000:]},
            )
            self._event(record, f"{STAGE_LABELS[stage]}：失敗（{error}）")

        return self.store.update(key, mutate)

    def get(self, key: str) -> dict[str, Any]:
        record = self.store.get(key)
        if record is None:
            raise KeyError(key)
        return record

    # ------------------------------------------------------- stage 1 and 2
    def run_until_review(self, key: str) -> dict[str, Any]:
        with self._run_lock:
            return self._run_until_review(key)

    def _run_until_review(self, key: str) -> dict[str, Any]:
        def reset(rec: dict[str, Any]) -> None:
            if any(item.get("status") not in (None, "pending") for item in rec["stages"].values()):
                self._event(rec, "重新執行整個流程")
            rec["stages"] = {name: {"stage": name, "status": "pending"} for name in STAGES}
            rec["decisions"] = {}
            rec["review_marks"] = {}
            rec["attention"] = []
            rec["error"] = ""

        record = self.store.update(key, reset)
        stage = "extraction"
        try:
            self._mark_running(key, stage)
            structured, result = self.extractor.extract(record["raw"])
            if not structured.get("ticket_id"):
                structured["ticket_id"] = record["ticket_id"]
            if record.get("project"):
                structured.setdefault("project", record["project"])
            structured.setdefault("created_at", record["created_at"])

            def store_structured(rec: dict[str, Any]) -> None:
                rec["structured_ticket"] = structured
                rec["ticket_id"] = structured.get("ticket_id") or rec["ticket_id"]
                rec["title"] = structured.get("title") or rec["title"]

            self._set_stage(key, stage, result, extra=store_structured)

            stage = "duplicate"
            self._mark_running(key, stage)
            result = self.duplicate_detector.detect(
                structured,
                extra_history_paths=[self.store.history_path],
                exclude_ids={record["ticket_id"], key},
            )
            output = result.get("output", {})

            def after_duplicate(rec: dict[str, Any]) -> None:
                rec["current_stage"] = ""
                if result["status"] == "failed":
                    rec["status"] = "failed"
                    rec["error"] = result.get("error", "")
                elif output.get("is_duplicate"):
                    rec["status"] = "duplicate"
                    rec["decisions"]["duplicate"] = {
                        "decision": "duplicate",
                        "duplicate_of": output.get("duplicate_of"),
                        "by": "auto_threshold",
                        "at": utc_now(),
                    }
                elif output.get("needs_review"):
                    rec["status"] = "awaiting_duplicate_review"
                else:
                    rec["status"] = "ready_to_continue"
                    rec["decisions"]["duplicate"] = {
                        "decision": "not_duplicate",
                        "by": "no_candidates" if result["status"] == "skipped" else "below_review_threshold",
                        "at": utc_now(),
                    }

            return self._set_stage(key, stage, result, extra=after_duplicate)
        except Exception as exc:  # pragma: no cover - defensive guard, reported in the UI
            return self._fail(key, stage, exc)

    def decide_duplicate(self, key: str, decision: str, *, duplicate_of: str = "", note: str = "", by: str = "engineer") -> dict[str, Any]:
        decision = decision.strip().lower()
        if decision not in {"duplicate", "not_duplicate"}:
            raise ValueError("decision must be 'duplicate' or 'not_duplicate'")
        record = self.get(key)
        if record["status"] not in {"awaiting_duplicate_review", "ready_to_continue", "duplicate"}:
            raise ValueError(f"Ticket 目前狀態為 {record['status']}，不能進行重複判定。")

        def mutate(rec: dict[str, Any]) -> None:
            actor = "全自動模式" if by == "workspace:auto" else "工程師"
            rec["decisions"]["duplicate"] = {
                "decision": decision,
                "duplicate_of": duplicate_of or None,
                "note": note,
                "by": by,
                "at": utc_now(),
            }
            if decision == "duplicate":
                rec["status"] = "duplicate"
                self._event(rec, f"{actor}確認為重複，對應 {duplicate_of or '（未指定）'}，流程結束")
            else:
                rec["status"] = "ready_to_continue"
                self._event(rec, f"{actor}確認非重複，繼續後續流程")

        updated = self.store.update(key, mutate)
        if decision == "duplicate":
            structured = updated.get("structured_ticket") or {}
            self.store.upsert_history([{**_history_row(structured), "duplicate_of": duplicate_of, "status": "duplicate"}])
        return updated

    # ------------------------------------------------------ stage 3, 4 and 5
    def run_after_duplicate(self, key: str, *, priority_result: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._after_lock:
            return self._run_after_duplicate(key, priority_result=priority_result)

    def _run_after_duplicate(self, key: str, *, priority_result: dict[str, Any] | None = None) -> dict[str, Any]:
        record = self.get(key)
        if record["status"] != "ready_to_continue":
            raise ValueError(f"Ticket 目前狀態為 {record['status']}，需要先完成重複判定。")
        structured = record["structured_ticket"]
        candidates = (record["stages"].get("duplicate", {}).get("output") or {}).get("top_k_candidates", [])
        stage = "priority"
        try:
            self._mark_running(key, stage)
            result = priority_result or self.priority_predictor.predict(structured, candidates)
            self._set_stage(key, stage, result)
            priority_output = result.get("output") or {"predicted_priority": "P3", "confidence": 0.0}

            stage = "assignee"
            self._mark_running(key, stage)
            result = self.assignee_recommender.recommend(structured, priority_output)
            self._set_stage(key, stage, result)

            stage = "localization"
            self._mark_running(key, stage)
            project = self.config.project(record.get("project"))
            result, raw = self.localizer.localize(structured, project, project_name=str(record.get("project") or ""))
            if raw is not None:
                self.store.write_artifact(key, "bug_location_raw.json", raw)
            assignee_output = (self.get(key)["stages"]["assignee"].get("output") or {})

            def finish(rec: dict[str, Any]) -> None:
                rec["status"] = "processed"
                rec["current_stage"] = ""
                attention = []
                if assignee_output.get("needs_manual_triage"):
                    attention.append("負責人需人工分派")
                elif "assignee" not in rec["decisions"]:
                    attention.append("待確認負責人")
                localization_output = result.get("output") or {}
                if result["status"] in {"skipped", "failed"}:
                    attention.append("未完成錯誤定位")
                elif localization_output.get("should_manual_review"):
                    attention.append("定位結果需人工確認")
                for name in STAGES:
                    if rec["stages"].get(name, {}).get("status") == "fallback":
                        attention.append(f"{STAGE_LABELS[name]}使用備援")
                rec["attention"] = attention

            updated = self._set_stage(key, stage, result, extra=finish)
            self.store.upsert_history([_history_row(updated["structured_ticket"], updated)])
            return updated
        except Exception as exc:  # pragma: no cover - defensive guard, reported in the UI
            return self._fail(key, stage, exc)

    def rerun_localization(self, key: str, *, project: str | None = None) -> dict[str, Any]:
        with self._after_lock:
            record = self.get(key)
            if project is not None:
                record = self.store.update(key, lambda rec: rec.update({"project": project}))
            if not record.get("structured_ticket"):
                raise ValueError("Ticket 尚未完成 JSON 轉換。")
            try:
                self._mark_running(key, "localization")
                result, raw = self.localizer.localize(
                    record["structured_ticket"], self.config.project(record.get("project")), project_name=str(record.get("project") or "")
                )
                if raw is not None:
                    self.store.write_artifact(key, "bug_location_raw.json", raw)

                def finish(rec: dict[str, Any]) -> None:
                    rec["status"] = "processed" if rec["status"] == "running" else rec["status"]
                    rec["current_stage"] = ""
                    if rec["stages"].pop("patch", None):
                        self._event(rec, "錯誤定位結果變了，先前產生的補丁已清除")
                    self._drop_patch_drafts(rec)
                    rec["attention"] = [item for item in rec.get("attention", []) if "定位" not in item]
                    if result["status"] in {"skipped", "failed"}:
                        rec["attention"].append("未完成錯誤定位")
                    elif (result.get("output") or {}).get("should_manual_review"):
                        rec["attention"].append("定位結果需人工確認")

                return self._set_stage(key, "localization", result, extra=finish)
            except Exception as exc:  # pragma: no cover
                return self._fail(key, "localization", exc)

    # ------------------------------------------------------ patch (on demand)
    def patch_precheck(self, key: str) -> dict[str, Any]:
        """Raise ValueError with a readable reason when the patch step cannot start."""

        record = self.get(key)
        localization = record["stages"].get("localization") or {}
        if localization.get("status") != "done":
            raise ValueError("要先完成錯誤定位，才能產生補丁。")
        if not self.store.artifact_path(key, "bug_location_raw.json").is_file():
            raise ValueError("找不到這張 Ticket 的錯誤定位原始結果，請按「重新定位」後再試。")
        if self.config.project(record.get("project")) is None:
            raise ValueError("這張 Ticket 沒有對應的專案程式碼。")
        return record

    def accept_localization_for_automatic(self, key: str) -> dict[str, Any]:
        """Remove the human localization gate in automatic mode without hiding its evidence."""

        def mutate(rec: dict[str, Any]) -> None:
            stage = rec.get("stages", {}).get("localization") or {}
            output = stage.get("output") or {}
            if stage.get("status") != "done" or not output.get("should_manual_review"):
                return
            output["automation_gate_override"] = {
                "applied": True,
                "reason": "automatic_mode",
                "original_should_manual_review": True,
                "original_recommend_patch_generation": bool(output.get("recommend_patch_generation")),
            }
            output["should_manual_review"] = False
            output["recommend_patch_generation"] = True
            output.setdefault("summary", {})["message"] = (
                "全自動模式已略過定位信心門檻；保留原始分數供後續審查。"
            )
            stage["warnings"] = [
                warning for warning in stage.get("warnings", [])
                if "定位信心未達自動化門檻" not in warning
            ]
            rec["attention"] = [item for item in rec.get("attention", []) if item != "定位結果需人工確認"]
            self._event(rec, "全自動模式已略過錯誤定位的人工確認門檻")

        return self.store.update(key, mutate)

    def generate_patch(self, key: str, *, automatic: bool = False, automatic_retry: int = 0) -> dict[str, Any]:
        with self._patch_lock:
            record = self.patch_precheck(key)
            previous = {"status": record["status"], "current_stage": record.get("current_stage", "")}
            patch_kwargs: dict[str, Any] = {}
            if automatic_retry:
                prior_output = (record.get("stages", {}).get("patch") or {}).get("output") or {}
                reasons = [str(item.get("reason") or "unknown") for item in prior_output.get("candidates") or []]
                counts = {reason: reasons.count(reason) for reason in sorted(set(reasons))}
                reason_summary = ", ".join(f"{reason} ({count})" for reason, count in counts.items()) or "unknown"
                patch_kwargs = {
                    "seed_override": int(self.config.patch.seed) + int(self.config.patch.candidates) * automatic_retry,
                    "retry_feedback": (
                        "The previous attempt produced no valid patch. Candidate rejection reasons: "
                        f"{reason_summary}. Re-evaluate the selected function and its related helper calls. "
                        "Do not repeat the original function unchanged; make only an evidence-based minimal change, "
                        "or return NO_CHANGE if the public evidence is still insufficient."
                    ),
                }

            def running(rec: dict[str, Any]) -> None:
                rec["status"], rec["current_stage"] = "running", "patch"
                rec["stages"]["patch"] = {"stage": "patch", "status": "running", "started_at": utc_now()}
                self._drop_patch_drafts(rec)

            self.store.update(key, running)
            try:
                raw = json.loads(self.store.artifact_path(key, "bug_location_raw.json").read_text(encoding="utf-8"))
                stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                output_dir = self.store.artifact_path(key, "patch_runs") / stamp
                result = self.patcher.generate(
                    record,
                    self.config.project(record.get("project")),
                    raw,
                    output_dir=output_dir,
                    **patch_kwargs,
                )
                output = result.get("output") or {}
                if automatic and str(raw.get("confidence_level") or "").lower() != "high":
                    result["warnings"] = [
                        warning for warning in result.get("warnings", [])
                        if not warning.startswith("錯誤定位信心為")
                    ]
                    result.setdefault("warnings", []).append(
                        "全自動模式已略過錯誤定位信心門檻；生成結果仍須通過所有補丁安全檢查。"
                    )
                if automatic_retry:
                    output["automatic_retry_count"] = automatic_retry
                    output["automatic_retry_reason"] = "no_valid_candidate"
                    output["automatic_retry_seed"] = patch_kwargs["seed_override"]
                    result.setdefault("warnings", []).append(
                        f"全自動模式第一次沒有有效補丁，已使用新 seeds 與 no-op 回饋自動重試 {automatic_retry} 次。"
                    )
                if result.get("status") == "done" and output.get("patch_status") == "generated" and output.get("patch"):
                    commit = self._commit_message(record, str(output["patch"]), output.get("official"))
                    output["commit_message"] = commit
                    if commit["status"] == "failed":
                        result.setdefault("warnings", []).append("補丁已生成，但提交訊息生成失敗：" + commit["error"])
            except Exception as exc:  # pragma: no cover - defensive; the patcher reports its own failures
                result = stage_result(stage="patch", status="failed", module="補丁生成", implementation="", error=f"{type(exc).__name__}: {exc}",
                                      details={"traceback": traceback.format_exc()[-3000:]})

            def finish(rec: dict[str, Any]) -> None:
                rec["status"], rec["current_stage"] = previous["status"], previous["current_stage"]
                attention = [item for item in rec.get("attention", []) if "補丁" not in item]
                output = result.get("output") or {}
                if result["status"] == "done" and output.get("patch_status") == "generated":
                    attention.append("補丁待人工審查")
                rec["attention"] = attention

            return self._set_stage(key, "patch", result, extra=finish)

    def skip_patch(self, key: str, reason: str) -> dict[str, Any]:
        """Automatic mode on a ticket that cannot be patched (no code, localization skipped...).
        Only step 6 is marked skipped; the ticket keeps its status."""

        result = stage_result(stage="patch", status="skipped", module="補丁生成", implementation="",
                              warnings=[f"全自動模式略過補丁生成：{reason}"])
        return self._set_stage(key, "patch", result)

    # ------------------------------------------------------- commit messages
    def _commit_message(self, record: dict[str, Any], patch: str, official: dict[str, Any] | None) -> dict[str, Any]:
        try:
            return self.commit_messenger.generate(record, patch, official=official)
        except Exception as exc:  # commit drafting is optional; never discard a valid patch
            return {"status": "failed", "commit_message": "", "model": self.config.commit_message.model,
                    "generation_method": "code_llama_instruct_zero_shot", "error": f"{type(exc).__name__}: {exc}"}

    @staticmethod
    def _drop_patch_drafts(rec: dict[str, Any]) -> None:
        """Drop drafts of the previous generated patch (records made before step 7 kept them here)."""
        drafts = rec.get("commit_messages") or {}
        for source in [name for name in drafts if name == "current" or name.startswith("candidate-")]:
            drafts.pop(source, None)

    def commit_message_patch(self, record: dict[str, Any], source: str) -> tuple[str, dict[str, Any] | None]:
        if source == "current":
            output = (record["stages"].get("patch") or {}).get("output") or {}
            return (str(output.get("patch") or "") if output.get("patch_status") == "generated" else ""), output.get("official")
        if source == "experiment":
            experiment = self.patcher.experiment_record(str(record.get("ticket_id") or "")) or {}
            return str(experiment.get("patch") or ""), experiment.get("official")
        raise ValueError("不明的補丁來源")

    def commit_message_precheck(self, key: str, source: str) -> None:
        patch, _ = self.commit_message_patch(self.get(key), source)
        if not patch:
            raise ValueError("找不到這個補丁，無法產生提交訊息。")

    def draft_commit_message(self, key: str, source: str) -> dict[str, Any]:
        """Draft (again) without regenerating the patch: the current patch (step 7) or the 6/36 run's patch."""

        with self._patch_lock:  # same GPU queue as patch generation
            record = self.get(key)
            patch, official = self.commit_message_patch(record, source)

            def store(value: dict[str, Any]) -> Callable[[dict[str, Any]], None]:
                def mutate(rec: dict[str, Any]) -> None:
                    if source == "current":
                        output = (rec["stages"].get("patch") or {}).get("output")
                        if output is not None:
                            output["commit_message"] = value
                            rec["stages"]["patch"]["warnings"] = [w for w in rec["stages"]["patch"].get("warnings", []) if "提交訊息" not in w]
                    else:
                        rec.setdefault("commit_messages", {})[source] = value
                return mutate

            self.store.update(key, store({"status": "running", "started_at": utc_now()}))
            result = self._commit_message(record, patch, official)

            def finish(rec: dict[str, Any]) -> None:
                store(result)(rec)
                label = "補丁" if source == "current" else "36 題實驗的補丁"
                if result["status"] == "generated":
                    self._event(rec, f"提交訊息（{label}）：{result['commit_message']}")
                else:
                    self._event(rec, f"提交訊息（{label}）失敗：{result.get('error', '')}")

            return self.store.update(key, finish)

    # ------------------------------------------------- duplicate review aids
    def find_history_ticket(self, ticket_id: str) -> dict[str, Any] | None:
        """Full historical ticket row (configured history files first, then workspace history)."""

        wanted = str(ticket_id).strip()
        rows, _ = self.duplicate_detector.history_rows([self.store.history_path])
        for row in rows:
            if str(row.get("ticket_id") or row.get("bug_id") or row.get("issue_id") or row.get("id") or "").strip() == wanted:
                return row
        return None

    # ------------------------------------------------------------ deleting
    def delete_ticket(self, key: str) -> dict[str, Any]:
        """Delete a ticket, its result files, and the history row the workspace added for it when it was
        processed. History rows imported as historical tickets are left alone."""

        record = self.store.get(key)
        if record is None:
            raise KeyError(key)
        ticket_id = str(record.get("ticket_id") or key)
        removed = self.store.remove_history(
            lambda row: str(row.get("ticket_id")) == ticket_id and row.get("source") == "integrated_workspace"
        )
        self.store.delete(key)
        return {"deleted": key, "history_rows_removed": removed}

    def project_usage(self, project: str) -> dict[str, Any]:
        """What deleting a project would remove (``project`` "" means tickets without a project)."""

        tickets = [record for record in self.store.list() if str(record.get("project") or "") == project]
        history = [row for row in self.store.history_rows() if project and str(row.get("project") or "") == project]
        return {"project": project, "tickets": len(tickets), "ticket_keys": [record["key"] for record in tickets], "history_rows": len(history)}

    def delete_project_data(self, project: str, *, tickets: bool = True, history: bool = True,
                            keep: set[str] | None = None) -> dict[str, Any]:
        """``keep``: tickets still running a step; the workspace deletes them when that step ends."""

        removed_tickets = []
        keep = keep or set()
        if tickets:
            for record in self.store.list():
                if str(record.get("project") or "") == project and record["key"] not in keep:
                    self.store.delete(record["key"])
                    removed_tickets.append(record["key"])
        removed_history = 0
        if history and project:
            removed_history = self.store.remove_history(lambda row: str(row.get("project") or "") == project)
        return {"tickets_removed": removed_tickets, "history_rows_removed": removed_history}

    def mark_candidate(self, key: str, candidate_id: str, mark: str, *, note: str = "") -> dict[str, Any]:
        """Engineer's review note on one duplicate candidate: possible / not / cleared."""

        mark = mark.strip().lower()
        if mark not in {"possible", "not", ""}:
            raise ValueError("mark must be 'possible', 'not' or ''")
        candidate_id = str(candidate_id).strip()
        if not candidate_id:
            raise ValueError("candidate_id is required")
        labels = {"possible": "標註為可能重複", "not": "標註為不是重複", "": "清除標註"}

        def mutate(rec: dict[str, Any]) -> None:
            marks = rec.setdefault("review_marks", {})
            if mark or note.strip():
                marks[candidate_id] = {"mark": mark, "note": note.strip(), "at": utc_now()}
            else:
                marks.pop(candidate_id, None)
            self._event(rec, f"候選 {candidate_id}：{labels[mark]}" + (f"（{note.strip()}）" if note.strip() else ""))

        return self.store.update(key, mutate)

    def set_pinned(self, key: str, pinned: bool) -> dict[str, Any]:
        """Pin a ticket to the top of the workspace home page (display only; does not touch any stage)."""

        def mutate(rec: dict[str, Any]) -> None:
            rec["pinned"] = bool(pinned)
            rec["pinned_at"] = utc_now() if pinned else ""

        return self.store.update(key, mutate)

    def confirm_assignee(self, key: str, assignee: str, *, decision: str = "accept", reviewer: str = "", note: str = "") -> dict[str, Any]:
        assignee = assignee.strip()
        if not assignee:
            raise ValueError("請選擇負責人。")
        record = self.get(key)
        prediction = record["stages"].get("assignee", {}).get("output") or {}
        feedback_saved = ""
        if prediction and decision != "automatic":
            try:
                feedback = build_assignee_feedback_record(
                    ticket_json=record["structured_ticket"],
                    prediction=prediction,
                    final_assignee=assignee,
                    reviewer=reviewer,
                    source="integrated_workspace",
                    notes=note,
                    feedback_origin="human_confirmation",
                    source_system="integrated_workspace",
                    source_event_id=f"{key}:{utc_now()}",
                )
                append_assignee_feedback(self.store.feedback_path, feedback)
                feedback_saved = str(self.store.feedback_path)
            except Exception as exc:  # feedback schema rejects incomplete records; keep the decision anyway
                feedback_saved = f"未寫入 feedback：{exc}"

        def mutate(rec: dict[str, Any]) -> None:
            rec["decisions"]["assignee"] = {
                "assignee": assignee,
                "decision": decision,
                "reviewer": reviewer,
                "note": note,
                "at": utc_now(),
                "feedback": feedback_saved,
            }
            rec["attention"] = [item for item in rec.get("attention", []) if "負責人" not in item]
            label = {
                "accept": "採用建議",
                "override": "改選其他人",
                "manual": "人工指定",
                "automatic": "全自動模式採用建議",
            }.get(decision, decision)
            self._event(rec, f"負責人確認為 {assignee}（{label}）")

        updated = self.store.update(key, mutate)
        self.store.upsert_history([_history_row(updated["structured_ticket"], updated)])
        return updated

    def automatic_assignee_choice(self, key: str) -> dict[str, str]:
        """Choose an auditable owner for automatic mode without leaving a human-confirmation gate.

        Prefer the routing model.  If its deployment gate returns manual triage, use the owner of
        the closest same-project historical ticket from duplicate retrieval, then progressively
        fall back to same component/product and finally the project's most frequent owner.
        """

        record = self.get(key)
        prediction = (record.get("stages", {}).get("assignee", {}).get("output") or {})
        suggested = str(prediction.get("suggested_assignee") or "").strip()
        if suggested:
            return {"assignee": suggested, "source": "routing_model", "reason": "全自動模式採用分派模型建議"}

        invalid = {"", "manual_triage", "unknown", "none", "unassigned"}
        duplicate_candidates = (
            record.get("stages", {}).get("duplicate", {}).get("output", {}).get("top_k_candidates") or []
        )
        for candidate in duplicate_candidates:
            owner = str(candidate.get("assignee") or "").strip()
            if owner.lower() not in invalid:
                ticket_id = str(candidate.get("ticket_id") or "").strip()
                return {
                    "assignee": owner,
                    "source": "same_domain_history",
                    "reason": f"全自動模式採用最相近的同專案歷史 Ticket {ticket_id or '（無編號）'} 之負責人",
                }

        for owner_value in prediction.get("ranked_candidates") or []:
            owner = str(owner_value or "").strip()
            if owner.lower() not in invalid:
                return {"assignee": owner, "source": "routing_candidate", "reason": "全自動模式採用分派模型最高順位候選"}

        structured = record.get("structured_ticket") or {}
        project = str(record.get("project") or "").strip()
        query_time = parse_time(structured.get("created_at") or structured.get("creation_time"))

        def known(value: Any) -> str:
            text = str(value or "").strip().lower()
            return "" if text in invalid or text == "n/a" else text

        product = known(structured.get("product"))
        component = known(structured.get("component"))
        rows = []
        for index, row in enumerate(self.store.history_rows()):
            owner = str(row.get("assignee") or "").strip()
            row_project = str(row.get("project") or "").strip()
            if owner.lower() in invalid or row_project != project:
                continue
            created = row_time(row) if query_time else None
            if created is not None and created >= query_time:
                continue
            row_product, row_component = known(row.get("product")), known(row.get("component"))
            tier = 0
            if component and row_component == component:
                tier = 3
            elif product and row_product == product:
                tier = 2
            rows.append((tier, index, owner))
        if rows:
            best_tier = max(item[0] for item in rows)
            eligible = [item for item in rows if item[0] == best_tier]
            counts: dict[str, tuple[int, int]] = {}
            for _, index, owner in eligible:
                count, last = counts.get(owner, (0, -1))
                counts[owner] = (count + 1, max(last, index))
            owner = max(counts, key=lambda item: counts[item])
            label = "同 component" if best_tier == 3 else "同 product" if best_tier == 2 else "同專案"
            return {
                "assignee": owner,
                "source": "project_history",
                "reason": f"全自動模式依{label}歷史處理次數分派",
            }
        return {}

    # --------------------------------------------------------------- batch/CLI
    def predict_priority_batch(self, keys: list[str]) -> dict[str, dict[str, Any]]:
        """One PRI call for every ticket that is ready to continue (build_features re-reads the whole
        history once per call, so a batch costs about the same as a single ticket)."""

        records = [self.get(key) for key in keys]
        ready = [record for record in records if record["status"] == "ready_to_continue"]
        if not ready:
            return {}
        tickets = [record["structured_ticket"] for record in ready]
        candidates = [(record["stages"].get("duplicate", {}).get("output") or {}).get("top_k_candidates", []) for record in ready]
        with self._after_lock:
            results = self.priority_predictor.predict_many(tickets, candidates)
        if len(ready) > 1:
            for result in results:
                result.setdefault("details", {})["batch_size"] = len(ready)
        return {record["key"]: result for record, result in zip(ready, results)}

    def process_batch(
        self,
        keys: list[str],
        *,
        assume_not_duplicate: bool = False,
        progress: Callable[[str], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Run tickets as far as the human gates allow; priority is predicted in one PRI batch."""

        say = progress or (lambda message: None)
        continuing = []
        for key in keys:
            record = self.run_until_review(key)
            say(f"{key}: 轉換＋重複偵測 → {record['status']}")
            if record["status"] == "awaiting_duplicate_review" and assume_not_duplicate:
                record = self.decide_duplicate(key, "not_duplicate", by="cli:--assume-not-duplicate")
            if record["status"] == "ready_to_continue":
                continuing.append(record)

        priority_results: dict[str, dict[str, Any]] = {}
        if continuing:
            say(f"優先級：以 PRI 模型批次預測 {len(continuing)} 筆")
            tickets = [record["structured_ticket"] for record in continuing]
            candidates = [
                (record["stages"].get("duplicate", {}).get("output") or {}).get("top_k_candidates", []) for record in continuing
            ]
            with self._after_lock:
                results = self.priority_predictor.predict_many(tickets, candidates)
            priority_results = {record["key"]: result for record, result in zip(continuing, results)}

        final = []
        for key in keys:
            if key in priority_results:
                record = self.run_after_duplicate(key, priority_result=priority_results[key])
                say(f"{key}: 優先級＋負責人＋錯誤定位 → {record['status']}")
            final.append(self.get(key))
        return final


def _history_row(structured: dict[str, Any], record: dict[str, Any] | None = None) -> dict[str, Any]:
    row = {
        key: structured.get(key)
        for key in (
            "ticket_id",
            "project",
            "title",
            "description",
            "product",
            "component",
            "severity",
            "bug_type",
            "error_message",
            "steps_to_reproduce",
            "expected_behavior",
            "actual_behavior",
            "logs",
            "created_at",
        )
        if structured.get(key) not in (None, "", [])
    }
    row["source"] = "integrated_workspace"
    if record:
        priority = (record["stages"].get("priority", {}).get("output") or {}).get("predicted_priority")
        if priority:
            row["priority"] = priority
        confirmed = (record.get("decisions") or {}).get("assignee", {}).get("assignee")
        if confirmed:
            row["assignee"] = confirmed
        row["status"] = "open"
    row.setdefault("created_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return row
