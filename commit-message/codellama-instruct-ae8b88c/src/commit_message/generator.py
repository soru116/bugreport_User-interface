"""Parse a visible diff and ask Code Llama-Instruct for a commit-message draft."""
from __future__ import annotations

from dataclasses import dataclass, field
import difflib
import json
import posixpath
import re
from typing import Any

from .llm import ModelError, generate_with_codellama


class PatchError(ValueError):
    """Invalid or unsupported patch input."""


@dataclass
class FileChange:
    path: str
    old_path: str
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    hunks: list[tuple[list[str], list[str]]] = field(default_factory=list)


def _path(header: str, prefix: str) -> str:
    value = header.split("\t", 1)[0]
    if value.startswith('"'):
        # Git uses C-quoted UTF-8 bytes, including octal escapes.
        try:
            has_octal = bool(re.search(r"\\[0-7]{3}", value))
            value = re.sub(r"\\([0-7]{3})", lambda m: f"\\u{int(m[1], 8):04x}", value)
            value = json.loads(value)
            if has_octal:
                value = value.encode("latin1").decode("utf-8")
        except (ValueError, TypeError) as exc:
            raise PatchError("Invalid quoted file path") from exc
    if value.startswith(prefix):
        value = value[len(prefix):]
    if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise PatchError("File path is empty or contains control characters")
    return value


def _source_lines(text: str) -> list[str]:
    # difflib needs terminated lines; preserve missing final newline via a marker.
    return text.splitlines(keepends=True)


def normalize_patch(patch_data: str | dict[str, Any]) -> str:
    """Accept diff text, {'patch': text}, or {file_path, before, after}.

    Explicit patch/unified_diff fields take precedence, even when empty/invalid.
    Before/after are complete file contents; neither is read from disk.
    """
    if isinstance(patch_data, str):
        return patch_data
    if not isinstance(patch_data, dict):
        raise PatchError("Expected diff text or a patch object")
    for key in ("patch", "unified_diff"):
        if key in patch_data:
            if not isinstance(patch_data[key], str):
                raise PatchError(f"{key} must be a string")
            return patch_data[key]
    path = patch_data.get("file_path")
    before, after = patch_data.get("before"), patch_data.get("after")
    if not isinstance(path, str) or not path or path == "/dev/null":
        raise PatchError("Before/after input requires file_path")
    if not isinstance(before, str) or not isinstance(after, str):
        raise PatchError("Before/after input requires two strings")
    old = json.dumps("a/" + path, ensure_ascii=False)
    new = json.dumps("b/" + path, ensure_ascii=False)
    pieces = difflib.unified_diff(_source_lines(before), _source_lines(after),
                                  fromfile=old, tofile=new)
    return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                   for line in pieces)


_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$")
_META = ("index ", "new file mode ", "deleted file mode ", "old mode ", "new mode ")


def parse_patch(text: str) -> list[FileChange]:
    """Parse textual unified diffs, checking every hunk's declared line counts.

    Supports git and plain unified headers. Binary, combined and metadata-only
    diffs are intentionally unsupported. No patch application is attempted.
    """
    if not text.strip():
        raise PatchError("Patch is empty")
    # split on LF only: other control characters may be literal source content.
    lines = text.replace("\r\n", "\n").split("\n")
    if lines[-1] == "":
        lines.pop()
    changes: list[FileChange] = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("diff --git "):
            i += 1
            while i < len(lines) and lines[i].startswith(_META):
                i += 1
        if i >= len(lines) or not lines[i].startswith("--- "):
            raise PatchError(f"Expected unified file header at line {i + 1}")
        old = _path(lines[i][4:], "a/")
        i += 1
        if i >= len(lines) or not lines[i].startswith("+++ "):
            raise PatchError(f"Missing +++ header at line {i + 1}")
        new = _path(lines[i][4:], "b/")
        if old == new == "/dev/null":
            raise PatchError("Both file paths are /dev/null")
        change = FileChange(old if new == "/dev/null" else new, old)
        i += 1
        hunks = 0
        previous_old_end = previous_new_end = -1
        while i < len(lines) and lines[i].startswith("@@"):
            match = _HUNK.fullmatch(lines[i])
            if not match:
                raise PatchError(f"Invalid hunk header at line {i + 1}")
            old_start, old_count, new_start, new_count = (
                int(match[1]), int(match[2] or 1), int(match[3]), int(match[4] or 1))
            old_pos = old_start if old_count == 0 else old_start - 1
            new_pos = new_start if new_count == 0 else new_start - 1
            if (old_pos < 0 or new_pos < 0 or old_pos < previous_old_end
                    or new_pos < previous_new_end
                    or (old == "/dev/null" and old_count)
                    or (new == "/dev/null" and new_count)):
                raise PatchError("Invalid or overlapping hunk range")
            previous_old_end, previous_new_end = old_pos + old_count, new_pos + new_count
            i += 1
            used_old = used_new = 0
            before: list[str] = []
            after: list[str] = []
            can_mark = False
            while used_old < old_count or used_new < new_count:
                if i >= len(lines):
                    raise PatchError("Truncated hunk")
                line = lines[i]
                if line == "\\ No newline at end of file" and can_mark:
                    can_mark = False
                    i += 1
                    continue
                if not line or line[0] not in " +-":
                    raise PatchError(f"Invalid hunk line at line {i + 1}")
                tag, content = line[0], line[1:]
                used_old += tag in " -"
                used_new += tag in " +"
                if tag in " -":
                    before.append(content)
                if tag in " +":
                    after.append(content)
                if used_old > old_count or used_new > new_count:
                    raise PatchError("Hunk line counts do not match header")
                if tag == "+":
                    change.added.append(content)
                elif tag == "-":
                    change.removed.append(content)
                can_mark = True
                i += 1
            if i < len(lines) and lines[i] == "\\ No newline at end of file" and can_mark:
                i += 1
            hunks += 1
            change.hunks.append((before, after))
        if not hunks or not (change.added or change.removed):
            raise PatchError("File has no textual changes")
        if any(c.path == change.path for c in changes):
            raise PatchError("Repeated file sections are unsupported; combine their hunks")
        changes.append(change)
    return changes


def _scope(files: list[str]) -> str:
    if len(files) == 1:
        raw = posixpath.splitext(posixpath.basename(files[0]))[0]
    else:
        directories = [p.split("/")[:-1] for p in files]
        common = []
        for parts in zip(*directories):
            if len(set(parts)) != 1:
                break
            common.append(parts[0])
        raw = common[-1] if common else "multiple"
    return re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")[:32] or "files"


def failure(error: str, generation_method: str = "code_llama_instruct_zero_shot") -> dict[str, Any]:
    return {"success": False, "commit_message": "", "changed_files": [],
            "generation_method": generation_method, "error": error, "diagnostics": None}


def _changed_lines_for_prompt(changes: list[FileChange]) -> str:
    sections = []
    for change in changes:
        lines = [f"FILE: {change.path}"]
        lines.extend(f"REMOVED: {line}" for line in change.removed)
        lines.extend(f"ADDED: {line}" for line in change.added)
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def generate_commit_message(
    patch_data: str | dict[str, Any],
    *,
    model: str | None = None,
    ollama_url: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Return a JSON-serializable draft or a structured input error.

    'success' means message generation only, never repair correctness.
    The diff is authoritative for changed files. Optional Ticket/test context helps
    the model explain why the visible change was needed.
    """
    try:
        patch = normalize_patch(patch_data)
        changes = parse_patch(patch)
    except PatchError as exc:
        return failure(str(exc))
    files = [c.path for c in changes]
    context = patch_data if isinstance(patch_data, dict) else None
    try:
        draft, selected_model = generate_with_codellama(
            patch, context, changed_lines=_changed_lines_for_prompt(changes),
            model=model, ollama_url=ollama_url, timeout=timeout)
    except ModelError as exc:
        return failure(str(exc))
    details = [
        f"Model: {selected_model}",
        "Prompting: zero-shot instruction following; no examples were supplied.",
        f"Visible change: {sum(len(c.added) for c in changes)} added, "
        f"{sum(len(c.removed) for c in changes)} removed lines across {len(files)} file(s).",
    ]
    return {"success": True,
            "commit_message": f"{draft['type']}({_scope(files)}): {draft['subject']}",
            "changed_files": files,
            "generation_method": "code_llama_instruct_zero_shot",
            "error": None,
            "diagnostics": {"analysis_basis": "llm_ticket_and_diff_analysis",
                            "model": selected_model,
                            "repair_verification": "not_run",
                            "details": "\n".join(details)}}
