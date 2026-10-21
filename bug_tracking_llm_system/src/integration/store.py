"""File-based ticket store for the integrated workflow (one JSON document per ticket)."""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4


SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_ticket_key(ticket_id: str) -> str:
    key = SAFE_ID.sub("_", str(ticket_id or "")).strip("._")[:80]
    return key or f"ticket_{uuid4().hex[:8]}"


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class TicketStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.tickets_dir = self.root / "tickets"
        self.artifacts_dir = self.root / "artifacts"
        self.history_path = self.root / "history" / "workspace_history.jsonl"
        self.feedback_path = self.root / "feedback" / "assignee_feedback.jsonl"
        self._lock = threading.RLock()
        self.tickets_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ tickets
    def _path(self, key: str) -> Path:
        return self.tickets_dir / f"{safe_ticket_key(key)}.json"

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def new_key(self, preferred: str) -> str:
        base = safe_ticket_key(preferred) if preferred else f"WS-{datetime.now().strftime('%Y%m%d')}-{uuid4().hex[:5].upper()}"
        key = base
        counter = 2
        with self._lock:
            while self._path(key).exists():
                key = f"{base}-{counter}"
                counter += 1
        return key

    def save(self, record: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            record["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            _atomic_write(self._path(record["key"]), record)
        return record

    def get(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        if not path.exists():
            return None
        with self._lock:
            return json.loads(path.read_text(encoding="utf-8"))

    def update(self, key: str, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        with self._lock:
            record = self.get(key)
            if record is None:
                raise KeyError(key)
            mutate(record)
            return self.save(record)

    def list(self) -> list[dict[str, Any]]:
        records = []
        with self._lock:
            for path in self.tickets_dir.glob("*.json"):
                try:
                    records.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    continue
        return sorted(records, key=lambda item: item.get("created_at", ""), reverse=True)

    def delete(self, key: str) -> None:
        """Remove the ticket record and its result files (artifacts)."""

        with self._lock:
            path = self._path(key)
            if path.exists():
                path.unlink()
            artifacts = self.artifacts_dir / safe_ticket_key(key)
            if artifacts.is_dir():
                shutil.rmtree(artifacts, ignore_errors=True)

    # --------------------------------------------------------------- artifacts
    def write_artifact(self, key: str, name: str, payload: Any) -> str:
        path = self.artifacts_dir / safe_ticket_key(key) / name
        _atomic_write(path, payload)
        return str(path)

    def artifact_path(self, key: str, name: str) -> Path:
        return self.artifacts_dir / safe_ticket_key(key) / name

    # ----------------------------------------------------------------- history
    def history_rows(self) -> list[dict[str, Any]]:
        if not self.history_path.exists():
            return []
        rows = []
        for line in self.history_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows

    def remove_history(self, predicate: Callable[[dict[str, Any]], bool]) -> int:
        """Drop workspace-history rows matching ``predicate``; returns how many were removed."""

        with self._lock:
            rows = self.history_rows()
            kept = [row for row in rows if not predicate(row)]
            removed = len(rows) - len(kept)
            if removed:
                tmp = self.history_path.with_suffix(".tmp")
                tmp.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in kept), encoding="utf-8")
                os.replace(tmp, self.history_path)
            return removed

    def upsert_history(self, rows: list[dict[str, Any]]) -> int:
        with self._lock:
            existing = {str(row.get("ticket_id")): row for row in self.history_rows() if row.get("ticket_id")}
            for row in rows:
                ticket_id = str(row.get("ticket_id") or row.get("id") or "").strip()
                if ticket_id:
                    existing[ticket_id] = {**existing.get(ticket_id, {}), **row, "ticket_id": ticket_id}
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.history_path.with_suffix(".tmp")
            tmp.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in existing.values()), encoding="utf-8")
            os.replace(tmp, self.history_path)
            return len(existing)
