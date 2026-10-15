"""Parse ticket import files (JSON / JSONL / CSV) into raw ticket dicts.

Accepted shapes per record:
* raw tickets: ``title`` / ``description`` / ``logs`` / ``steps_to_reproduce`` ...
* ToJson unlabeled records: ``{"id", "bug_report", "meta": {"title", "direct_fields"}}``
* ToJson predictions: ``{"id", "predicted_json": {...}}`` (extraction is not re-run)
* GitHub-like issues: ``title`` / ``body``
* plain text blocks: ``{"text": "..."}``
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any


LIST_COLUMNS = {"steps_to_reproduce"}
MAX_RECORDS = 5000


def parse_import_payload(content: str, *, filename: str = "") -> list[dict[str, Any]]:
    text = (content or "").lstrip("﻿").strip()
    if not text:
        return []
    lower = filename.lower()
    if lower.endswith(".csv"):
        records = _parse_csv(text)
    elif lower.endswith(".jsonl"):
        records = _parse_jsonl(text)
    elif lower.endswith(".json") or text[:1] in "[{":
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            records = _parse_jsonl(text)
        else:
            records = _records_from_json(value)
    elif lower.endswith(".txt") or lower.endswith(".md"):
        records = [{"text": block.strip()} for block in text.split("\n---\n") if block.strip()]
    else:
        records = _parse_csv(text) if "," in text.splitlines()[0] else [{"text": text}]
    cleaned = [record for record in records if isinstance(record, dict) and any(str(v).strip() for v in record.values())]
    if len(cleaned) > MAX_RECORDS:
        raise ValueError(f"一次最多匯入 {MAX_RECORDS} 筆 Ticket。")
    return cleaned


def _records_from_json(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("tickets", "records", "items", "issues"):
            if isinstance(value.get(key), list):
                return [item for item in value[key] if isinstance(item, dict)]
        return [value]
    raise ValueError("JSON 內容必須是物件或物件陣列。")


def _parse_jsonl(text: str) -> list[dict[str, Any]]:
    records = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSONL 第 {line_number} 行不是合法 JSON：{exc}") from exc
        if isinstance(value, dict):
            records.append(value)
    return records


def _parse_csv(text: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text))
    records = []
    for row in reader:
        record: dict[str, Any] = {}
        for key, value in row.items():
            if key is None:
                continue
            key = key.strip()
            value = (value or "").strip()
            if key in LIST_COLUMNS and value:
                parts = [part.strip() for part in value.replace("\r", "").split("\n") if part.strip()]
                if len(parts) <= 1 and "|" in value:
                    parts = [part.strip() for part in value.split("|") if part.strip()]
                record[key] = parts
            else:
                record[key] = value
        records.append(record)
    return records
