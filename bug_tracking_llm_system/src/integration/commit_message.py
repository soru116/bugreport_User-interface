"""Commit-message drafts for a generated patch (Lyy12705/commit-message-generator, vendored).

The team's generator asks Code Llama-Instruct (zero-shot) for a Conventional Commit ``type`` and
``subject`` from the diff and the ticket text, then validates them.  This module only supplies the
inputs and keeps the result with the ticket:

* the patch: the one the workspace generated, a passing candidate, or the 6/36 run's patch;
* the ticket title/description;
* ``test_result``: the official SWE-bench verdict when the patch is byte-identical (sha256) to a judged
  patch, otherwise an explicit "no tests were run" so the draft cannot claim the bug is fixed.

A draft is text for an engineer to review.  Nothing here runs ``git commit``.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from integration.common import utc_now
from integration.config import IntegrationConfig

MODULE = "提交訊息草稿（Code Llama-Instruct 零樣本，Lyy12705/commit-message-generator ae8b88c）"
IMPLEMENTATION = "commit-message/codellama-instruct-ae8b88c/src/commit_message/generator.py"
DESCRIPTION_LIMIT = 3000
_IMPORT_LOCK = threading.Lock()


class CommitMessageDrafter:
    def __init__(self, config: IntegrationConfig, *, generate_fn: Callable[..., dict[str, Any]] | None = None) -> None:
        self.config = config
        self.generate_fn = generate_fn

    def readiness(self) -> dict[str, Any]:
        path = self.config.commit_message_code_dir() / "src" / "commit_message" / "generator.py"
        return {"code": path.is_file(), "code_path": str(path.parent)}

    def _generate(self) -> Callable[..., dict[str, Any]]:
        if self.generate_fn is not None:
            return self.generate_fn
        src = self.config.commit_message_code_dir() / "src"
        if not (src / "commit_message" / "generator.py").is_file():
            raise RuntimeError(f"找不到提交訊息生成程式：{src / 'commit_message'}。請確認已 git pull。")
        with _IMPORT_LOCK:
            if str(src) not in sys.path:
                sys.path.append(str(src))
            from commit_message import generate_commit_message  # noqa: PLC0415
        return generate_commit_message

    @staticmethod
    def test_evidence(official: dict[str, Any] | None) -> dict[str, Any]:
        if official and isinstance(official.get("official_resolved"), bool):
            resolved = official["official_resolved"]
            return {
                "official_swebench_harness": "resolved" if resolved else "unresolved",
                "meaning": "FAIL_TO_PASS all passed and PASS_TO_PASS had no regressions" if resolved
                else "the official tests did not all pass",
                "run_id": official.get("run_id"),
            }
        return {"status": "not_run",
                "note": "The patch only passed syntax, AST and git-apply checks. No tests were executed."}

    @staticmethod
    def ticket_context(record: dict[str, Any]) -> dict[str, Any]:
        raw = record.get("raw") or {}
        structured = record.get("structured_ticket") or {}
        description = str(raw.get("description") or raw.get("problem_statement") or structured.get("description") or "")
        if len(description) > DESCRIPTION_LIMIT:
            description = description[:DESCRIPTION_LIMIT] + "\n…(truncated)"
        return {
            "title": str(raw.get("title") or structured.get("title") or record.get("title") or ""),
            "description": description,
            "ticket": {"ticket_id": record.get("ticket_id"), "project": record.get("project")},
        }

    def draft(self, record: dict[str, Any], patch: str, *, official: dict[str, Any] | None, source: str) -> dict[str, Any]:
        started = time.perf_counter()
        settings = self.config.commit_message
        result: dict[str, Any] = {"source": source, "module": MODULE, "implementation": IMPLEMENTATION,
                                  "model": settings.model, "finished_at": utc_now()}
        if not settings.enabled:
            return {**result, "status": "failed", "error": "設定檔已關閉提交訊息草稿（commit_message.enabled = false）。"}
        if not patch:
            return {**result, "status": "failed", "error": "沒有補丁，無法產生提交訊息。"}
        evidence = self.test_evidence(official)
        payload = {"patch": patch, **self.ticket_context(record), "test_result": evidence}
        try:
            generated = self._generate()(payload, model=settings.model, ollama_url=settings.ollama_url,
                                         timeout=float(settings.timeout_seconds))
        except Exception as exc:  # import problems, unexpected errors in the vendored code
            generated = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
        result["duration_ms"] = round((time.perf_counter() - started) * 1000)
        result["test_evidence"] = evidence
        if not generated.get("success"):
            error = str(generated.get("error") or "未知錯誤")
            if "Cannot reach Ollama" in error:
                error += f"。請確認 Ollama 正在執行，並已 ollama pull {settings.model}。"
            return {**result, "status": "failed", "error": error}
        message = str(generated.get("commit_message") or "")
        footer = [f"Refs: {record.get('ticket_id')}"]
        if evidence.get("official_swebench_harness") == "resolved":
            footer.append(f"Verified-by: official SWE-bench harness ({evidence.get('run_id')})")
        return {
            **result,
            "status": "done",
            "commit_message": message,
            "full_message": message + "\n\n" + "\n".join(footer) + "\n",
            "changed_files": generated.get("changed_files") or [],
            "model": (generated.get("diagnostics") or {}).get("model") or settings.model,
            "generation_method": generated.get("generation_method"),
            "details": (generated.get("diagnostics") or {}).get("details", ""),
        }
