"""Readiness report: which real modules and trained artifacts are available on this machine."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any
from urllib import request

from integration.config import IntegrationConfig
from integration.duplicate import read_history_rows, sbert_dir_problem


def _has(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _check(label: str, ok: bool, detail: str = "", *, required: bool = True) -> dict[str, Any]:
    return {"label": label, "ok": bool(ok), "detail": detail, "required": required}


def _ollama_status(url: str, model: str) -> dict[str, Any]:
    tags_url = url.split("/api/")[0].rstrip("/") + "/api/tags"
    try:
        with request.urlopen(tags_url, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return _check("Ollama 服務可連線", False, f"{tags_url}：{exc}")
    names = [str(item.get("name") or item.get("model") or "") for item in payload.get("models", [])]
    has_model = any(name == model or name.startswith(f"{model}:") for name in names)
    return _check(f"Ollama 已安裝 {model}", has_model, "已安裝：" + (", ".join(names) or "（無）"))


def _count_rows(path: Path) -> int:
    try:
        return len(read_history_rows(path))
    except Exception:
        return 0


def readiness_report(config: IntegrationConfig, *, store_history_path: Path | None = None, check_services: bool = True) -> dict[str, Any]:
    stages: dict[str, dict[str, Any]] = {}

    script = config.subproject("tojson") / "scripts" / "To_Json" / "extract_to_json.py"
    checks = [
        _check("ToJson 抽取程式", script.is_file(), str(script)),
        _check("Python 套件 requests", _has("requests")),
    ]
    if check_services and config.extraction.enabled:
        checks.append(_ollama_status(config.extraction.ollama_url, config.extraction.model))
    stages["extraction"] = {"title": "轉換 JSON（ToJson / Code Llama-Instruct）", "checks": checks}

    dup_root = config.subproject("duplicate") / "src" / "duplicate_ticket_detection"
    sbert_dir = config.duplicate.sbert_model_dir
    history_counts = [(path, _count_rows(path)) for path in config.historical_tickets_paths]
    workspace_rows = _count_rows(store_history_path) if store_history_path else 0
    stages["duplicate"] = {
        "title": "重複偵測（bug-duplicate-detection）",
        "checks": [
            _check("DUP 套件", dup_root.is_dir(), str(dup_root)),
            _check("Python 套件 numpy / scikit-learn", _has("numpy") and _has("sklearn")),
            _check(
                "SBERT 微調模型（title_model / content_model，含權重檔）",
                not sbert_dir_problem(sbert_dir),
                sbert_dir_problem(sbert_dir) or str(sbert_dir),
            ),
            _check("Python 套件 sentence-transformers", _has("sentence_transformers"), "SBERT 模型需要", required=bool(sbert_dir)),
            _check(
                "歷史 Ticket 資料庫",
                any(count for _, count in history_counts) or workspace_rows > 0,
                "；".join([*(f"{path}（{count} 筆）" for path, count in history_counts), f"工作台累積 {workspace_rows} 筆"]),
            ),
        ],
    }

    paths = config.priority_paths()
    stages["priority"] = {
        "title": "優先級判定（bug-priority-drone）",
        "checks": [
            _check("PRI 程式（build_features.py）", (Path(paths["root"]) / "scripts" / "build_features.py").is_file(), str(paths["root"])),
            _check("Python 套件 pandas / scipy / joblib / scikit-learn", all(_has(name) for name in ("pandas", "scipy", "joblib", "sklearn"))),
            _check("模型 recall_balanced_priority_model.joblib", bool(paths["model_path"] and Path(paths["model_path"]).is_file()), str(paths["model_path"])),
            _check("訓練特徵目錄（fitted encoders）", bool(paths["reference_feature_dir"] and Path(paths["reference_feature_dir"]).is_dir()), str(paths["reference_feature_dir"])),
            _check("清洗後歷史 CSV", bool(paths["history_csv"] and Path(paths["history_csv"]).is_file()), str(paths["history_csv"] or "未設定 priority.history_csv")),
            _check("REP- 權重 JSON", bool(paths["rep_weights_json"] and Path(paths["rep_weights_json"]).is_file()), str(paths["rep_weights_json"]), required=False),
        ],
    }

    from config import PipelineConfig

    assignee_history = config.assignee.get("assignee_dataset_path") or PipelineConfig().assignee_dataset_path
    configured_ok = Path(assignee_history).is_file()
    workspace_assigned = 0
    if store_history_path and Path(store_history_path).is_file():
        workspace_assigned = sum(1 for row in read_history_rows(Path(store_history_path)) if str(row.get("assignee") or "").strip())
    stages["assignee"] = {
        "title": "分派負責人（AssigneeTriager）",
        "checks": [
            _check(
                "負責人歷史資料",
                configured_ok or workspace_assigned > 0,
                f"{assignee_history}（{_count_rows(Path(assignee_history))} 筆）"
                + ("" if configured_ok else f"；未找到時改用工作台內含負責人的紀錄（{workspace_assigned} 筆）"),
            ),
        ],
    }

    project_checks = []
    for name, project in config.projects.items():
        project_checks.append(_check(f"專案 {name} 的 Repository", Path(project.repo_path).is_dir(), str(project.repo_path)))
        if project.code_index_path:
            project_checks.append(
                _check(f"專案 {name} 的程式碼索引", Path(project.code_index_path).is_file(), str(project.code_index_path), required=False)
            )
    if not project_checks:
        project_checks.append(_check("至少設定一個專案（projects.repo_path）", False, "沒有專案就無法做錯誤定位"))
    stages["localization"] = {"title": "錯誤定位（Stage-3 BugLocalizer）", "checks": project_checks}

    for item in stages.values():
        required = [check for check in item["checks"] if check["required"]]
        item["ready"] = all(check["ok"] for check in required)
    return {"stages": stages, "config": config.to_public_dict()}
