#!/usr/bin/env python3
"""Ticket 工作台：web UI for the integrated workflow.

Run from bug_tracking_llm_system/:

    python3 workspace/server.py --config integration_config.json --port 8780

Then open http://127.0.0.1:8780 .  Long-running stages execute in background threads; the page
polls the ticket until the stage finishes.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import threading
import traceback
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

WORKSPACE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = WORKSPACE_ROOT.parent
STATIC_ROOT = WORKSPACE_ROOT / "static"
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from integration.config import IntegrationConfig, load_integration_config  # noqa: E402
from integration.importers import parse_import_payload  # noqa: E402
from integration.pipeline import STAGE_LABELS, STAGES, IntegratedPipeline  # noqa: E402
from integration.status import readiness_report  # noqa: E402


MAX_BODY_BYTES = 20_000_000
ARTIFACT_NAMES = {"duplicate_result.json", "priority_result.json", "localization_result.json", "bug_location_raw.json"}


class WorkspaceApp:
    def __init__(self, config: IntegrationConfig) -> None:
        self.config = config
        self.pipeline = IntegratedPipeline(config)
        self._jobs: dict[str, threading.Thread] = {}
        self._jobs_lock = threading.Lock()
        self._index_jobs: dict[str, dict[str, Any]] = {}
        self._config_mtime = self._read_config_mtime()

    # ------------------------------------------------------- config reload
    def _read_config_mtime(self) -> float | None:
        path = self.config.config_path
        try:
            return path.stat().st_mtime if path else None
        except OSError:
            return None

    def maybe_reload_config(self) -> None:
        """Pick up edits to integration_config.json without restarting (only while nothing runs)."""

        mtime = self._read_config_mtime()
        if mtime is None or mtime == self._config_mtime:
            return
        with self._jobs_lock:
            if any(job.is_alive() for job in self._jobs.values()):
                return
            try:
                config = load_integration_config(self.config.config_path)
            except Exception as exc:  # keep the running config when the edited file is invalid
                print(f"設定檔有錯誤，暫不套用：{exc}")
                self._config_mtime = mtime
                return
            self.config = config
            self.pipeline = IntegratedPipeline(config)
            self._config_mtime = mtime
            print("偵測到設定檔變更，已重新載入。")

    # ----------------------------------------------------------- background
    def _submit(self, key: str, target, *args, **kwargs) -> bool:
        with self._jobs_lock:
            job = self._jobs.get(key)
            if job is not None and job.is_alive():
                return False

            def run() -> None:
                try:
                    target(*args, **kwargs)
                except Exception:  # errors are recorded on the ticket by the pipeline
                    traceback.print_exc()

            thread = threading.Thread(target=run, name=f"ticket-{key}", daemon=True)
            self._jobs[key] = thread
            thread.start()
            return True

    def busy(self, key: str) -> bool:
        with self._jobs_lock:
            job = self._jobs.get(key)
            return bool(job and job.is_alive())

    def _run_full(self, key: str) -> None:
        record = self.pipeline.run_until_review(key)
        if record["status"] == "ready_to_continue":
            self.pipeline.run_after_duplicate(key)

    def start(self, key: str) -> bool:
        self.maybe_reload_config()
        return self._submit(key, self._run_full, key)

    def continue_after_duplicate(self, key: str) -> bool:
        self.maybe_reload_config()
        return self._submit(key, self.pipeline.run_after_duplicate, key)

    def relocalize(self, key: str, project: str | None) -> bool:
        self.maybe_reload_config()
        return self._submit(key, self.pipeline.rerun_localization, key, project=project)

    def build_index(self, project_name: str) -> dict[str, Any]:
        project = self.config.project(project_name)
        if project is None:
            raise ValueError(f"沒有這個專案：{project_name}")
        state = self._index_jobs.get(project_name)
        if state and state.get("status") == "running":
            return state
        state = {"status": "running", "project": project_name}
        self._index_jobs[project_name] = state

        def run() -> None:
            try:
                state.update(self.pipeline.localizer.build_index(project), status="done")
            except Exception as exc:
                state.update(status="failed", error=str(exc))

        threading.Thread(target=run, daemon=True).start()
        return state

    # ---------------------------------------------------------------- views
    def ticket_summary(self, record: dict[str, Any]) -> dict[str, Any]:
        stages = record.get("stages", {})
        priority = (stages.get("priority", {}).get("output") or {}).get("predicted_priority")
        return {
            "key": record["key"],
            "ticket_id": record.get("ticket_id"),
            "title": record.get("title"),
            "project": record.get("project"),
            "status": record.get("status"),
            "current_stage": record.get("current_stage"),
            "attention": record.get("attention", []),
            "priority": priority,
            "stage_status": {name: stages.get(name, {}).get("status", "pending") for name in STAGES},
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
            "busy": self.busy(record["key"]),
        }

    def ticket_detail(self, key: str) -> dict[str, Any]:
        record = self.pipeline.get(key)
        record["busy"] = self.busy(key)
        return record

    def projects(self) -> list[dict[str, Any]]:
        items = []
        for name, project in self.config.projects.items():
            status = self.pipeline.localizer.index_status(project)
            status["description"] = project.description
            status["index_job"] = self._index_jobs.get(name)
            items.append(status)
        return items


class Handler(SimpleHTTPRequestHandler):
    app: WorkspaceApp

    # ---------------------------------------------------------------- utils
    def _json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"error": message}, status)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise ValueError("檔案太大（上限 20 MB）。")
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        value = json.loads(raw or "{}")
        if not isinstance(value, dict):
            raise ValueError("Request body must be a JSON object.")
        return value

    def _file(self, path: Path, *, download: bool = False) -> None:
        data = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:  # quiet console
        return

    def _parts(self) -> list[str]:
        path = urlparse(self.path).path
        return [unquote(part) for part in path.strip("/").split("/") if part]

    # ------------------------------------------------------------------ GET
    def do_GET(self) -> None:
        parts = self._parts()
        try:
            if not parts:
                return self._file(STATIC_ROOT / "index.html")
            if parts[0] != "api":
                target = (STATIC_ROOT / "/".join(parts)).resolve()
                if STATIC_ROOT.resolve() not in target.parents or not target.is_file():
                    return self._error("Not found", 404)
                return self._file(target)
            if parts[1:] == ["tickets"]:
                records = self.app.pipeline.store.list()
                return self._json({"tickets": [self.app.ticket_summary(record) for record in records]})
            if len(parts) == 3 and parts[1] == "tickets":
                return self._json(self.app.ticket_detail(parts[2]))
            if len(parts) == 5 and parts[1] == "tickets" and parts[3] == "artifact":
                if parts[4] == "ticket.json":
                    data = self.app.ticket_detail(parts[2])
                    body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Disposition", f'attachment; filename="{parts[2]}.json"')
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return None
                if parts[4] not in ARTIFACT_NAMES:
                    return self._error("Unknown artifact", 404)
                path = self.app.pipeline.store.artifact_path(parts[2], parts[4])
                if not path.is_file():
                    return self._error("Artifact not found", 404)
                return self._file(path, download=True)
            if parts[1:] == ["status"]:
                self.app.maybe_reload_config()
                return self._json(readiness_report(self.app.config, store_history_path=self.app.pipeline.store.history_path))
            if parts[1:] == ["projects"]:
                return self._json({"projects": self.app.projects(), "default_project": self.app.config.default_project})
            if len(parts) == 3 and parts[1] == "history":
                row = self.app.pipeline.find_history_ticket(parts[2])
                if row is None:
                    return self._error("找不到這張歷史 Ticket。", 404)
                return self._json({"ticket": row})
            if parts[1:] == ["meta"]:
                return self._json({"stages": [{"key": key, "label": STAGE_LABELS[key]} for key in STAGES]})
            return self._error("Not found", 404)
        except KeyError:
            return self._error("找不到這張 Ticket。", 404)
        except Exception as exc:
            traceback.print_exc()
            return self._error(f"{type(exc).__name__}: {exc}", 500)

    # ----------------------------------------------------------------- POST
    def do_POST(self) -> None:
        parts = self._parts()
        try:
            body = self._body()
            app = self.app
            if parts == ["api", "import"]:
                records = parse_import_payload(str(body.get("content") or ""), filename=str(body.get("filename") or ""))
                if body.get("ticket"):
                    records = [body["ticket"]]
                if not records:
                    return self._error("沒有讀到任何 Ticket，請確認檔案內容。")
                if body.get("as_history"):
                    total = app.pipeline.import_history(records)
                    return self._json({"history_imported": len(records), "history_total": total})
                created = app.pipeline.import_raw(records, source=str(body.get("source") or "工作台匯入"), project=str(body.get("project") or ""))
                if body.get("auto_start", True):
                    for record in created:
                        app.start(record["key"])
                return self._json({"created": [record["key"] for record in created]})
            if len(parts) == 4 and parts[:2] == ["api", "tickets"]:
                key, action = parts[2], parts[3]
                if app.busy(key):
                    return self._error("這張 Ticket 正在處理中，請稍候。", 409)
                if action == "run":
                    app.start(key)
                    return self._json({"started": True})
                if action == "duplicate":
                    record = app.pipeline.decide_duplicate(
                        key,
                        str(body.get("decision") or ""),
                        duplicate_of=str(body.get("duplicate_of") or ""),
                        note=str(body.get("note") or ""),
                    )
                    if record["status"] == "ready_to_continue":
                        app.continue_after_duplicate(key)
                    return self._json({"status": record["status"]})
                if action == "mark":
                    record = app.pipeline.mark_candidate(
                        key,
                        str(body.get("candidate_id") or ""),
                        str(body.get("mark") or ""),
                        note=str(body.get("note") or ""),
                    )
                    return self._json({"review_marks": record.get("review_marks", {})})
                if action == "assignee":
                    record = app.pipeline.confirm_assignee(
                        key,
                        str(body.get("assignee") or ""),
                        decision=str(body.get("decision") or "accept"),
                        reviewer=str(body.get("reviewer") or ""),
                        note=str(body.get("note") or ""),
                    )
                    return self._json({"decisions": record["decisions"]})
                if action == "localize":
                    project = body.get("project")
                    app.relocalize(key, str(project) if project else None)
                    return self._json({"started": True})
                return self._error("Unknown action", 404)
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "index":
                return self._json(app.build_index(parts[2]))
            return self._error("Not found", 404)
        except KeyError:
            return self._error("找不到這張 Ticket。", 404)
        except ValueError as exc:
            return self._error(str(exc))
        except Exception as exc:
            traceback.print_exc()
            return self._error(f"{type(exc).__name__}: {exc}", 500)

    def do_DELETE(self) -> None:
        parts = self._parts()
        if len(parts) == 3 and parts[:2] == ["api", "tickets"]:
            if self.app.busy(parts[2]):
                return self._error("這張 Ticket 正在處理中，請稍候。", 409)
            self.app.pipeline.store.delete(parts[2])
            return self._json({"deleted": parts[2]})
        return self._error("Not found", 404)


class WorkspaceServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second server bind a port that an old server still serves,
    # so requests silently keep going to the old process.  Fail loudly instead.
    allow_reuse_address = os.name != "nt"


def main() -> None:
    parser = argparse.ArgumentParser(description="Ticket 工作台")
    parser.add_argument("--config", default=None, help="integration_config.json")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8780)
    args = parser.parse_args()
    config = load_integration_config(args.config)
    Handler.app = WorkspaceApp(config)
    try:
        server = WorkspaceServer((args.host, args.port), Handler)
    except OSError as exc:
        print(f"無法使用連接埠 {args.port}：{exc}")
        print("通常是舊的工作台伺服器還在執行。請關掉舊的視窗，或執行：")
        print(f"  netstat -ano | findstr :{args.port}")
        print("  taskkill /F /PID <上一行最後一欄的數字>")
        raise SystemExit(1)
    print(f"設定檔：{config.config_path or '（未指定，使用預設值）'}")
    report = readiness_report(config, store_history_path=Handler.app.pipeline.store.history_path, check_services=False)
    for name in STAGES:
        stage = report["stages"][name]
        missing = [check["label"] for check in stage["checks"] if check["required"] and not check["ok"]]
        state = "使用訓練模型／資料" if stage["ready"] else "會使用備援：缺 " + "、".join(missing)
        print(f"  {STAGE_LABELS[name]}：{state}")
    print(f"Ticket 工作台：http://{args.host}:{args.port}  （資料存放於 {config.store_dir}）")
    print("按 Ctrl + C 停止伺服器。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
