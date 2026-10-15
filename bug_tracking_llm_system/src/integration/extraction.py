"""Stage 1: raw ticket -> structured ticket JSON using ToJson's Code Llama extraction code."""

from __future__ import annotations

import time
from typing import Any

from integration.common import StageUnavailable, import_module_from_path, relative_to_workspace, stage_result
from integration.config import IntegrationConfig
from utils.json_schema import normalize_structured_ticket


TEXT_SECTIONS = (
    ("description", "Description"),
    ("body", "Description"),
    ("steps_to_reproduce", "Steps to reproduce"),
    ("expected_behavior", "Expected"),
    ("actual_behavior", "Actual"),
    ("environment", "Environment"),
    ("error_message", "Error"),
    ("logs", "Log"),
    ("screenshots_text", "Screenshot text"),
)
DIRECT_FIELDS = ("product", "severity", "bug_type", "component", "os", "version", "priority", "error_message")
PASSTHROUGH_KEYS = ("created_at", "creation_time", "reporter", "creator", "project", "repo", "base_commit")
UNKNOWN_VALUES = {"", "unknown", "none", "null", "n/a"}


def raw_ticket_id(raw: dict[str, Any]) -> str:
    for key in ("ticket_id", "id", "bug_id", "issue_id"):
        value = str(raw.get(key) or "").strip()
        if value:
            return value
    return ""


def raw_title(raw: dict[str, Any]) -> str:
    meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    for value in (raw.get("title"), raw.get("summary"), meta.get("title")):
        if str(value or "").strip():
            return str(value).strip()
    text = str(raw.get("text") or raw.get("bug_report") or "").strip()
    return text.splitlines()[0][:200] if text else ""


def _section_text(value: Any) -> str:
    if isinstance(value, list):
        return "\n".join(f"{index}. {item}" for index, item in enumerate(value, start=1) if str(item).strip())
    if isinstance(value, dict):
        return ", ".join(f"{key}: {item}" for key, item in value.items() if str(item).strip())
    return str(value or "").strip()


def raw_bug_report(raw: dict[str, Any]) -> str:
    """Compose the ``bug_report`` text that ToJson's extractor expects (title + body)."""

    if str(raw.get("bug_report") or "").strip():
        return str(raw["bug_report"]).strip()
    if str(raw.get("text") or "").strip():
        return str(raw["text"]).strip()
    parts = [raw_title(raw)]
    for key, label in TEXT_SECTIONS:
        text = _section_text(raw.get(key))
        if text:
            parts.append(f"{label}:\n{text}" if key not in {"description", "body"} else text)
    return "\n\n".join(part for part in parts if part)


def to_tojson_record(raw: dict[str, Any]) -> dict[str, Any]:
    """Build the record shape produced by ToJson/scripts/To_Json/transform_issues.py."""

    meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    direct = dict(meta.get("direct_fields") or {})
    for key in DIRECT_FIELDS:
        value = raw.get(key)
        if isinstance(value, str) and value.strip().lower() not in UNKNOWN_VALUES and not direct.get(key):
            direct[key] = value.strip()
    return {
        "id": raw_ticket_id(raw),
        "source": str(raw.get("source") or "workspace"),
        "bug_report": raw_bug_report(raw),
        "meta": {**meta, "title": raw_title(raw), "direct_fields": direct},
    }


def _explicit_fields(raw: dict[str, Any]) -> dict[str, Any]:
    """Fields the reporter filled in explicitly; they take precedence over model guesses."""

    explicit: dict[str, Any] = {}
    for key, value in raw.items():
        if key in {"predicted_json", "meta", "bug_report", "text"}:
            continue
        if isinstance(value, str) and value.strip().lower() in UNKNOWN_VALUES:
            continue
        if value in (None, [], {}):
            continue
        explicit[key] = value
    if isinstance(raw.get("environment"), str):
        explicit.pop("environment", None)
    return explicit


def finalize_structured_ticket(merged: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    structured = normalize_structured_ticket(merged)
    if not raw_ticket_id(raw) and structured.get("ticket_id") == "RAW-UNKNOWN":
        structured["ticket_id"] = ""
    for key in PASSTHROUGH_KEYS:
        if raw.get(key) not in (None, ""):
            structured[key] = raw[key]
    if "created_at" not in structured and raw.get("creation_time"):
        structured["created_at"] = raw["creation_time"]
    if "reporter" not in structured and raw.get("creator"):
        structured["reporter"] = raw["creator"]
    if isinstance(raw.get("environment"), str) and raw["environment"].strip():
        structured["environment"] = raw["environment"].strip()
    return structured


class ToJsonExtractor:
    """Calls ToJson's build_prompt -> call_ollama -> extract_json -> normalize_prediction."""

    MODULE_NAME = "tojson_extract_to_json"

    def __init__(self, config: IntegrationConfig, *, llm_call: Any | None = None) -> None:
        self.config = config
        self.script_path = config.subproject("tojson") / "scripts" / "To_Json" / "extract_to_json.py"
        self._llm_call = llm_call
        self._module = None

    @property
    def implementation(self) -> str:
        return relative_to_workspace(self.script_path, self.config.workspace_root)

    def module(self):
        if self._module is None:
            module = import_module_from_path(self.MODULE_NAME, self.script_path)
            settings = self.config.extraction
            # extract_to_json reads these module-level settings inside call_ollama/build_prompt.
            module.MODEL = settings.model
            module.OLLAMA_URL = settings.ollama_url
            module.OLLAMA_TIMEOUT = int(settings.timeout_seconds)
            module.PROMPT_MODE = settings.prompt_mode
            self._module = module
        return self._module

    def extract(self, raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        started = time.perf_counter()
        if not isinstance(raw, dict):
            raise ValueError("raw ticket must be a JSON object")
        settings = self.config.extraction
        module_label = "ToJson：Code Llama-Instruct 欄位抽取"

        if isinstance(raw.get("predicted_json"), dict):
            structured = finalize_structured_ticket({**raw["predicted_json"], **_explicit_fields(raw)}, raw)
            return structured, stage_result(
                stage="extraction",
                status="done",
                module=module_label,
                implementation=self.implementation,
                output={"structured_ticket": structured, "source": "imported predicted_json"},
                details={"note": "匯入資料已含 ToJson 產生的 predicted_json，直接沿用，未重新呼叫模型。"},
                started=started,
            )

        if not settings.enabled:
            return self._fallback(raw, started, "設定中停用了 LLM 抽取（extraction.enabled=false）")

        try:
            module = self.module()
            record = to_tojson_record(raw)
            title = module.clip_text(record["meta"].get("title", ""), module.MAX_TITLE_CHARS)
            bug_report = module.clip_text(record["bug_report"], module.MAX_REPORT_CHARS)
            prompt = module.build_prompt(title=title, bug_report=bug_report, rec=record)
            call = self._llm_call or module.call_ollama
            raw_output = call(prompt)
            predicted = module.normalize_prediction(module.extract_json(raw_output), rec=record)
        except StageUnavailable as exc:
            return self._fallback(raw, started, str(exc))
        except Exception as exc:  # network errors, timeouts, HTTP errors from Ollama
            if not settings.fallback_to_rule_normalization:
                raise
            return self._fallback(raw, started, f"無法呼叫 Ollama（{settings.model} @ {settings.ollama_url}）：{exc}")

        structured = finalize_structured_ticket({"predicted_json": predicted, **_explicit_fields(raw)}, raw)
        return structured, stage_result(
            stage="extraction",
            status="done",
            module=module_label,
            implementation=self.implementation,
            output={"structured_ticket": structured, "predicted_json": predicted},
            details={
                "model": settings.model,
                "prompt_mode": settings.prompt_mode,
                "decoding": {"temperature": 0.0, "top_p": 1.0},
                "llm_raw_output": str(raw_output)[:4000],
                "precedence": "回報者明確填寫的欄位優先，其餘欄位採用模型抽取結果。",
            },
            started=started,
        )

    def _fallback(self, raw: dict[str, Any], started: float, reason: str) -> tuple[dict[str, Any], dict[str, Any]]:
        structured = finalize_structured_ticket(_explicit_fields(raw) | {"description": raw_bug_report(raw)}, raw)
        if raw.get("description"):
            structured["description"] = str(raw["description"])
        return structured, stage_result(
            stage="extraction",
            status="fallback",
            module="主系統 json_schema 規則正規化（備援）",
            implementation="bug_tracking_llm_system/src/utils/json_schema.py",
            output={"structured_ticket": structured},
            warnings=["未使用 Code Llama 抽取，欄位由規則正規化產生，準確度較低。"],
            fallback_reason=reason,
            started=started,
        )
