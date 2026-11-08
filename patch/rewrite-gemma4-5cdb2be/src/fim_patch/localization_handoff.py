"""Read and validate frozen external fault-localization handoffs.

This module intentionally does not import either ``modules`` or ``utils`` so
the patch runtime cannot silently fall back to the repository's FL copy.
"""
from __future__ import annotations

import json
import re
from pathlib import Path


_IDENTIFIER_KEYS = ("ticket_id", "instance_id", "bug_id", "id")


def ticket_identifier(ticket: dict) -> str:
    for key in _IDENTIFIER_KEYS:
        value = str(ticket.get(key) or "").strip()
        if value:
            return value
    return ""


def _unwrap_record(record: object) -> dict:
    if not isinstance(record, dict):
        raise ValueError("Each localization record must be a JSON object")
    nested = record.get("localization")
    if isinstance(nested, dict):
        for key in _IDENTIFIER_KEYS:
            if key not in nested and record.get(key) is not None:
                nested[key] = record[key]
        return nested
    return record


def read_localization_records(path: str | Path) -> list[dict]:
    """Read one JSON object, a JSON array, or line-delimited JSON objects."""
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.casefold() in {".jsonl", ".ndjson"}:
        records = []
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                records.append(_unwrap_record(json.loads(line)))
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"Invalid localization JSONL at line {line_number}: {exc}") from exc
        if not records:
            raise ValueError("Localization JSONL contains no records")
        return records

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid localization JSON: {exc}") from exc
    if isinstance(payload, list):
        records = [_unwrap_record(record) for record in payload]
    else:
        records = [_unwrap_record(payload)]
    if not records:
        raise ValueError("Localization JSON contains no records")
    return records


def load_localization(path: str | Path, ticket: dict) -> dict:
    """Select exactly the localization record belonging to ``ticket``."""
    records = read_localization_records(path)
    wanted = ticket_identifier(ticket)
    if len(records) == 1:
        record = records[0]
        found = ticket_identifier(record)
        if wanted and found and found != wanted:
            raise ValueError(f"Localization ticket id {found!r} does not match {wanted!r}")
        return record
    if not wanted:
        raise ValueError("A ticket identifier is required to select from multi-record localization")
    matches = [record for record in records if ticket_identifier(record) == wanted]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one localization for ticket {wanted!r}; found {len(matches)}")
    return matches[0]


def load_localization_map(path: str | Path) -> dict[str, dict]:
    """Load a batch handoff and reject missing or duplicate ticket identifiers."""
    records = read_localization_records(path)
    mapping = {}
    for record in records:
        identifier = ticket_identifier(record)
        if not identifier:
            raise ValueError("Every JSONL localization record must contain a ticket identifier")
        if identifier in mapping:
            raise ValueError(f"Duplicate localization record for ticket {identifier!r}")
        mapping[identifier] = record
    return mapping
