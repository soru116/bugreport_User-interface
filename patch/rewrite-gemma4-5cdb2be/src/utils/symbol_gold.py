from __future__ import annotations

import hashlib
import math
import re
import shlex
from dataclasses import dataclass
from typing import Any, Sequence

from utils.symbol_localization import (
    SYMBOL_RECORD_SCHEMA_VERSION,
    SymbolParseResult,
    SymbolRecordV1,
    normalize_repository_path,
)


PATCH_SYMBOL_PROVENANCE_SCHEMA_VERSION = "patch-symbol-provenance-v1"
SYMBOL_GOLD_RECORD_SCHEMA_VERSION = "symbol-gold-record-v1"

PATCH_CHANGE_TYPES = frozenset({"modified", "deleted", "added", "renamed"})
GOLD_MAPPING_STATUSES = frozenset({"mapped", "excluded"})
GOLD_MAPPING_METHODS = frozenset(
    {
        "modified_line_containment",
        "deleted_line_containment",
        "insertion_anchor_containment",
        "context_line_containment",
        "module_level",
        "unmapped",
    }
)
GOLD_EXCLUSION_REASONS = frozenset(
    {
        "new_file",
        "missing_base_file",
        "unsupported_language",
        "parser_failure",
        "no_containing_symbol",
        "ambiguous_mapping",
        "invalid_path",
        "symlink_escape",
    }
)

PATCH_PROVENANCE_FIELDS = frozenset(
    {
        "schema_version",
        "source",
        "patch_sha256",
        "change_type",
        "old_file_path",
        "new_file_path",
        "hunk_index",
        "old_start_line",
        "old_line_count",
        "new_start_line",
        "new_line_count",
        "matched_old_lines",
        "context_old_lines",
        "insertion_anchor_line",
    }
)
SYMBOL_GOLD_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "symbol_record_schema_version",
        "gold_id",
        "ticket_id",
        "repo",
        "base_commit",
        "file_path",
        "mapping_status",
        "mapping_method",
        "mapping_confidence",
        "exclusion_reason",
        "symbol_id",
        "qualified_name",
        "symbol_kind",
        "start_line",
        "start_column",
        "end_line",
        "end_column",
        "provenance",
    }
)


def make_patch_sha256(patch: str | bytes) -> str:
    payload = patch.encode("utf-8") if isinstance(patch, str) else bytes(patch)
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _normalize_optional_path(value: str) -> str:
    text = str(value or "").strip()
    return normalize_repository_path(text) if text else ""


def _require_fields(payload: dict[str, Any], required: frozenset[str], *, label: str) -> None:
    missing = sorted(required.difference(payload))
    if missing:
        raise ValueError(f"{label} is missing required fields: {', '.join(missing)}.")


def _validated_lines(values: Sequence[int], *, field_name: str) -> tuple[int, ...]:
    lines = tuple(int(value) for value in values)
    if any(line <= 0 for line in lines):
        raise ValueError(f"{field_name} must contain only positive line numbers.")
    if tuple(sorted(set(lines))) != lines:
        raise ValueError(f"{field_name} must be unique and sorted.")
    return lines


@dataclass(frozen=True, slots=True)
class PatchSymbolProvenanceV1:
    """Trace one gold mapping back to a developer-patch hunk."""

    patch_sha256: str
    change_type: str
    old_file_path: str
    new_file_path: str
    hunk_index: int
    old_start_line: int
    old_line_count: int
    new_start_line: int
    new_line_count: int
    matched_old_lines: tuple[int, ...] = ()
    context_old_lines: tuple[int, ...] = ()
    insertion_anchor_line: int | None = None
    source: str = "developer_patch"
    schema_version: str = PATCH_SYMBOL_PROVENANCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "patch_sha256", self.patch_sha256.strip().lower())
        object.__setattr__(self, "change_type", self.change_type.strip().casefold())
        object.__setattr__(self, "old_file_path", _normalize_optional_path(self.old_file_path))
        object.__setattr__(self, "new_file_path", _normalize_optional_path(self.new_file_path))
        object.__setattr__(self, "source", self.source.strip())
        object.__setattr__(
            self,
            "matched_old_lines",
            _validated_lines(self.matched_old_lines, field_name="matched_old_lines"),
        )
        object.__setattr__(
            self,
            "context_old_lines",
            _validated_lines(self.context_old_lines, field_name="context_old_lines"),
        )

        if self.schema_version != PATCH_SYMBOL_PROVENANCE_SCHEMA_VERSION:
            raise ValueError(
                f"schema_version must be {PATCH_SYMBOL_PROVENANCE_SCHEMA_VERSION!r}."
            )
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.patch_sha256):
            raise ValueError("patch_sha256 must be a sha256-prefixed 64-character digest.")
        if self.change_type not in PATCH_CHANGE_TYPES:
            raise ValueError(f"Unsupported patch change_type: {self.change_type!r}.")
        if self.source != "developer_patch":
            raise ValueError("source must be 'developer_patch'.")
        if self.hunk_index < 0:
            raise ValueError("hunk_index must be non-negative.")
        if min(
            self.old_start_line,
            self.old_line_count,
            self.new_start_line,
            self.new_line_count,
        ) < 0:
            raise ValueError("Hunk starts and line counts must be non-negative.")
        if self.insertion_anchor_line is not None and self.insertion_anchor_line < 0:
            raise ValueError("insertion_anchor_line must be non-negative when present.")

        if self.change_type == "added":
            if self.old_file_path or not self.new_file_path:
                raise ValueError("An added file requires only new_file_path.")
        elif self.change_type == "deleted":
            if not self.old_file_path or self.new_file_path:
                raise ValueError("A deleted file requires only old_file_path.")
        elif not self.old_file_path or not self.new_file_path:
            raise ValueError("Modified and renamed files require both patch paths.")
        if self.change_type == "modified" and self.old_file_path != self.new_file_path:
            raise ValueError("A modified file must keep the same patch path.")
        if self.change_type == "renamed" and self.old_file_path == self.new_file_path:
            raise ValueError("A renamed file must change its patch path.")

        for field_name, lines in (
            ("matched_old_lines", self.matched_old_lines),
            ("context_old_lines", self.context_old_lines),
        ):
            if not lines:
                continue
            if self.old_line_count <= 0:
                raise ValueError(f"{field_name} requires a non-empty old hunk range.")
            old_end = self.old_start_line + self.old_line_count - 1
            if any(
                line < self.old_start_line or line > old_end
                for line in lines
            ):
                raise ValueError(f"{field_name} must lie inside the old hunk range.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source": self.source,
            "patch_sha256": self.patch_sha256,
            "change_type": self.change_type,
            "old_file_path": self.old_file_path,
            "new_file_path": self.new_file_path,
            "hunk_index": self.hunk_index,
            "old_start_line": self.old_start_line,
            "old_line_count": self.old_line_count,
            "new_start_line": self.new_start_line,
            "new_line_count": self.new_line_count,
            "matched_old_lines": list(self.matched_old_lines),
            "context_old_lines": list(self.context_old_lines),
            "insertion_anchor_line": self.insertion_anchor_line,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PatchSymbolProvenanceV1":
        _require_fields(payload, PATCH_PROVENANCE_FIELDS, label="provenance")
        return cls(
            schema_version=str(payload.get("schema_version") or ""),
            source=str(payload.get("source") or ""),
            patch_sha256=str(payload.get("patch_sha256") or ""),
            change_type=str(payload.get("change_type") or ""),
            old_file_path=str(payload.get("old_file_path") or ""),
            new_file_path=str(payload.get("new_file_path") or ""),
            hunk_index=int(payload.get("hunk_index") or 0),
            old_start_line=int(payload.get("old_start_line") or 0),
            old_line_count=int(payload.get("old_line_count") or 0),
            new_start_line=int(payload.get("new_start_line") or 0),
            new_line_count=int(payload.get("new_line_count") or 0),
            matched_old_lines=tuple(payload.get("matched_old_lines") or ()),
            context_old_lines=tuple(payload.get("context_old_lines") or ()),
            insertion_anchor_line=(
                int(payload["insertion_anchor_line"])
                if payload.get("insertion_anchor_line") is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class ParsedPatchHunk:
    """One parsed unified-diff hunk with base-commit line evidence."""

    patch_sha256: str
    change_type: str
    old_file_path: str
    new_file_path: str
    hunk_index: int
    old_start_line: int
    old_line_count: int
    new_start_line: int
    new_line_count: int
    deleted_old_lines: tuple[int, ...] = ()
    context_old_lines: tuple[int, ...] = ()
    added_new_lines: tuple[int, ...] = ()
    insertion_anchor_lines: tuple[int, ...] = ()
    section_header: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "deleted_old_lines",
            _validated_lines(self.deleted_old_lines, field_name="deleted_old_lines"),
        )
        object.__setattr__(
            self,
            "context_old_lines",
            _validated_lines(self.context_old_lines, field_name="context_old_lines"),
        )
        object.__setattr__(
            self,
            "added_new_lines",
            _validated_lines(self.added_new_lines, field_name="added_new_lines"),
        )
        anchors = tuple(int(line) for line in self.insertion_anchor_lines)
        if any(line < 0 for line in anchors) or tuple(sorted(set(anchors))) != anchors:
            raise ValueError("insertion_anchor_lines must be unique, sorted, and non-negative.")
        object.__setattr__(self, "insertion_anchor_lines", anchors)
        object.__setattr__(self, "section_header", self.section_header.strip())

        PatchSymbolProvenanceV1(
            patch_sha256=self.patch_sha256,
            change_type=self.change_type,
            old_file_path=self.old_file_path,
            new_file_path=self.new_file_path,
            hunk_index=self.hunk_index,
            old_start_line=self.old_start_line,
            old_line_count=self.old_line_count,
            new_start_line=self.new_start_line,
            new_line_count=self.new_line_count,
            matched_old_lines=self.deleted_old_lines,
            context_old_lines=self.context_old_lines,
            insertion_anchor_line=(anchors[0] if anchors else None),
        )
        if self.added_new_lines:
            if self.new_line_count <= 0:
                raise ValueError("added_new_lines require a non-empty new hunk range.")
            new_end = self.new_start_line + self.new_line_count - 1
            if any(
                line < self.new_start_line or line > new_end
                for line in self.added_new_lines
            ):
                raise ValueError("added_new_lines must lie inside the new hunk range.")

    @property
    def is_metadata_only(self) -> bool:
        return self.old_line_count == 0 and self.new_line_count == 0

    def to_provenance(
        self,
        *,
        matched_old_lines: Sequence[int] | None = None,
        insertion_anchor_line: int | None = None,
    ) -> PatchSymbolProvenanceV1:
        selected_anchor = insertion_anchor_line
        if selected_anchor is None and self.insertion_anchor_lines:
            selected_anchor = self.insertion_anchor_lines[0]
        if (
            selected_anchor is not None
            and self.insertion_anchor_lines
            and selected_anchor not in self.insertion_anchor_lines
        ):
            raise ValueError("insertion_anchor_line must come from this parsed hunk.")
        return PatchSymbolProvenanceV1(
            patch_sha256=self.patch_sha256,
            change_type=self.change_type,
            old_file_path=self.old_file_path,
            new_file_path=self.new_file_path,
            hunk_index=self.hunk_index,
            old_start_line=self.old_start_line,
            old_line_count=self.old_line_count,
            new_start_line=self.new_start_line,
            new_line_count=self.new_line_count,
            matched_old_lines=tuple(
                self.deleted_old_lines
                if matched_old_lines is None
                else matched_old_lines
            ),
            context_old_lines=self.context_old_lines,
            insertion_anchor_line=selected_anchor,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "patch_sha256": self.patch_sha256,
            "change_type": self.change_type,
            "old_file_path": self.old_file_path,
            "new_file_path": self.new_file_path,
            "hunk_index": self.hunk_index,
            "old_start_line": self.old_start_line,
            "old_line_count": self.old_line_count,
            "new_start_line": self.new_start_line,
            "new_line_count": self.new_line_count,
            "deleted_old_lines": list(self.deleted_old_lines),
            "context_old_lines": list(self.context_old_lines),
            "added_new_lines": list(self.added_new_lines),
            "insertion_anchor_lines": list(self.insertion_anchor_lines),
            "section_header": self.section_header,
            "is_metadata_only": self.is_metadata_only,
        }


_HUNK_HEADER_PATTERN = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:\s?(.*))?$"
)


def parse_unified_diff(patch: str) -> tuple[ParsedPatchHunk, ...]:
    """Parse a Git unified diff into file-qualified hunk evidence."""

    patch_text = str(patch or "")
    lines = patch_text.splitlines()
    block_starts = [
        index for index, line in enumerate(lines) if line.startswith("diff --git ")
    ]
    if not block_starts:
        raise ValueError("Patch contains no 'diff --git' file headers.")

    patch_sha256 = make_patch_sha256(patch_text)
    parsed: list[ParsedPatchHunk] = []
    block_starts.append(len(lines))
    for block_number in range(len(block_starts) - 1):
        start = block_starts[block_number]
        end = block_starts[block_number + 1]
        block = lines[start:end]
        diff_old_path, diff_new_path = _parse_diff_git_paths(block[0])
        old_header_path = ""
        new_header_path = ""
        rename_from = ""
        rename_to = ""
        is_added = False
        is_deleted = False
        hunk_positions: list[int] = []
        for offset, line in enumerate(block[1:], start=1):
            if line.startswith("@@ "):
                hunk_positions.append(offset)
                continue
            if hunk_positions:
                continue
            if line.startswith("new file mode "):
                is_added = True
            elif line.startswith("deleted file mode "):
                is_deleted = True
            elif line.startswith("rename from "):
                rename_from = _decode_patch_path(line[len("rename from ") :])
            elif line.startswith("rename to "):
                rename_to = _decode_patch_path(line[len("rename to ") :])
            elif line.startswith("--- "):
                old_header_path = _parse_file_marker_path(line[4:], prefix="a/")
            elif line.startswith("+++ "):
                new_header_path = _parse_file_marker_path(line[4:], prefix="b/")

        old_path = rename_from or old_header_path or diff_old_path
        new_path = rename_to or new_header_path or diff_new_path
        if is_added or old_header_path == "/dev/null":
            old_path = ""
            change_type = "added"
        elif is_deleted or new_header_path == "/dev/null":
            new_path = ""
            change_type = "deleted"
        elif rename_from or rename_to or old_path != new_path:
            change_type = "renamed"
        else:
            change_type = "modified"

        old_path = _normalize_optional_path(old_path)
        new_path = _normalize_optional_path(new_path)
        if not hunk_positions:
            parsed.append(
                ParsedPatchHunk(
                    patch_sha256=patch_sha256,
                    change_type=change_type,
                    old_file_path=old_path,
                    new_file_path=new_path,
                    hunk_index=0,
                    old_start_line=0,
                    old_line_count=0,
                    new_start_line=0,
                    new_line_count=0,
                )
            )
            continue

        hunk_positions.append(len(block))
        for hunk_index in range(len(hunk_positions) - 1):
            header_position = hunk_positions[hunk_index]
            next_position = hunk_positions[hunk_index + 1]
            parsed.append(
                _parse_hunk(
                    block[header_position:next_position],
                    patch_sha256=patch_sha256,
                    change_type=change_type,
                    old_file_path=old_path,
                    new_file_path=new_path,
                    hunk_index=hunk_index,
                )
            )
    return tuple(parsed)


def _parse_diff_git_paths(line: str) -> tuple[str, str]:
    try:
        tokens = shlex.split(line, posix=True)
    except ValueError as exc:
        raise ValueError(f"Invalid diff file header: {exc}") from exc
    if len(tokens) != 4 or tokens[:2] != ["diff", "--git"]:
        raise ValueError(f"Invalid diff file header: {line!r}.")
    return (
        _strip_git_side_prefix(tokens[2], "a/"),
        _strip_git_side_prefix(tokens[3], "b/"),
    )


def _decode_patch_path(value: str) -> str:
    text = value.strip()
    if text.startswith('"'):
        try:
            values = shlex.split(text, posix=True)
        except ValueError as exc:
            raise ValueError(f"Invalid quoted patch path: {exc}") from exc
        if len(values) != 1:
            raise ValueError(f"Invalid quoted patch path: {value!r}.")
        return values[0]
    return text


def _parse_file_marker_path(value: str, *, prefix: str) -> str:
    text = value.split("\t", 1)[0].strip()
    decoded = _decode_patch_path(text)
    if decoded == "/dev/null":
        return decoded
    return _strip_git_side_prefix(decoded, prefix)


def _strip_git_side_prefix(value: str, prefix: str) -> str:
    return value[len(prefix) :] if value.startswith(prefix) else value


def _parse_hunk(
    lines: Sequence[str],
    *,
    patch_sha256: str,
    change_type: str,
    old_file_path: str,
    new_file_path: str,
    hunk_index: int,
) -> ParsedPatchHunk:
    match = _HUNK_HEADER_PATTERN.fullmatch(lines[0])
    if match is None:
        raise ValueError(f"Invalid unified diff hunk header: {lines[0]!r}.")
    old_start = int(match.group(1))
    old_count = int(match.group(2) or 1)
    new_start = int(match.group(3))
    new_count = int(match.group(4) or 1)
    section_header = str(match.group(5) or "")
    old_cursor = old_start
    new_cursor = new_start
    observed_old = 0
    observed_new = 0
    deleted_old_lines: list[int] = []
    context_old_lines: list[int] = []
    added_new_lines: list[int] = []
    insertion_points: list[int] = []
    previous_prefix = ""

    for line in lines[1:]:
        if line == r"\ No newline at end of file":
            continue
        if not line or line[0] not in {" ", "+", "-"}:
            raise ValueError(f"Malformed unified diff hunk line: {line!r}.")
        prefix = line[0]
        if prefix == " ":
            context_old_lines.append(old_cursor)
            old_cursor += 1
            new_cursor += 1
            observed_old += 1
            observed_new += 1
        elif prefix == "-":
            deleted_old_lines.append(old_cursor)
            old_cursor += 1
            observed_old += 1
        else:
            if previous_prefix != "+":
                insertion_points.append(old_cursor)
            added_new_lines.append(new_cursor)
            new_cursor += 1
            observed_new += 1
        previous_prefix = prefix

    if observed_old != old_count or observed_new != new_count:
        raise ValueError(
            "Hunk body line counts do not match its header: "
            f"expected old/new {old_count}/{new_count}, "
            f"observed {observed_old}/{observed_new}."
        )
    anchors = tuple(
        sorted(
            {
                _insertion_anchor(
                    point,
                    old_start=old_start,
                    old_count=old_count,
                    context_old_lines=context_old_lines,
                )
                for point in insertion_points
            }
        )
    )
    return ParsedPatchHunk(
        patch_sha256=patch_sha256,
        change_type=change_type,
        old_file_path=old_file_path,
        new_file_path=new_file_path,
        hunk_index=hunk_index,
        old_start_line=old_start,
        old_line_count=old_count,
        new_start_line=new_start,
        new_line_count=new_count,
        deleted_old_lines=tuple(deleted_old_lines),
        context_old_lines=tuple(context_old_lines),
        added_new_lines=tuple(added_new_lines),
        insertion_anchor_lines=anchors,
        section_header=section_header,
    )


def _insertion_anchor(
    insertion_point: int,
    *,
    old_start: int,
    old_count: int,
    context_old_lines: Sequence[int],
) -> int:
    context = set(context_old_lines)
    if insertion_point - 1 in context:
        return insertion_point - 1
    if insertion_point in context:
        return insertion_point
    if old_count <= 0:
        return 0
    old_end = old_start + old_count - 1
    return min(max(insertion_point, old_start), old_end)


def make_symbol_gold_id(
    *,
    ticket_id: str,
    repo: str,
    base_commit: str,
    file_path: str,
    mapping_status: str,
    mapping_method: str,
    symbol_id: str,
    qualified_name: str,
    symbol_kind: str,
    start_line: int | None,
    start_column: int | None,
    end_line: int | None,
    end_column: int | None,
    exclusion_reason: str,
    provenance: PatchSymbolProvenanceV1,
) -> str:
    fields = (
        ticket_id.strip(),
        repo.strip(),
        base_commit.strip(),
        normalize_repository_path(file_path),
        mapping_status.strip().casefold(),
        mapping_method.strip().casefold(),
        symbol_id.strip(),
        qualified_name.strip(),
        symbol_kind.strip(),
        str(start_line) if start_line is not None else "",
        str(start_column) if start_column is not None else "",
        str(end_line) if end_line is not None else "",
        str(end_column) if end_column is not None else "",
        exclusion_reason.strip().casefold(),
        provenance.patch_sha256,
        provenance.change_type,
        provenance.old_file_path,
        provenance.new_file_path,
        str(provenance.hunk_index),
        str(provenance.old_start_line),
        str(provenance.old_line_count),
        str(provenance.new_start_line),
        str(provenance.new_line_count),
        ",".join(str(line) for line in provenance.matched_old_lines),
        ",".join(str(line) for line in provenance.context_old_lines),
        (
            str(provenance.insertion_anchor_line)
            if provenance.insertion_anchor_line is not None
            else ""
        ),
    )
    return f"sha256:{hashlib.sha256(chr(0).join(fields).encode('utf-8')).hexdigest()}"


@dataclass(frozen=True, slots=True)
class SymbolGoldRecordV1:
    """One mapped or explicitly excluded file-qualified symbol gold record."""

    gold_id: str
    ticket_id: str
    repo: str
    base_commit: str
    file_path: str
    mapping_status: str
    mapping_method: str
    mapping_confidence: float
    exclusion_reason: str
    symbol_id: str
    qualified_name: str
    symbol_kind: str
    start_line: int | None
    start_column: int | None
    end_line: int | None
    end_column: int | None
    provenance: PatchSymbolProvenanceV1
    symbol_record_schema_version: str = SYMBOL_RECORD_SCHEMA_VERSION
    schema_version: str = SYMBOL_GOLD_RECORD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "gold_id", self.gold_id.strip().lower())
        object.__setattr__(self, "ticket_id", self.ticket_id.strip())
        object.__setattr__(self, "repo", self.repo.strip())
        object.__setattr__(self, "base_commit", self.base_commit.strip())
        object.__setattr__(self, "file_path", normalize_repository_path(self.file_path))
        object.__setattr__(self, "mapping_status", self.mapping_status.strip().casefold())
        object.__setattr__(self, "mapping_method", self.mapping_method.strip().casefold())
        object.__setattr__(self, "exclusion_reason", self.exclusion_reason.strip().casefold())
        object.__setattr__(self, "symbol_id", self.symbol_id.strip().lower())
        object.__setattr__(self, "qualified_name", self.qualified_name.strip())
        object.__setattr__(self, "symbol_kind", self.symbol_kind.strip())

        if self.schema_version != SYMBOL_GOLD_RECORD_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {SYMBOL_GOLD_RECORD_SCHEMA_VERSION!r}.")
        if self.symbol_record_schema_version != SYMBOL_RECORD_SCHEMA_VERSION:
            raise ValueError(
                f"symbol_record_schema_version must be {SYMBOL_RECORD_SCHEMA_VERSION!r}."
            )
        if not self.ticket_id or not self.repo or not self.base_commit:
            raise ValueError("ticket_id, repo, and base_commit must be non-empty.")
        if self.mapping_status not in GOLD_MAPPING_STATUSES:
            raise ValueError(f"Unsupported mapping_status: {self.mapping_status!r}.")
        if self.mapping_method not in GOLD_MAPPING_METHODS:
            raise ValueError(f"Unsupported mapping_method: {self.mapping_method!r}.")
        if not math.isfinite(float(self.mapping_confidence)):
            raise ValueError("mapping_confidence must be finite.")
        if not 0.0 <= float(self.mapping_confidence) <= 1.0:
            raise ValueError("mapping_confidence must be between 0 and 1.")

        base_path = (
            self.provenance.new_file_path
            if self.provenance.change_type == "added"
            else self.provenance.old_file_path
        )
        if self.file_path != base_path:
            raise ValueError("file_path must identify the patch path used for base mapping.")

        positions = (self.start_line, self.start_column, self.end_line, self.end_column)
        if self.mapping_status == "mapped":
            if self.provenance.change_type == "added":
                raise ValueError("New files cannot be mapped to a base-commit symbol.")
            if self.mapping_method == "unmapped" or self.exclusion_reason:
                raise ValueError("Mapped records require a mapping method and no exclusion reason.")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.symbol_id):
                raise ValueError("Mapped records require a valid symbol_id.")
            if not self.qualified_name or not self.symbol_kind:
                raise ValueError("Mapped records require qualified_name and symbol_kind.")
            if any(value is None for value in positions):
                raise ValueError("Mapped records require a complete symbol range.")
            assert all(value is not None for value in positions)
            if self.start_line <= 0 or self.end_line <= 0:
                raise ValueError("Mapped symbol line numbers must be positive.")
            if self.start_column < 0 or self.end_column < 0:
                raise ValueError("Mapped symbol columns must be non-negative.")
            if (self.end_line, self.end_column) < (self.start_line, self.start_column):
                raise ValueError("Mapped symbol end position must not precede its start.")
            if float(self.mapping_confidence) <= 0.0:
                raise ValueError("Mapped records require positive mapping_confidence.")
        else:
            if self.mapping_method != "unmapped":
                raise ValueError("Excluded records must use mapping_method='unmapped'.")
            if self.exclusion_reason not in GOLD_EXCLUSION_REASONS:
                raise ValueError("Excluded records require a supported exclusion_reason.")
            if any((self.symbol_id, self.qualified_name, self.symbol_kind)) or any(
                value is not None for value in positions
            ):
                raise ValueError("Excluded records must not contain guessed symbol fields.")
            if float(self.mapping_confidence) != 0.0:
                raise ValueError("Excluded records require mapping_confidence=0.")

        expected_id = make_symbol_gold_id(
            ticket_id=self.ticket_id,
            repo=self.repo,
            base_commit=self.base_commit,
            file_path=self.file_path,
            mapping_status=self.mapping_status,
            mapping_method=self.mapping_method,
            symbol_id=self.symbol_id,
            qualified_name=self.qualified_name,
            symbol_kind=self.symbol_kind,
            start_line=self.start_line,
            start_column=self.start_column,
            end_line=self.end_line,
            end_column=self.end_column,
            exclusion_reason=self.exclusion_reason,
            provenance=self.provenance,
        )
        if self.gold_id != expected_id:
            raise ValueError("gold_id does not match the record identity fields.")

    @classmethod
    def create_mapped(
        cls,
        *,
        ticket_id: str,
        repo: str,
        base_commit: str,
        file_path: str,
        mapping_method: str,
        mapping_confidence: float,
        symbol_id: str,
        qualified_name: str,
        symbol_kind: str,
        start_line: int,
        start_column: int,
        end_line: int,
        end_column: int,
        provenance: PatchSymbolProvenanceV1,
    ) -> "SymbolGoldRecordV1":
        identity = {
            "ticket_id": ticket_id,
            "repo": repo,
            "base_commit": base_commit,
            "file_path": file_path,
            "mapping_status": "mapped",
            "mapping_method": mapping_method,
            "symbol_id": symbol_id,
            "exclusion_reason": "",
            "provenance": provenance,
        }
        return cls(
            gold_id=make_symbol_gold_id(
                qualified_name=qualified_name,
                symbol_kind=symbol_kind,
                start_line=start_line,
                start_column=start_column,
                end_line=end_line,
                end_column=end_column,
                **identity,
            ),
            mapping_confidence=mapping_confidence,
            qualified_name=qualified_name,
            symbol_kind=symbol_kind,
            start_line=start_line,
            start_column=start_column,
            end_line=end_line,
            end_column=end_column,
            **identity,
        )

    @classmethod
    def create_excluded(
        cls,
        *,
        ticket_id: str,
        repo: str,
        base_commit: str,
        file_path: str,
        exclusion_reason: str,
        provenance: PatchSymbolProvenanceV1,
    ) -> "SymbolGoldRecordV1":
        identity = {
            "ticket_id": ticket_id,
            "repo": repo,
            "base_commit": base_commit,
            "file_path": file_path,
            "mapping_status": "excluded",
            "mapping_method": "unmapped",
            "symbol_id": "",
            "exclusion_reason": exclusion_reason,
            "provenance": provenance,
        }
        return cls(
            gold_id=make_symbol_gold_id(
                qualified_name="",
                symbol_kind="",
                start_line=None,
                start_column=None,
                end_line=None,
                end_column=None,
                **identity,
            ),
            mapping_confidence=0.0,
            qualified_name="",
            symbol_kind="",
            start_line=None,
            start_column=None,
            end_line=None,
            end_column=None,
            **identity,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "symbol_record_schema_version": self.symbol_record_schema_version,
            "gold_id": self.gold_id,
            "ticket_id": self.ticket_id,
            "repo": self.repo,
            "base_commit": self.base_commit,
            "file_path": self.file_path,
            "mapping_status": self.mapping_status,
            "mapping_method": self.mapping_method,
            "mapping_confidence": float(self.mapping_confidence),
            "exclusion_reason": self.exclusion_reason,
            "symbol_id": self.symbol_id,
            "qualified_name": self.qualified_name,
            "symbol_kind": self.symbol_kind,
            "start_line": self.start_line,
            "start_column": self.start_column,
            "end_line": self.end_line,
            "end_column": self.end_column,
            "provenance": self.provenance.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SymbolGoldRecordV1":
        _require_fields(payload, SYMBOL_GOLD_RECORD_FIELDS, label="symbol gold record")
        provenance_payload = payload.get("provenance")
        if not isinstance(provenance_payload, dict):
            raise ValueError("provenance must be an object.")

        def optional_int(key: str) -> int | None:
            return int(payload[key]) if payload.get(key) is not None else None

        return cls(
            schema_version=str(
                payload.get("schema_version") or SYMBOL_GOLD_RECORD_SCHEMA_VERSION
            ),
            symbol_record_schema_version=str(
                payload.get("symbol_record_schema_version")
                or SYMBOL_RECORD_SCHEMA_VERSION
            ),
            gold_id=str(payload.get("gold_id") or ""),
            ticket_id=str(payload.get("ticket_id") or ""),
            repo=str(payload.get("repo") or ""),
            base_commit=str(payload.get("base_commit") or ""),
            file_path=str(payload.get("file_path") or ""),
            mapping_status=str(payload.get("mapping_status") or ""),
            mapping_method=str(payload.get("mapping_method") or ""),
            mapping_confidence=float(payload.get("mapping_confidence") or 0.0),
            exclusion_reason=str(payload.get("exclusion_reason") or ""),
            symbol_id=str(payload.get("symbol_id") or ""),
            qualified_name=str(payload.get("qualified_name") or ""),
            symbol_kind=str(payload.get("symbol_kind") or ""),
            start_line=optional_int("start_line"),
            start_column=optional_int("start_column"),
            end_line=optional_int("end_line"),
            end_column=optional_int("end_column"),
            provenance=PatchSymbolProvenanceV1.from_dict(provenance_payload),
        )


def make_module_symbol_id(*, repo: str, base_commit: str, file_path: str) -> str:
    fields = (
        "symbol-gold-module-v1",
        repo.strip(),
        base_commit.strip(),
        normalize_repository_path(file_path),
    )
    return f"sha256:{hashlib.sha256(chr(0).join(fields).encode('utf-8')).hexdigest()}"


def map_patch_hunks_to_symbol_gold(
    *,
    ticket_id: str,
    repo: str,
    base_commit: str,
    hunks: Sequence[ParsedPatchHunk],
    parse_results: Sequence[SymbolParseResult],
) -> tuple[SymbolGoldRecordV1, ...]:
    """Map patch evidence to innermost base-commit symbols or exclusions."""

    normalized_ticket_id = str(ticket_id or "").strip()
    normalized_repo = str(repo or "").strip()
    normalized_commit = str(base_commit or "").strip()
    if not normalized_ticket_id or not normalized_repo or not normalized_commit:
        raise ValueError("ticket_id, repo, and base_commit must be non-empty.")

    results_by_file: dict[str, SymbolParseResult] = {}
    for result in parse_results:
        if result.repo != normalized_repo or result.base_commit != normalized_commit:
            raise ValueError("Symbol parse result repository snapshot does not match the mapper input.")
        if result.file_path in results_by_file:
            raise ValueError(f"Duplicate symbol parse result for {result.file_path!r}.")
        results_by_file[result.file_path] = result

    gold: list[SymbolGoldRecordV1] = []
    for hunk in hunks:
        file_path = hunk.new_file_path if hunk.change_type == "added" else hunk.old_file_path
        if hunk.change_type == "added":
            gold.append(
                _excluded_gold(
                    ticket_id=normalized_ticket_id,
                    repo=normalized_repo,
                    base_commit=normalized_commit,
                    file_path=file_path,
                    reason="new_file",
                    hunk=hunk,
                )
            )
            continue

        parse_result = results_by_file.get(file_path)
        if parse_result is None:
            gold.append(
                _excluded_gold(
                    ticket_id=normalized_ticket_id,
                    repo=normalized_repo,
                    base_commit=normalized_commit,
                    file_path=file_path,
                    reason="missing_base_file",
                    hunk=hunk,
                )
            )
            continue
        exclusion_reason = _parse_exclusion_reason(parse_result.status)
        if exclusion_reason:
            gold.append(
                _excluded_gold(
                    ticket_id=normalized_ticket_id,
                    repo=normalized_repo,
                    base_commit=normalized_commit,
                    file_path=file_path,
                    reason=exclusion_reason,
                    hunk=hunk,
                )
            )
            continue

        if hunk.deleted_old_lines:
            method = (
                "deleted_line_containment"
                if hunk.change_type == "deleted"
                else "modified_line_containment"
            )
            evidence = [(line, None) for line in hunk.deleted_old_lines]
        elif hunk.insertion_anchor_lines:
            method = "insertion_anchor_containment"
            evidence = [(line, line) for line in hunk.insertion_anchor_lines]
        else:
            method = "module_level"
            evidence = []

        symbols = tuple(parse_result.symbols)
        mapped: dict[tuple[str, int | None], tuple[SymbolRecordV1, list[int]]] = {}
        module_evidence: dict[int | None, list[int]] = {}
        ambiguous_lines: list[int] = []
        for line, anchor in evidence:
            matches = [
                symbol
                for symbol in symbols
                if symbol.start_line <= line <= symbol.end_line
            ]
            if not matches:
                module_evidence.setdefault(anchor, []).append(line)
                continue
            best_span = min(_symbol_span(symbol) for symbol in matches)
            innermost = [
                symbol for symbol in matches if _symbol_span(symbol) == best_span
            ]
            if len(innermost) != 1:
                ambiguous_lines.append(line)
                continue
            symbol = innermost[0]
            key = (symbol.symbol_id, anchor)
            if key not in mapped:
                mapped[key] = (symbol, [])
            mapped[key][1].append(line)

        for (_, anchor), (symbol, lines) in mapped.items():
            provenance = hunk.to_provenance(
                matched_old_lines=(() if anchor is not None else tuple(lines)),
                insertion_anchor_line=anchor,
            )
            gold.append(
                SymbolGoldRecordV1.create_mapped(
                    ticket_id=normalized_ticket_id,
                    repo=normalized_repo,
                    base_commit=normalized_commit,
                    file_path=file_path,
                    mapping_method=method,
                    mapping_confidence=(0.8 if anchor is not None else 1.0),
                    symbol_id=symbol.symbol_id,
                    qualified_name=symbol.qualified_name,
                    symbol_kind=symbol.symbol_kind,
                    start_line=symbol.start_line,
                    start_column=symbol.start_column,
                    end_line=symbol.end_line,
                    end_column=symbol.end_column,
                    provenance=provenance,
                )
            )

        if not evidence:
            module_evidence[None] = []
        for anchor, lines in module_evidence.items():
            gold.append(
                _module_gold(
                    ticket_id=normalized_ticket_id,
                    repo=normalized_repo,
                    base_commit=normalized_commit,
                    file_path=file_path,
                    hunk=hunk,
                    evidence_lines=lines,
                    insertion_anchor_line=anchor,
                    confidence=(0.5 if hunk.is_metadata_only else 0.8 if anchor is not None else 1.0),
                )
            )
        for line in ambiguous_lines:
            gold.append(
                _excluded_gold(
                    ticket_id=normalized_ticket_id,
                    repo=normalized_repo,
                    base_commit=normalized_commit,
                    file_path=file_path,
                    reason="ambiguous_mapping",
                    hunk=hunk,
                    matched_old_lines=(line,),
                )
            )
    return tuple(gold)


def _symbol_span(symbol: SymbolRecordV1) -> tuple[int, int, int]:
    return (
        symbol.end_line - symbol.start_line,
        symbol.end_column if symbol.end_line != symbol.start_line else symbol.end_column - symbol.start_column,
        -symbol.start_column,
    )


def _parse_exclusion_reason(status: str) -> str:
    return {
        "syntax_error": "parser_failure",
        "parser_error": "parser_failure",
        "unsupported_language": "unsupported_language",
        "missing_file": "missing_base_file",
        "invalid_path": "invalid_path",
        "symlink_escape": "symlink_escape",
    }.get(status, "")


def _excluded_gold(
    *,
    ticket_id: str,
    repo: str,
    base_commit: str,
    file_path: str,
    reason: str,
    hunk: ParsedPatchHunk,
    matched_old_lines: Sequence[int] | None = None,
) -> SymbolGoldRecordV1:
    return SymbolGoldRecordV1.create_excluded(
        ticket_id=ticket_id,
        repo=repo,
        base_commit=base_commit,
        file_path=file_path,
        exclusion_reason=reason,
        provenance=hunk.to_provenance(matched_old_lines=matched_old_lines),
    )


def _module_gold(
    *,
    ticket_id: str,
    repo: str,
    base_commit: str,
    file_path: str,
    hunk: ParsedPatchHunk,
    evidence_lines: Sequence[int],
    insertion_anchor_line: int | None,
    confidence: float,
) -> SymbolGoldRecordV1:
    evidence = tuple(evidence_lines)
    start_line = min(evidence) if evidence else max(1, hunk.old_start_line)
    end_line = max(evidence) if evidence else max(start_line, hunk.old_start_line + hunk.old_line_count - 1)
    return SymbolGoldRecordV1.create_mapped(
        ticket_id=ticket_id,
        repo=repo,
        base_commit=base_commit,
        file_path=file_path,
        mapping_method="module_level",
        mapping_confidence=confidence,
        symbol_id=make_module_symbol_id(
            repo=repo,
            base_commit=base_commit,
            file_path=file_path,
        ),
        qualified_name="<module>",
        symbol_kind="module",
        start_line=start_line,
        start_column=0,
        end_line=end_line,
        end_column=0,
        provenance=hunk.to_provenance(
            matched_old_lines=(() if insertion_anchor_line is not None else evidence),
            insertion_anchor_line=insertion_anchor_line,
        ),
    )
