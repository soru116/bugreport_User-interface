"""Shared helpers: stage-result envelope and importing sub-project code by path."""

from __future__ import annotations

import importlib
import importlib.util
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any


STAGE_STATUSES = {"pending", "running", "done", "fallback", "skipped", "failed"}
_IMPORT_LOCK = threading.Lock()


class StageUnavailable(RuntimeError):
    """Raised when a trained artifact, dependency or service needed by a stage is missing."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stage_result(
    *,
    stage: str,
    status: str,
    module: str,
    implementation: str,
    output: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    error: str = "",
    fallback_reason: str = "",
    started: float | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Uniform envelope so the UI can always show which real module produced a result."""

    if status not in STAGE_STATUSES:
        raise ValueError(f"Unknown stage status: {status}")
    payload: dict[str, Any] = {
        "stage": stage,
        "status": status,
        "module": module,
        "implementation": implementation,
        "finished_at": utc_now(),
        "duration_ms": int((time.perf_counter() - started) * 1000) if started is not None else None,
        "output": output or {},
        "warnings": list(warnings or []),
        "error": error,
        "fallback_reason": fallback_reason,
    }
    if details:
        payload["details"] = details
    return payload


def import_module_from_path(module_name: str, file_path: Path) -> ModuleType:
    """Import a single script file (e.g. ToJson's extract_to_json.py) as a module."""

    file_path = Path(file_path)
    if not file_path.is_file():
        raise StageUnavailable(f"找不到子專案程式：{file_path}")
    with _IMPORT_LOCK:
        cached = sys.modules.get(module_name)
        if cached is not None and Path(getattr(cached, "__file__", "")).resolve() == file_path.resolve():
            return cached
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        if spec is None or spec.loader is None:
            raise StageUnavailable(f"無法載入子專案程式：{file_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except ImportError as exc:
            sys.modules.pop(module_name, None)
            raise StageUnavailable(f"{file_path.name} 缺少相依套件：{exc}") from exc
        return module


def import_package_from_root(package: str, source_root: Path) -> ModuleType:
    """Import a package (e.g. duplicate_ticket_detection) from a sub-project's src/ folder."""

    source_root = Path(source_root)
    top_level = package.split(".", 1)[0]
    if not (source_root / top_level).is_dir():
        raise StageUnavailable(f"找不到子專案套件：{source_root / package}")
    with _IMPORT_LOCK:
        root_text = str(source_root.resolve())
        if root_text not in sys.path:
            sys.path.insert(0, root_text)
        try:
            return importlib.import_module(package)
        except ImportError as exc:
            raise StageUnavailable(f"{package} 缺少相依套件：{exc}") from exc


def relative_to_workspace(path: Path, workspace_root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(workspace_root).resolve()))
    except ValueError:
        return str(path)
