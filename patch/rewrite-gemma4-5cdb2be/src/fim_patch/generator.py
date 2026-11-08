from __future__ import annotations

import ast
import builtins
import copy
import difflib
import hashlib
import io
import json
import re
import subprocess
import symtable
import textwrap
import tokenize
from pathlib import Path, PurePosixPath

from .backend import GenerationError, OllamaFIM, OllamaRewrite


def git(root, *args, data=None):
    result = subprocess.run(["git", "-C", str(root), *args], input=data,
                            capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", errors="replace"))
    return result.stdout


def sha(data):
    return hashlib.sha256(data).hexdigest()


def source_for(ticket, location, repo, row):
    root = Path(repo).resolve()
    base = ticket.get("base_commit") or location.get("base_commit")
    if not isinstance(base, str) or not re.fullmatch(r"[0-9a-fA-F]{7,40}", base):
        raise ValueError("A fixed base_commit SHA is required")
    commit = git(root, "rev-parse", "--verify", base + "^{commit}").decode().strip()
    if location.get("base_commit"):
        other = location["base_commit"]
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", str(other)):
            raise ValueError("Invalid localization base_commit")
        if git(root, "rev-parse", "--verify", other + "^{commit}").decode().strip() != commit:
            raise ValueError("Ticket and localization base_commit disagree")
    if git(root, "rev-parse", "HEAD").decode().strip() != commit:
        raise ValueError("HEAD does not match base_commit")
    path = str(row.get("file_path", "")).replace("\\", "/")
    relative = PurePosixPath(path)
    if (not path or relative.is_absolute() or ".." in relative.parts or
            any(c in path for c in (":", "\n", "\r", "\t", '"'))):
        raise ValueError("Invalid source path")
    if (relative.suffix != ".py" or any(x in ("test", "tests") for x in relative.parts)
            or relative.name.startswith("test_") or relative.name.endswith("_test.py")):
        raise ValueError("MVP supports Python source files only, not tests")
    target = (root / path).resolve()
    if not target.is_relative_to(root) or target.is_symlink() or not target.is_file():
        raise ValueError("Source is missing or outside repository")
    if target.stat().st_size > 500_000:
        raise ValueError("Source exceeds 500 KB limit")
    raw = target.read_bytes()
    expected = git(root, "show", f"{commit}:{path}")
    if expected != raw:
        raise ValueError("Source differs from base_commit (including line endings)")
    digest = (location.get("source_file_sha256") or {}).get(path)
    if digest and digest != sha(raw):
        raise ValueError("Source hash differs from localization")
    if b"\r" in raw.replace(b"\r\n", b""):
        raise ValueError("Unsupported mixed/CR-only line endings")
    if b"\r\n" in raw and b"\n" in raw.replace(b"\r\n", b""):
        raise ValueError("Mixed LF and CRLF line endings")
    return raw, commit, path


def function_span(raw, row, *, expand_truncated_end=False, max_body_lines=0,
                  include_signature=False):
    text = raw.decode("utf-8-sig")
    tree = ast.parse(text)
    wanted = row.get("symbol_qualified_name")
    found = []

    def visit(node, parents):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                names = parents + [child.name]
                if ".".join(names) == wanted and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    found.append(child)
                visit(child, names)
            else:
                visit(child, parents)

    visit(tree, [])
    if len(found) != 1:
        raise ValueError("Unsupported or ambiguous function symbol")
    node = found[0]
    earliest = min([node.lineno] + [d.lineno for d in node.decorator_list])
    end_matches = row.get("end_line") == node.end_lineno
    expanded = (expand_truncated_end and type(row.get("end_line")) is int and
                node.lineno <= row["end_line"] < node.end_lineno)
    if row.get("start_line") not in (earliest, node.lineno) or not (end_matches or expanded):
        raise ValueError("Localization coordinates disagree with AST")
    body_start_line = node.body[0].lineno
    lines = text.splitlines(keepends=True)
    # Reject inline suites (def f(): return ...) and shared physical lines.
    if body_start_line == node.lineno or lines[body_start_line - 1][:node.body[0].col_offset].strip():
        raise ValueError("Inline function bodies are unsupported in v1")
    # A body far larger than the model's usable generation budget is rejected before
    # any model call, so the run fails cheaply and visibly instead of producing a
    # truncated body that only fails later at the syntax gate.
    body_lines = node.end_lineno - body_start_line + 1
    if max_body_lines and body_lines > max_body_lines:
        raise ValueError(
            f"Body spans {body_lines} lines, above max_body_lines={max_body_lines}")
    start_line = earliest if include_signature else body_start_line
    start = sum(len(line) for line in lines[:start_line - 1])
    end = sum(len(line) for line in lines[:node.end_lineno])
    prefix, middle, suffix = text[:start], text[start:end], text[end:]
    return {"prefix": prefix, "original_middle": middle, "suffix": suffix,
            "start": start, "end": end, "line_start": start_line,
            "line_end": node.end_lineno, "symbol": wanted,
            "body_lines": body_lines,
            "include_signature": bool(include_signature),
            "coordinates_expanded": bool(expanded),
            "localization_end_line": row.get("end_line"),
            "bom": raw.startswith(b"\xef\xbb\xbf"),
            "newline": "\r\n" if b"\r\n" in raw else "\n"}


PUBLIC_REQUIREMENT_KEYS = (
    "title", "description", "problem_statement", "expected_behavior",
    "actual_behavior", "error_message", "steps_to_reproduce",
)
REWRITE_PUBLIC_REQUIREMENT_KEYS = PUBLIC_REQUIREMENT_KEYS + ("bug_report",)


def evidence(ticket, original_body=None, *, max_requirement_chars=0,
             requirement_keys=PUBLIC_REQUIREMENT_KEYS):
    """Build the commented requirement block that precedes the code.

    ``original_body`` is the implementation being replaced. Including it turns
    the task from "write this function" into "fix this function", which is what
    is actually being asked. Without it the model has never seen the code it is
    replacing: research-3h produced three patches that deleted a 51-line and a
    62-line body and called methods that do not exist (``WriterClass``,
    ``_report_hdu_count``) -- confident guesses at what the function probably
    contained. It is passed as comments in the prefix rather than as the middle,
    so the model still generates the body from scratch and the reconstruction
    path is untouched.

    The requirement fields stay an allowlist: gold patches and hidden tests can
    never enter the prompt, whatever this argument holds.
    """

    rows = []
    for key in requirement_keys:
        value = ticket.get(key)
        if value:
            value = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            rows.extend(f"# {line}" for line in value.splitlines())
    if not rows:
        raise ValueError("No public repair requirements provided")
    if max_requirement_chars:
        kept = []
        used = 0
        for row in rows:
            cost = len(row) + 1
            if kept and used + cost > max_requirement_chars:
                kept.append("# [public issue text truncated to fit local repair context]")
                break
            kept.append(row[:max_requirement_chars] if not kept else row)
            used += min(cost, max_requirement_chars)
            if used >= max_requirement_chars:
                break
        rows = kept
    block = "# Complete the function according to these requirements:\n" + "\n".join(rows) + "\n"
    if original_body:
        body = original_body.replace("\r\n", "\n").rstrip("\n")
        if any(marker in body for marker in ("<PRE>", "<SUF>", "<MID>", "<EOT>")):
            raise ValueError("FIM marker collision in source/evidence")
        block += ("# BEFORE (buggy):\n"
                  + "\n".join(f"# {line}" for line in body.splitlines())
                  + "\n# Treat this implementation as the baseline: preserve its working behavior and structure.\n"
                  + "# Change only the smallest set of executable lines needed for the reported issue.\n")
    return block


# GitHub issue templates leave their instructions behind as HTML comments, and
# SWE-bench keeps the raw body. On 13073 and 13236 the first fifteen lines of
# the prompt were template boilerplate: the model's scarcest resource spent on
# text that says nothing about the bug.
TEMPLATE_COMMENT = re.compile(r"<!--.*?-->", re.S)
# Two or more hashes, deliberately. GitHub issue templates emit "### Expected
# behavior"; a single "#" at the start of a line is far more often a shell
# comment inside a reproduction block -- "# cat test.qdp" was being read as a
# section heading, which filed the entire reproduction under a new section and
# emptied the one the prompt labels "Input / how to reproduce".
MARKDOWN_HEADING = re.compile(r"^\s{0,3}#{2,6}\s*(?P<title>.+?)\s*#*\s*$")
CODE_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
BOLD_HEADING = re.compile(r"^\s{0,3}\*\*(?P<title>[^*]+?)\*\*\s*:?\s*$")
ENVIRONMENT_TITLE = re.compile(
    r"^(versions?|version info|environment|system details|additional context"
    r"|additional information|checklist)\b", re.I)
EXPECTED_TITLE = re.compile(r"^(expected|desired|should)\b", re.I)
REPRODUCE_TITLE = re.compile(
    r"^(how to reproduce|steps to reproduce|to reproduce|reproduction"
    r"|reproducing|minimal example|example)\b", re.I)
ACTUAL_TITLE = re.compile(
    r"^(actual|observed|current behaviou?r|error|traceback|what happens)\b", re.I)
BLANK_RUN = re.compile(r"\n{3,}")


def _heading_title(line):
    """The section title this line announces, or None if it announces none."""

    for pattern in (MARKDOWN_HEADING, BOLD_HEADING):
        match = pattern.match(line)
        if match:
            title = match.group("title").strip()
            if title:
                return title
    return None


def _headings(lines):
    """Yield ``(line, title_or_None)``, with fenced code never a heading.

    A reproduction block is the one place an issue is certain to contain lines
    that look like markup but are code.
    """

    fenced = False
    for line in lines:
        if CODE_FENCE.match(line):
            fenced = not fenced
            yield line, None
            continue
        yield line, (None if fenced else _heading_title(line))


def clean_issue_text(text):
    """Drop what the issue carries for humans and bug trackers, not for a fixer.

    Removed: template HTML comments, and any section whose heading names the
    reporter's environment rather than the defect. Nothing is paraphrased and
    no new text is introduced -- this only deletes.
    """

    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    # Template comments outside code are noise; inside reproductions they may
    # be literal input (for example an HTML parser regression).
    chunks, prose, fenced = [], [], False
    for line in text.split("\n"):
        if CODE_FENCE.match(line):
            if prose:
                chunks.append(TEMPLATE_COMMENT.sub("", "\n".join(prose)))
                prose = []
            chunks.append(line)
            fenced = not fenced
        elif fenced:
            chunks.append(line)
        else:
            prose.append(line)
    if prose:
        chunks.append(TEMPLATE_COMMENT.sub("", "\n".join(prose)))
    text = "\n".join(chunks)
    kept, dropping = [], False
    for line, title in _headings(text.split("\n")):
        if title is not None:
            dropping = bool(ENVIRONMENT_TITLE.match(title))
        if not dropping:
            kept.append(line)
    return BLANK_RUN.sub("\n\n", "\n".join(kept)).strip("\n")


def issue_sections(text):
    """Split cleaned issue text into the three fields a repair actually needs.

    Returns ``{"reproduce": ..., "expected": ..., "actual": ..., "rest": ...}``.
    A field is "" when the issue never stated it -- which is the common case and
    worth seeing in the prompt as an absence rather than as prose to mine. The
    five development tickets split 1 complete / 2 partial / 2 with no expected
    behaviour at all, so this is the field that most often comes back empty.
    """

    buckets = {"reproduce": [], "expected": [], "actual": [], "rest": []}
    current = "rest"
    for line, title in _headings((text or "").split("\n")):
        if title is not None:
            if EXPECTED_TITLE.match(title):
                current = "expected"
                continue
            if REPRODUCE_TITLE.match(title):
                current = "reproduce"
                continue
            if ACTUAL_TITLE.match(title):
                current = "actual"
                continue
            current = "rest"
        buckets[current].append(line)
    return {key: BLANK_RUN.sub("\n\n", "\n".join(lines)).strip("\n")
            for key, lines in buckets.items()}


TRIPLE_QUOTES = (chr(34) * 3, chr(39) * 3)
NUMPYDOC_SECTION = re.compile(
    r"^(parameters|returns|yields|raises|examples|notes|see also|attributes"
    r"|references|other parameters|warns)\s*$", re.I)


def compact_docstring(body, max_lines=3):
    """Keep a docstring's summary and drop its parameter table.

    ``_read_table_qdp`` spends 26 of its 40 body lines on a numpydoc block of
    Parameters/Returns/Examples. Those lines are inside the prompt's scarcest
    region -- the commented copy of the function being repaired -- and none of
    them describe the defect. The summary is kept because it states what the
    function is for.
    """

    lines = (body or "").replace("\r\n", "\n").split("\n")
    start = next((i for i, line in enumerate(lines) if line.strip()), None)
    if start is None:
        return body
    opener = lines[start].strip()
    quote = next((q for q in TRIPLE_QUOTES if opener.startswith(q)), None)
    if quote is None:
        return body
    # A one-line docstring is already compact.
    if opener.count(quote) >= 2 and len(opener) > len(quote):
        return body
    end = next((i for i in range(start + 1, len(lines)) if quote in lines[i]), None)
    if end is None:
        return body
    kept_last = min(start + max_lines - 1, end - 1)
    # Do not stop on a blank line or on an orphaned numpydoc section heading --
    # "Parameters" with its table removed is noise, not a summary.
    while kept_last > start:
        stripped = lines[kept_last].strip()
        if stripped and not NUMPYDOC_SECTION.match(stripped) and set(stripped) != {"-"}:
            break
        kept_last -= 1
    omitted = end - 1 - kept_last
    if omitted <= 0:
        return body
    indent = lines[start][:len(lines[start]) - len(lines[start].lstrip())]
    placeholder = (f"{indent}[... {omitted} further docstring line"
                   f"{'' if omitted == 1 else 's'} omitted ...]")
    rebuilt = (lines[:kept_last + 1] + [placeholder] + lines[end:])
    return "\n".join(rebuilt)


ISSUE_BRIEF_HEADER = "# ===== ISSUE REPORT (public text only) ====="
# Lowest value first: what gets cut when the budget binds. "expected" and
# "actual" are last because they are the two the repair is a function of, and
# because evidence()'s field-order truncation used to cut "expected" first
# simply by virtue of where it sat in the ticket JSON.
BRIEF_DROP_ORDER = ("rest", "reproduce", "actual", "expected")
BRIEF_LABELS = {
    "reproduce": "--- Reproduce ---",
    "expected": "--- Expected behaviour ---",
    "actual": "--- Actual behaviour / error ---",
    "rest": "--- Other details ---",
}


def issue_brief(ticket, *, max_chars=0, requirement_keys=PUBLIC_REQUIREMENT_KEYS, infer_expected=False):
    """The public issue text, cleaned and labelled by what each part is for.

    Same allowlist as ``evidence``: gold patches and hidden tests cannot enter
    the prompt through here, and nothing is added that the reporter did not
    write. The change is organisation, not content -- the fields a fixer needs
    are named and put first instead of being buried in template boilerplate.
    """

    title = ""
    parts = []
    structured = {"steps_to_reproduce": "reproduce", "expected_behavior": "expected",
                  "actual_behavior": "actual", "error_message": "actual"}
    explicit = {key: [] for key in ("reproduce", "expected", "actual", "rest")}
    for key in requirement_keys:
        value = ticket.get(key)
        if not value:
            continue
        value = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if key == "title" and not title:
            title = value.strip().splitlines()[0] if value.strip() else ""
            continue
        # SWE-bench rows repeat one body across several fields -- on 14365
        # ``description`` and ``bug_report`` are the same 1339 characters. Keep
        # the longest copy of any text and drop the rest.
        text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
        if key in structured:
            # Keep JSON field semantics even when its value has no Markdown heading.
            bucket = explicit[structured[key]]
            if text and text not in bucket:
                bucket.append(text)
            continue
        if not text or any(text in kept for kept in parts):
            continue
        parts = [kept for kept in parts if kept not in text]
        parts.append(text)
    merged = clean_issue_text("\n\n".join(parts))
    if title:
        # The body usually opens by repeating the title verbatim.
        merged = BLANK_RUN.sub("\n\n", "\n".join(
            line for line in merged.split("\n") if line.strip() != title)).strip("\n")
    sections = issue_sections(merged)
    for key, values in explicit.items():
        for value in values:
            # Exact duplicates only. Never shorten code, asserts or tracebacks.
            if value in sections[key]:
                continue
            if sections["rest"].strip() == value:
                sections["rest"] = ""
            sections[key] = "\n\n".join(filter(None, (sections[key], value)))
    # Description headings are redundant once the block has a Title/Other label.
    sections["rest"] = "\n".join(
        line for line, heading in _headings(sections["rest"].split("\n"))
        if not (heading and heading.casefold() in ("description", "bug description", "describe the bug"))
    ).strip("\n")
    if title and not any(sections.values()):
        sections["rest"] = ""
    if not title and not any(sections.values()):
        raise ValueError("No public repair requirements provided")

    if infer_expected and not sections['expected']:
        from .public_behavior import expectation_evidence, evidence_text
        allowed_ticket = {key:ticket[key] for key in requirement_keys if key in ticket}
        sections['expected'] = '\n'.join(evidence_text(expectation_evidence(allowed_ticket)))
    had_expected = bool(sections['expected'])

    def render(current):
        lines = [ISSUE_BRIEF_HEADER]
        if title:
            lines.append(f"# Title: {title}")
        for key in ("reproduce", "expected", "actual", "rest"):
            text = current.get(key)
            if not text:
                # Expected and Actual are named even when the report omits them.
                # An unstated requirement is a fact about the ticket worth
                # showing: 4 of the 5 development tickets omit one of the two,
                # and the directive refers to both by these labels.
                if key in ("expected", "actual"):
                    missing = ('Not retained within context budget.' if infer_expected and key == 'expected' and had_expected else 'Not stated.')
                    lines.append(f"# {BRIEF_LABELS[key]} {missing}")
                continue
            lines.append(f"# {BRIEF_LABELS[key]}")
            lines.extend(f"# {line}" for line in text.split("\n"))
        return "\n".join(lines) + "\n"

    block = render(sections)
    if not max_chars:
        return block
    for key in BRIEF_DROP_ORDER:
        if len(block) <= max_chars:
            break
        if not sections.get(key):
            continue
        kept = sections[key].split("\n")
        while kept and len(block) > max_chars:
            kept = kept[:max(1, len(kept) * 3 // 4)]
            if len(kept) == 1:
                sections[key] = ""
                break
            sections[key] = "\n".join(kept) + "\n[truncated to fit the context budget]"
            block = render(sections)
        else:
            block = render(sections)
            continue
        block = render(sections)
    return block


def current_implementation_block(body, indent, symbol, *, docstring_lines=3):
    """The code being repaired, commented, directly above the generation point.

    ``evidence`` used to put this 100+ lines earlier, ahead of the whole source
    prefix. Here it sits where the model is about to write, at the body's own
    indentation, so the last thing it reads before generating is the code it is
    replacing rather than an unrelated neighbouring function.
    """

    text = (body or "").replace("\r\n", "\n").rstrip("\n")
    if not text:
        return ""
    if any(marker in text for marker in ("<PRE>", "<SUF>", "<MID>", "<EOT>")):
        raise ValueError("FIM marker collision in source/evidence")
    text = compact_docstring(text, max_lines=docstring_lines)
    lines = [f"{indent}# ===== CURRENT IMPLEMENTATION OF {symbol} (buggy) ====="]
    for line in text.split("\n"):
        stripped = line[len(indent):] if line.startswith(indent) else line.lstrip()
        lines.append(f"{indent}# {stripped}".rstrip())
    return "\n".join(lines) + "\n"


def task_block(directive, indent, symbol, *, replacement_scope="function_body",
               include_signature=False):
    """The instruction, rendered last, immediately before the generation point.

    This is the whole point of the rewrite. In the previous layout the directive
    sat at prompt line 91 and 110 lines of ordinary source code followed it, so
    the most probable continuation at the generation point was ordinary source
    code -- which is what the model produced: 42.7% of candidates were copies of
    the original body, and 0 of 32 probe candidates ever acted on the directive.
    Recency is the only lever a base (non-instruct) model gives us.
    """

    what = ("the one replacement statement or branch"
            if replacement_scope == "issue_statement" else
            (f"the complete corrected function {symbol}, including its signature"
             if include_signature else f"the corrected body of {symbol}"))
    lines = [f"{indent}# ===== WHAT TO WRITE BELOW ====="]
    for line in (directive or "").replace("\r\n", "\n").split("\n"):
        if not line.strip():
            continue
        text = line.lstrip()
        text = text[1:].strip() if text.startswith("#") else text
        lines.append(f"{indent}# {text}".rstrip())
    lines.append(f"{indent}# Write {what} on the next line and nothing else.")
    return "\n".join(lines) + "\n"


PROMPT_LAYOUTS = ("task_last", "legacy", "compact_fim")


def fim_prompt_version(*, generation_scope="function_body",
                       include_original_body=False, prompt_layout="task_last"):
    """One place both the generator and the batch runner read the label from.

    They used to carry the same three-way conditional separately, so a new
    layout could be recorded in a run's config and not in its records.
    """

    if prompt_layout == "compact_fim":
        return "compact-fim-v1-" + generation_scope + ("-original" if include_original_body and generation_scope != "issue_statement" else "")
    if prompt_layout == "task_last":
        if generation_scope == "issue_statement":
            return "issue-brief-local-statement-task-last-psm-v7-scope-contract-v1-issue-compact-v1"
        if include_original_body:
            return "issue-brief-before-task-last-psm-v7-scope-contract-v1-issue-compact-v1"
        return "issue-brief-task-last-psm-v7-scope-contract-v1-issue-compact-v1"
    if generation_scope == "issue_statement":
        return "issue-plus-local-statement-psm-v6-scope-contract-v1"
    if include_original_body:
        return "issue-plus-before-after-localization-psm-v5-scope-contract-v1"
    return "issue-plus-localization-psm-v3-scope-contract-v1"


def localization_guidance(rows, selected_rank):
    """Expose bounded, non-gold symbol evidence to the repair model.

    Stage 3 may deliberately reserve caller/callee neighbors.  Previously
    those rows were recorded only after generation, so the model could not use
    them to recognize that a symptom in one method was implemented by a helper
    in another file.  Keep this block to public localization identities; source
    text, hidden tests, and reference patches are never copied here.
    """

    lines = ["# Fault localization ranked these related symbols:"]
    for rank, row in enumerate(rows[:5], 1):
        if not isinstance(row, dict):
            continue
        path = row.get("file_path")
        symbol = row.get("symbol_qualified_name")
        if not path or not symbol:
            continue
        marker = " [selected repair target]" if rank == selected_rank else ""
        lines.append(f"# {rank}. {path}::{symbol}{marker}")
    # Under strict_top1 this block lists exactly one symbol, and the delegation
    # sentence then asked the model to compare that symbol against an empty set.
    if len(lines) > 2:
        lines.append("# Check whether the selected symbol delegates the faulty behavior to one of these callers/helpers.")
    return "\n".join(lines) + "\n"


REWRITE_PROMPT_VERSION = "issue-plus-full-function-rewrite-v2"
REWRITE_REQUIREMENT_CHAR_LIMIT = 4000


def rewrite_prompt(ticket_json, span, localization_rows, selected_rank, *,
                   max_requirement_chars=REWRITE_REQUIREMENT_CHAR_LIMIT):
    """Build a non-FIM prompt for one complete-function rewrite.

    Requirements retain ``evidence()``'s public-field allowlist, localization
    retains ``localization_guidance()``'s path/symbol-only projection, and the
    source slice must contain the complete function signature.  The response
    contract is stated here but enforced by the host-side parser in Phase 3.
    """

    if not isinstance(ticket_json, dict) or not isinstance(span, dict):
        raise ValueError("Rewrite prompt requires ticket and span objects")
    if not isinstance(localization_rows, list):
        raise ValueError("Rewrite prompt requires localization rows")
    if type(selected_rank) is not int or not 1 <= selected_rank <= 5:
        raise ValueError("Rewrite selected rank must be between 1 and 5")
    if not span.get("include_signature"):
        raise ValueError("Rewrite prompt requires a complete function span")
    function_text = span.get("original_middle")
    if not isinstance(function_text, str) or not function_text.strip():
        raise ValueError("Rewrite prompt requires complete function source")
    function_text = function_text.replace("\r\n", "\n")
    forbidden_markers = ("<PRE>", "<SUF>", "<MID>", "<EOT>",
                         "▁<PRE>", "▁<SUF>", "▁<MID>", "▁<EOT>")
    if any(marker in function_text for marker in forbidden_markers):
        raise ValueError("FIM marker collision in rewrite source")
    requirements = evidence(
        ticket_json, max_requirement_chars=max_requirement_chars,
        requirement_keys=REWRITE_PUBLIC_REQUIREMENT_KEYS)
    guidance = localization_guidance(localization_rows, selected_rank)
    return (
        f"# Rewrite protocol: {REWRITE_PROMPT_VERSION}\n"
        + requirements
        + guidance
        + "# Repair the complete Python function shown below.\n"
        + "# Preserve its working behavior and change only the smallest set of executable lines needed for the issue.\n"
        + "# Preserve the function name, def/async kind, decorators, parameters, defaults, and return annotation exactly.\n"
        + "# Return exactly one complete Python function and nothing else.\n"
        + "# Do not return Markdown, analysis, a diff, module statements, classes, or sibling functions.\n"
        + "# If this function should not be changed for the issue, return exactly NO_CHANGE.\n"
        + "# Original function:\n"
        + function_text.rstrip("\n")
        + "\n# End of original function.\n"
        + "# The original function above is input context, not part of your response.\n"
        + "# Do not emit a BEFORE/AFTER pair and do not repeat the buggy function first.\n"
        + "# Begin your response immediately with the one final corrected def/async def "
          "(including its decorators), or exactly NO_CHANGE.\n"
    )


def _complete_rewrite_function(text, *, source):
    """Parse exactly one complete standalone function from rewrite text."""

    normalized = text.replace("\r\n", "\n")
    if "\r" in normalized:
        raise ValueError(f"rewrite_contract_violation: {source}_carriage_return")
    normalized = textwrap.dedent(normalized).strip()
    if not normalized:
        raise ValueError(f"rewrite_contract_violation: empty_{source}")
    if "```" in normalized:
        raise ValueError("rewrite_contract_violation: markdown_fence")
    if any(line.startswith(("diff --git ", "--- ", "+++ ", "@@ "))
           for line in normalized.splitlines()):
        raise ValueError("rewrite_contract_violation: diff_output")
    normalized += "\n"
    try:
        tree = ast.parse(normalized, type_comments=True)
    except SyntaxError:
        raise ValueError("rewrite_contract_violation: invalid_python") from None
    if len(tree.body) != 1:
        if sum(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               for node in tree.body) > 1:
            raise ValueError("multiple_functions")
        raise ValueError("rewrite_contract_violation: expected_one_complete_function")
    node = tree.body[0]
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        raise ValueError("rewrite_contract_violation: expected_one_complete_function")
    earliest = min([node.lineno] + [item.lineno for item in node.decorator_list])
    lines = normalized.splitlines(keepends=True)
    outside = "".join(lines[:earliest - 1] + lines[node.end_lineno:])
    if outside.strip():
        raise ValueError("rewrite_contract_violation: extra_text")
    return normalized, node


def _rewrite_signature(node):
    """Return the body-independent function contract enforced by rewrite v1."""

    return {
        "name": node.name,
        "kind": type(node).__name__,
        "decorators": [ast.dump(item, include_attributes=False)
                       for item in node.decorator_list],
        "arguments": ast.dump(node.args, include_attributes=False),
        "returns": (ast.dump(node.returns, include_attributes=False)
                    if node.returns is not None else None),
        "type_comment": node.type_comment,
        "type_params": [ast.dump(item, include_attributes=False)
                        for item in getattr(node, "type_params", [])],
    }


def _function_suite_start_line(text):
    """Find the first physical line after the complete ``def`` header."""

    saw_def = False
    depth = 0
    try:
        tokens = tokenize.generate_tokens(iter(text.splitlines(keepends=True)).__next__)
        for token in tokens:
            if token.type == tokenize.NAME and token.string == "def":
                saw_def = True
                continue
            if not saw_def or token.type != tokenize.OP:
                continue
            if token.string in "([{":
                depth += 1
            elif token.string in ")]}":
                depth -= 1
            elif token.string == ":" and depth == 0:
                return token.end[0] + 1
    except (IndentationError, tokenize.TokenError):
        pass
    raise ValueError("rewrite_contract_violation: incomplete_function_header")


def parse_rewrite_response(response, original_span):
    """Validate a complete-function answer and return its reindented body.

    ``NO_CHANGE`` is accepted only as the entire trimmed response.  Every code
    answer is parsed independently, locked to the original signature contract,
    and reduced to a body that can enter the existing host reconstruction.
    """

    if not isinstance(response, str):
        raise ValueError("rewrite_contract_violation: response_not_text")
    if response.strip() == "NO_CHANGE":
        return {"status": "no_change", "reason": "rewrite_no_change", "middle": None}
    if not isinstance(original_span, dict) or not original_span.get("include_signature"):
        raise ValueError("rewrite_contract_violation: complete_original_span_required")
    original_source = original_span.get("original_middle")
    if not isinstance(original_source, str) or not original_source.strip():
        raise ValueError("rewrite_contract_violation: complete_original_source_required")
    _, original_node = _complete_rewrite_function(
        original_source, source="original")
    candidate_text, candidate_node = _complete_rewrite_function(
        response, source="response")
    original_signature = _rewrite_signature(original_node)
    candidate_signature = _rewrite_signature(candidate_node)
    for field, expected in original_signature.items():
        if candidate_signature[field] != expected:
            raise ValueError(f"signature_changed: {field}")
    suite_start = _function_suite_start_line(candidate_text)
    if suite_start > candidate_node.body[0].lineno:
        raise ValueError("rewrite_contract_violation: inline_function_body")
    candidate_lines = candidate_text.splitlines(keepends=True)
    body = "".join(candidate_lines[suite_start - 1:candidate_node.end_lineno])
    body = textwrap.dedent(body)
    if not body.strip():
        raise ValueError("rewrite_contract_violation: empty_function_body")
    # AST parsing dedents a nested method into a standalone function. Recover
    # the actual repository indentation from the untouched source slice.
    original_lines = original_source.replace("\r\n", "\n").splitlines(keepends=True)
    original_body_line = original_lines[original_node.body[0].lineno - 1]
    target_indent = original_body_line[:len(original_body_line) - len(original_body_line.lstrip())]
    middle = textwrap.indent(body, target_indent, lambda line: bool(line.strip()))
    if not middle.endswith("\n"):
        middle += "\n"
    return {"status": "parsed", "reason": None, "middle": middle,
            "function_kind": type(candidate_node).__name__}


def repair_directive(ticket_json, selected_row, *, replacement_scope="function_body",
                     explicit_argument_edit=False, include_signature=False, domain_adapters=()):
    """Derive bounded repair instructions from public issue text and source."""

    public_issue = " ".join(
        str(ticket_json.get(key) or "")
        for key in ("title", "summary", "body", "description", "bug_report")
    )
    public_text = public_issue.casefold()
    source = str(selected_row.get("code_text") or "")
    # Four lines, not five, and each names something the issue brief above
    # labelled. The previous wording ("identify the actual behavior and the
    # required behavior from the issue") asked the model to find two fields that
    # the prompt had not marked; issue_brief marks them, so the instruction can
    # point at them instead of describing them.
    lines = [
        "# TASK: make Expected behaviour happen instead of Actual behaviour, as stated in the issue above.",
        "# Make an executable code change; returning the current implementation unchanged is invalid.",
        "# Change only the lines the issue requires; keep every other branch, call, check and return value.",
        ("# Output only the one replacement statement or branch -- no prose, no Markdown, "
         "no sibling statements, no incomplete fragment."
         if replacement_scope == "issue_statement" else
         "# Output only the complete replacement function, including its signature "
         "and body -- no prose, no Markdown, no sibling functions."
         if include_signature else
         "# Output only the complete replacement function body -- no prose, no Markdown, "
         "no signature line, no sibling functions."),
    ]
    if explicit_argument_edit:
        match = re.search(
            r"\b(first|second|third|fourth|fifth)\s+argument\s+of\s+['\"`]?"
            r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)['\"`]?\s+should\s+be\s+"
            r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*,?\s+rather\s+than\s+"
            r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)",
            public_issue,
            flags=re.IGNORECASE,
        )
        if match:
            ordinal, call, replacement, original = match.groups()
            if original != replacement and re.search(rf"\b{re.escape(call)}\s*\(", source):
                position = {"first": 1, "second": 2, "third": 3,
                            "fourth": 4, "fifth": 5}[ordinal.casefold()]
                lines.append(
                    f"# Issue-stated argument edit: in each {call}(...) call whose argument "
                    f"{position} is {original}, replace only that argument with {replacement}."
                )
    from .domain_adapters import directives,validate_adapters
    lines.extend(directives(validate_adapters(domain_adapters),ticket_json,source))
    return "\n".join(lines) + "\n"


# Measured on this corpus: a 7.5 KB prompt tokenised to 2222 tokens, i.e. a
# little over three bytes per token. A rewritten body can be materially longer
# than the one it replaces, so the estimate is tripled before it becomes a cap.
BYTES_PER_TOKEN = 3.4
BODY_BUDGET_FACTOR = 3
MIN_BODY_TOKENS = 128
LARGE_FUNCTION_STRATEGIES = ("full_body", "issue_statement")
LARGE_FUNCTION_LINE_THRESHOLD = 80
ISSUE_STATEMENT_MAX_LINES = 40
LOCAL_REQUIREMENT_CHAR_LIMIT = 4000
GENERATION_MODES = ("fim", "rewrite")
REWRITE_UNSUPPORTED_TARGET_POLICIES = (
    "called_helper_multisymbol", "related_targets_top5",
)


def issue_statement_span(raw, function, ticket_json, *, max_statement_lines=ISSUE_STATEMENT_MAX_LINES):
    """Narrow a large function to one issue-relevant top-level statement.

    Selection uses only public issue words and source identifiers. The full
    function remains in the reconstruction; only the selected statement is
    sent through FIM, so unrelated branches cannot disappear from the patch.
    """

    if function.get("include_signature"):
        raise ValueError("issue_statement does not support whole-function signature repair")
    text = raw.decode("utf-8-sig")
    node = _target_function_node(text, function["symbol"])
    title_text = str(ticket_json.get("title") or "").casefold()
    description_text = " ".join(
        str(ticket_json.get(key) or "")
        for key in ("description", "problem_statement", "expected_behavior",
                    "actual_behavior", "error_message", "steps_to_reproduce")
    ).casefold()
    public_text = title_text + " " + description_text
    primary_text = title_text + " " + description_text[:3000]
    issue_terms = {
        term for term in re.findall(r"[a-z_][a-z0-9_]{3,}", public_text)
        if term not in {"that", "this", "with", "from", "have", "will", "into",
                        "should", "would", "could", "there", "their", "when"}
    }
    lines = text.splitlines(keepends=True)
    candidates = []
    for statement in node.body:
        if (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)):
            continue
        line_count = statement.end_lineno - statement.lineno + 1
        if line_count > max_statement_lines:
            continue
        identifiers = set()
        for child in ast.walk(statement):
            if isinstance(child, ast.Name):
                identifiers.add(child.id.casefold())
            elif isinstance(child, ast.Attribute):
                identifiers.add(child.attr.casefold())
            elif (isinstance(child, ast.Constant) and isinstance(child.value, str)
                  and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", child.value)):
                identifiers.add(child.value.casefold())
        overlap = issue_terms & identifiers
        if not overlap:
            continue
        relevance = sum(5 * primary_text.count(term) + public_text.count(term)
                        for term in overlap)
        earliest_issue_reference = min(public_text.find(term) for term in overlap)
        title_match = any(term in title_text for term in overlap)
        score = (int(title_match), -earliest_issue_reference, relevance, len(overlap),
                 -line_count, -statement.lineno)
        candidates.append((score, statement, sorted(overlap)))
    if not candidates:
        raise ValueError("issue_statement_unavailable: no bounded statement matches public issue terms")
    _, statement, overlap = max(candidates, key=lambda item: item[0])
    start = sum(len(line) for line in lines[:statement.lineno - 1])
    end = sum(len(line) for line in lines[:statement.end_lineno])
    narrowed = dict(function)
    narrowed.update(
        prefix=text[:start], original_middle=text[start:end], suffix=text[end:],
        start=start, end=end, line_start=statement.lineno,
        line_end=statement.end_lineno,
        body_lines=statement.end_lineno - statement.lineno + 1,
        function_body_lines=function["body_lines"],
        generation_scope="issue_statement",
        statement_kind=type(statement).__name__,
        statement_issue_terms=overlap,
    )
    return narrowed


def _parsed_generated_statements(text):
    normalized = textwrap.dedent(text.replace("\r\n", "\n"))
    if not normalized.strip():
        raise ValueError("incomplete_statement: generation produced no code")
    try:
        return ast.parse(normalized).body
    except SyntaxError as exc:
        raise ValueError("incomplete_statement: generated statement does not parse") from exc


def check_single_statement_replacement(text):
    """Keep issue_statement generation inside its selected AST boundary."""

    statements = _parsed_generated_statements(text)
    if len(statements) != 1:
        raise ValueError("statement_scope_escape: expected exactly one replacement statement")


def trim_generated_statement(text):
    """Keep the first complete statement when a model runs past the span."""

    normalized = textwrap.dedent(text.replace("\r\n", "\n"))
    statements = _parsed_generated_statements(text)
    first = statements[0]
    lines = normalized.splitlines(keepends=True)
    kept = "".join(lines[:first.end_lineno])
    indent = min(
        len(line) - len(line.lstrip())
        for line in text.replace("\r\n", "\n").splitlines()
        if line.strip()
    )
    rebuilt = textwrap.indent(kept, " " * indent)
    return rebuilt if rebuilt.endswith("\n") else rebuilt + "\n"


def body_token_budget(span, ceiling):
    """Size one generation to the body being replaced, not to a flat maximum.

    research-3f spent 2048 tokens (and two of four candidates' whole 180 s
    timeout) regenerating a seven-line body, because the cap was a single
    number for every target. Sizing it per target is what makes hitting the cap
    an ordinary event rather than a run-ending one.
    """

    if "fixed_output_budget" in span:
        return min(ceiling, span["fixed_output_budget"])
    estimate = int(len(span["original_middle"].encode("utf-8")) / BYTES_PER_TOKEN
                   * BODY_BUDGET_FACTOR)
    return max(MIN_BODY_TOKENS, min(ceiling, estimate))


def body_indent_of(span):
    """The indentation column the replaced body's own statements start at."""

    original = span["original_middle"].replace("\r\n", "\n")
    indents = [len(line) - len(line.lstrip())
               for line in original.splitlines() if line.strip()]
    if not indents:
        raise ValueError("incomplete_body: original body has no statement to match")
    return min(indents)


def body_overruns(text, span):
    """Did the generation carry on past the end of the body it was replacing?

    True when some non-blank line dedents out of the body -- the sibling
    function or method that follows the target in the file.

    This is asked even of a generation the model closed itself. A visible
    ``<EOT>`` says the model decided it was finished, not that it finished in
    the right place: in runs/multitarget-A four candidates on correctly
    localized functions emitted ``<EOT>`` only after writing the *next*
    function (``_line_type`` ran 105 lines and carried ``def
    _get_type_from_file``; ``diff_values`` carried ``def where_not_allclose``).
    Trusting the marker meant no trim, and the whole overrun was spliced in and
    then rejected by the scope gate.
    """

    try:
        body_indent = body_indent_of(span)
    except ValueError:
        return False
    return any(line.strip() and len(line) - len(line.lstrip()) < body_indent
               for line in text.replace("\r\n", "\n").splitlines())


def trim_generated_body(text, span):
    """Cut a run-on generation back at the body's own structural end.

    The model keeps writing past the target: correct body first, then the
    sibling methods that follow it in the file. Python says exactly where the
    body stopped -- at the first non-blank line that dedents out of it -- so
    the cut is the language's answer, not a guess about the model.

    Seeing that dedent is required. Without it the generation ran out of budget
    still inside the body, and nothing here can tell a finished body from a
    half-written one, so the candidate is refused rather than silently
    truncated into something that parses.

    A dedent inside a multi-line string would cut in the wrong place. That
    candidate then fails ``ast.parse`` in build_patch and is rejected, which is
    the intended direction to fail in.
    """

    body_indent = body_indent_of(span)
    kept, ended = [], False
    for line in text.replace("\r\n", "\n").splitlines(keepends=True):
        if line.strip() and len(line) - len(line.lstrip()) < body_indent:
            ended = True
            break
        kept.append(line)
    if not ended:
        raise ValueError("incomplete_body: generation never dedented out of the body")
    while kept and not kept[-1].strip():
        kept.pop()
    if not kept:
        raise ValueError("incomplete_body: generation produced no body lines")
    body = "".join(kept)
    return body if body.endswith("\n") else body + "\n"


DEFAULT_MIN_BODY_RETENTION = 0.5
SHRINK_CHECK_MIN_LINES = 10


def check_body_retention(middle, span, ratio):
    """Refuse a body that dropped most of the code it replaced.

    A fix edits behaviour; it rarely deletes nine tenths of a function. In
    research-3h a 62-line body came back as four lines calling two methods that
    do not exist, and passed every other gate. Line count is a crude signal and
    this is openly a heuristic -- a fix that removes a deprecated branch can
    legitimately halve a function -- so the threshold is configurable, the check
    is skipped entirely for bodies too short for the ratio to mean anything, and
    a rejection is recorded under its own reason rather than folded into others.
    """

    if not ratio:
        return
    def weight(text):
        source=textwrap.dedent(text.replace('\r\n','\n'))
        tree=ast.parse(source)
        docs=set()
        for node in ast.walk(tree):
            if isinstance(node,(ast.Module,ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)) and node.body:
                first=node.body[0]
                if isinstance(first,ast.Expr) and isinstance(first.value,ast.Constant) and isinstance(first.value.value,str):
                    docs.update(range(first.lineno,first.end_lineno+1))
        ignored={tokenize.COMMENT,tokenize.NL,tokenize.NEWLINE,tokenize.INDENT,tokenize.DEDENT,tokenize.ENDMARKER}
        return len({t.start[0] for t in tokenize.generate_tokens(io.StringIO(source).readline)
                    if t.type not in ignored and t.start[0] not in docs})
    before = weight(span["original_middle"])
    if before < SHRINK_CHECK_MIN_LINES:
        return
    after = weight(middle)
    if after < before * ratio:
        raise ValueError(
            f"body_shrank: {after} of {before} lines kept, below the "
            f"{ratio:.0%} retention floor")


def propagate_tolerance_keywords(raw, span, helper_symbol):
    # Compatibility export; this transformation belongs to the opt-in adapter.
    from .domain_adapters import propagate_tolerance_keywords as transform
    return transform(raw,span,helper_symbol)


def _without_docstrings(tree):
    """Remove docstrings so adding or deleting one cannot count as a repair."""

    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            first = body[0] if body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                del body[0]
                if not body and not isinstance(node, ast.Module):
                    body.append(ast.Pass())
    return tree


def check_executable_change(before_text, after_text):
    """Refuse a patch the interpreter cannot tell apart from the original.

    ``no_op`` compares bytes, so a candidate that only adds a comment or fixes a
    typo in a docstring passes it and is recorded as a generated patch. Both
    happened in research-3i: one patch added a two-line TODO and another
    corrected 'BaseReader' to 'BaseWriter' in a docstring, and two of three
    tickets were counted as generated on that basis.

    Comparing the two syntax trees with docstrings normalised away answers the
    only question that matters here -- does this change anything that runs? A
    fix whose FAIL_TO_PASS test must start passing always does.
    """

    before = ast.dump(_without_docstrings(ast.parse(before_text)), include_attributes=False)
    after = ast.dump(_without_docstrings(ast.parse(after_text)), include_attributes=False)
    if before == after:
        raise ValueError("no_executable_change: only comments or docstrings differ")


def _function_symbol_table(text, qualified_name):
    """Return the symbol table for one dotted function/class path."""

    table = symtable.symtable(text, "<generated-patch>", "exec")
    for part in qualified_name.split("."):
        matches = [child for child in table.get_children()
                   if child.get_name() == part]
        if not matches:
            raise ValueError(f"Symbol table is missing target: {qualified_name}")
        table = matches[0]
    return table


def _unresolved_global_references(text, qualified_name):
    """Find bare names referenced by the target that Python cannot resolve.

    The check is intentionally conservative. Existing unresolved names are
    compared away by ``check_new_undefined_names`` because repositories may
    populate globals dynamically. Attribute names such as ``obj.method`` are
    not symbols and are therefore outside this gate.
    """

    tree = ast.parse(text)
    if any(isinstance(node, ast.ImportFrom)
           and any(alias.name == "*" for alias in node.names)
           for node in tree.body):
        return set()
    module = symtable.symtable(text, "<generated-patch>", "exec")
    module_bindings = {
        symbol.get_name()
        for symbol in module.get_symbols()
        if symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace()
    }
    allowed = module_bindings | set(dir(builtins))
    target = _function_symbol_table(text, qualified_name)
    unresolved = set()

    def visit(table):
        for symbol in table.get_symbols():
            if (symbol.is_referenced() and symbol.is_global()
                    and symbol.get_name() not in allowed):
                unresolved.add(symbol.get_name())
        for child in table.get_children():
            visit(child)

    visit(target)
    return unresolved


def check_new_undefined_names(before_text, after_text, qualified_name):
    """Reject newly unresolved bare names introduced by a generated patch."""

    before = _unresolved_global_references(before_text, qualified_name)
    after = _unresolved_global_references(after_text, qualified_name)
    introduced = sorted(after - before)
    if introduced:
        raise ValueError("undefined_name: " + ", ".join(introduced))


def _target_function_node(text, qualified_name):
    """Return the one function node matching a dotted symbol identity."""

    tree = ast.parse(text)
    found = []

    def visit(node, parents):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                names = parents + [child.name]
                if (".".join(names) == qualified_name
                        and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))):
                    found.append(child)
                visit(child, names)
            else:
                visit(child, parents)

    visit(tree, [])
    if len(found) != 1:
        raise ValueError(f"AST is missing an unambiguous target: {qualified_name}")
    return found[0]


def _body_statement_dumps(node):
    """Return top-level executable statements with the docstring removed."""

    body = list(node.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return [ast.dump(statement, include_attributes=False) for statement in body]


def check_statement_reorder_only(before_text, after_text, qualified_name):
    """Reject candidates whose only executable change is statement ordering."""

    before = _target_function_node(before_text, qualified_name)
    after = _target_function_node(after_text, qualified_name)
    before_header = copy.deepcopy(before)
    after_header = copy.deepcopy(after)
    before_header.body = [ast.Pass()]
    after_header.body = [ast.Pass()]
    if (ast.dump(before_header, include_attributes=False)
            != ast.dump(after_header, include_attributes=False)):
        return
    before_statements = _body_statement_dumps(before)
    after_statements = _body_statement_dumps(after)
    if (before_statements != after_statements
            and sorted(before_statements) == sorted(after_statements)):
        raise ValueError("statement_reorder_only: executable statements only changed order")


def _terminates(statements):
    """Does this statement list always leave the function before its end?"""

    for node in statements:
        if isinstance(node, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
            return True
        if isinstance(node, ast.If) and node.orelse:
            if _terminates(node.body) and _terminates(node.orelse):
                return True
        if isinstance(node, ast.With) and _terminates(node.body):
            return True
        if isinstance(node, ast.Try):
            if node.finalbody and _terminates(node.finalbody):
                return True
            if (_terminates(node.body)
                    and all(_terminates(handler.body) for handler in node.handlers)
                    and (not node.orelse or _terminates(node.orelse))):
                return True
    return False


def _trailing_dead_statements(node):
    """How many top-level body statements sit after the function must have left."""

    body = node.body
    for index in range(len(body) - 1):
        if _terminates([body[index]]):
            return len(body) - index - 1
    return 0


def check_reachable(before_text, after_text, qualified_name):
    """Reject a candidate whose new statements can never run.

    A weak model asked to fix a function will sometimes append its edit after
    an if/elif/else in which every branch returns. The result parses, stays
    inside the function, introduces no undefined name, is not a reorder, and
    applies cleanly -- and is dead code. The rewrite smoke run on astropy-14598
    produced four such candidates, all four of which passed every other gate.

    Only NEWLY unreachable statements are refused: a function that already
    ended with unreachable code is not this candidate's fault.
    """

    before = _target_function_node(before_text, qualified_name)
    after = _target_function_node(after_text, qualified_name)
    added = _trailing_dead_statements(after) - _trailing_dead_statements(before)
    if added > 0:
        raise ValueError(
            f"unreachable_code: {added} new statement(s) after the function always returns")

def check_new_global_names(before_text, after_text, symbol):
    """Reject newly referenced globals absent from the original module."""

    before_tree = ast.parse(before_text)
    if any(isinstance(node, ast.ImportFrom) and
           any(alias.name == "*" for alias in node.names)
           for node in before_tree.body):
        return  # A star import has no statically enumerable bindings.

    def target_table(text):
        root = symtable.symtable(text, "<candidate>", "exec")
        table = root
        for name in symbol.split("."):
            table = next((child for child in table.get_children()
                          if child.get_name() == name), None)
            if table is None:
                raise ValueError("target_function_missing")
        return root, table

    def global_reads(table):
        reads = {entry.get_name() for entry in table.get_symbols()
                 if entry.is_referenced() and entry.is_global()}
        for child in table.get_children():
            reads.update(global_reads(child))
        return reads

    before_root, before_target = target_table(before_text)
    _, after_target = target_table(after_text)
    module_names = {entry.get_name() for entry in before_root.get_symbols()
                    if entry.is_assigned() or entry.is_imported() or entry.is_namespace()}
    introduced = global_reads(after_target) - global_reads(before_target)
    missing = introduced - module_names - set(dir(builtins))
    if missing:
        raise ValueError("new_unbound_names: " + ", ".join(sorted(missing)))


def check_locked_function_contract(before_tree, after_tree, symbol):
    """Keep the public call shape and established instance state for a target."""

    def target_node(tree):
        nodes = tree.body
        target = None
        for name in symbol.split("."):
            target = next((node for node in nodes
                           if isinstance(node, (ast.ClassDef, ast.FunctionDef,
                                                ast.AsyncFunctionDef)) and node.name == name), None)
            if target is None:
                raise ValueError("target_function_missing")
            nodes = target.body
        return target

    before, after = target_node(before_tree), target_node(after_tree)
    def shape(value):
        if isinstance(value, list):
            return [shape(item) for item in value]
        return ast.dump(value, include_attributes=False) if isinstance(value, ast.AST) else value

    for field in ("args", "returns", "decorator_list", "type_comment"):
        if shape(getattr(before, field)) != shape(getattr(after, field)):
            raise ValueError("signature_changed: " + symbol)

    def assigned_instance_attributes(node):
        return {target.attr for statement in ast.walk(node)
                if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign))
                for target in (statement.targets if isinstance(statement, ast.Assign)
                               else [statement.target])
                if isinstance(target, ast.Attribute) and
                isinstance(target.value, ast.Name) and target.value.id == "self"}

    removed = assigned_instance_attributes(before) - assigned_instance_attributes(after)
    if removed:
        raise ValueError("instance_attributes_removed: " + ", ".join(sorted(removed)))


def changed_line_count(patch):
    """How many lines a unified diff adds or removes.

    Diff headers are excluded. ``--- a/f`` starts with "-" and ``+++ b/f``
    starts with "+", so counting every such line charged two per file touched:
    a two-file patch carried a four-line surcharge against a one-file patch's
    two. That surcharge is invisible when the thing being compared is two
    candidates for the same function, and decisive when it is two patches on
    different functions -- and the gold patch for astropy-13465 spans two files.
    """

    total = 0
    for line in (patch or "").splitlines():
        if line.startswith(("+++", "---", "@@", "diff --git", "index ",
                            "new file", "deleted file", "similarity ",
                            "rename ", "Binary files", "\\ No newline")):
            continue
        if line.startswith(("+", "-")):
            total += 1
    return total


def build_patch(raw, span, middle, path):
    expression_scope = span.get('generation_scope') == 'python_expression'
    if expression_scope:
        from .local_fim import normalize_expression
        middle = normalize_expression(middle)
    middle = middle.replace("\r\n", "\n")
    if "\r" in middle:
        raise ValueError("Unexpected carriage return in generation")
    if span["newline"] == "\r\n":
        middle = middle.replace("\n", "\r\n")
    if not expression_scope and middle and not middle.endswith("\n") and span["suffix"]:
        raise ValueError("Middle does not join suffix at a line boundary")
    text = span["prefix"] + middle + span["suffix"]
    ast.parse(text)
    encoding = "utf-8-sig" if span["bom"] else "utf-8"
    after = text.encode(encoding)
    if after == raw:
        raise ValueError("no_op")
    check_executable_change(raw.decode("utf-8-sig"), text)
    check_new_undefined_names(raw.decode("utf-8-sig"), text, span["symbol"])
    check_statement_reorder_only(raw.decode("utf-8-sig"), text, span["symbol"])
    check_reachable(raw.decode("utf-8-sig"), text, span["symbol"])
    # Body replacement must not escape its function and insert new definitions.
    old_tree = ast.parse(raw.decode("utf-8-sig"))
    new_tree = ast.parse(text)
    check_new_global_names(raw.decode("utf-8-sig"), text, span["symbol"])
    if span.get("lock_signature"):
        check_locked_function_contract(old_tree, new_tree, span["symbol"])
    def outside_body(tree):
        def walk(node, parents):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    names = parents + [child.name]
                    if ".".join(names) == span["symbol"] and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        child.body = [ast.Pass()]
                        if span.get("include_signature"):
                            child.decorator_list = []
                            child.returns = None
                            child.type_comment = None
                            child.args = ast.arguments(
                                posonlyargs=[], args=[], vararg=None,
                                kwonlyargs=[], kw_defaults=[], kwarg=None,
                                defaults=[],
                            )
                    else:
                        walk(child, names)
                else:
                    walk(child, parents)
        walk(tree, [])
        return ast.dump(tree, include_attributes=False)
    if outside_body(old_tree) != outside_body(new_tree):
        raise ValueError("Generated body changed syntax outside the target function")
    before_text, after_text = raw.decode("utf-8"), after.decode("utf-8")
    chunks = difflib.unified_diff(before_text.splitlines(keepends=True),
                                  after_text.splitlines(keepends=True),
                                  fromfile="a/" + path, tofile="b/" + path)
    patch = ""
    for line in chunks:
        patch += line
        if not line.endswith("\n"):
            patch += "\n\\ No newline at end of file\n"
    return patch, after


# How far down the Stage-3 ranking a policy may look for a target the AST supports.
# "strict_top1" patches rank 1 or nothing, which is the honest default when the
# question is "can we repair what fault localization actually pointed at".
# "first_supported_ast" skips ranks the AST cannot support (class targets, inline
# bodies, oversized bodies) and reports the rank it landed on, so a run can be
# split into strict and fallback populations afterwards.
TARGET_POLICY_DEPTH = {
    "strict_top1": 1,
    "first_supported_ast": 5,
    "callable_candidates_top5": 5,
    "first_supported_ast_top5": 5,
    "called_helper_top5": 5,
    "called_helper_multisymbol": 5,
    "related_targets_top5": 5,
}
RESEARCH_ONLY_TARGET_POLICIES = (
    "callable_candidates_top5",
    "first_supported_ast_top5", "called_helper_top5",
    "called_helper_multisymbol",
    "related_targets_top5",
)


# A repair target has to be a function body this pipeline can replace.
# ``function_span`` refuses anything else outright -- that refusal is the
# ``unsupported_symbol`` rejection bucket.
CALLABLE_SYMBOL_KINDS = frozenset({"function", "method"})


def callable_candidate_rows(bug_location, limit):
    """Rank from the whole symbol pool with classes and modules removed.

    Stage-3's returned top-5 is ordered by text retrieval score alone, which
    does not know that a class can never be patched here. On the fixed five
    tickets that cost most of the budget: astropy-14365 spent 4 of its 5 slots
    on classes while the symbol the reference patch edits sat at candidate #10,
    and astropy-13236 spent 3 slots on classes with its gold symbol at #9.
    Dropping non-callables moves both into the top 5 (#5 and #4) without any
    new scoring signal, because the rows removed were ones the AST gate would
    have thrown away anyway.

    Returns [] when the pool is missing, so the caller keeps the ranked list.
    """

    pool = bug_location.get("stage3_candidate_symbols")
    if not isinstance(pool, list):
        return []
    kept = [row for row in pool
            if isinstance(row, dict)
            and row.get("symbol_kind") in CALLABLE_SYMBOL_KINDS
            and row.get("file_path") and row.get("symbol_qualified_name")]
    return kept[:limit]


def _called_helper_target_key(ticket_json, rank, row):
    """Prefer call-backed helpers whose identity matches public ticket terms."""

    ticket_text = " ".join(
        str(ticket_json.get(key) or "")
        for key in ("title", "summary", "body", "description", "bug_report")
    ).casefold()
    identity = " ".join(
        str(row.get(key) or "")
        for key in ("symbol_qualified_name", "called_by_symbol")
    )
    identity = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", identity)
    identity_terms = {
        term for term in re.findall(r"[A-Za-z][A-Za-z0-9]+", identity.casefold())
        if len(term) >= 4
    }
    overlap = sum(1 for term in identity_terms if term in ticket_text)
    return (
        -overlap,
        -float(row.get("called_helper_score") or 0.0),
        rank,
    )


class FIMPatchGenerator:
    """Drop-in generate(ticket_json, bug_location, repo_path) interface.

    Validation callback takes (patch, repository_path) and returns a report.
    This module never edits the input repository.
    """

    def __init__(self, backend=None, *, candidates=4, seed=42, validator=None,
                 research_mode=False, local_context=False, target_policy="strict_top1",
                 expand_truncated_end=None, max_body_lines=0,
                 include_original_body=False,
                 min_body_retention=DEFAULT_MIN_BODY_RETENTION,
                 max_related_targets=3,
                 large_function_strategy="full_body",
                 large_function_line_threshold=LARGE_FUNCTION_LINE_THRESHOLD,
                 generation_mode="fim", extra_directive="",
                 explicit_argument_edit=False, prompt_layout="task_last",
                 safe_function_extraction=False, fixed_signature_body=False, helper_contracts=False, helper_implementations=False,
                 fixed_body_extraction=False, local_fim=False, local_fim_expressions=False,
                 scope_adjustment=False, fim_scope=None, fim_output_ceiling=None, safe_body_feedback=False,
                 operation_contract_preview=False, contract_feedback=False, domain_adapters=(),
                 fim_context_mode='local',body_boundary_extraction=None,source_none_probe=None):
        if generation_mode not in GENERATION_MODES:
            raise ValueError("Unknown generation mode")
        self.generation_mode = generation_mode
        self.helper_implementations = bool(helper_implementations)
        if self.helper_implementations and not helper_contracts:
            raise ValueError("helper implementations require helper contracts")
        self.helper_contracts = bool(helper_contracts)
        if self.helper_contracts and generation_mode != "fim":
            raise ValueError("helper contracts require FIM mode")
        if fim_scope not in (None, 'expression', 'statement', 'body'):
            raise ValueError('Unknown explicit FIM scope')
        if fim_scope is not None and (scope_adjustment or not research_mode or candidates != 1
                                     or (local_fim_expressions and fim_scope != 'expression')):
            raise ValueError('explicit FIM scope requires single-candidate research mode without scope adjustment or conflicting expression flag')
        if fim_output_ceiling is not None and (fim_scope is None or isinstance(fim_output_ceiling, bool)
                or not isinstance(fim_output_ceiling, int) or fim_output_ceiling < MIN_BODY_TOKENS):
            raise ValueError('fim_output_ceiling requires explicit scope and at least 128 tokens')
        self.fim_scope, self.fim_output_ceiling = fim_scope, fim_output_ceiling
        self.safe_body_feedback = bool(safe_body_feedback)
        if fim_context_mode not in ('local','file'):raise ValueError('unknown FIM context mode')
        if fim_context_mode=='file' and fim_scope is None:raise ValueError('file FIM context requires explicit scope')
        for option in (body_boundary_extraction,source_none_probe):
            if option is not None and type(option) is not bool:raise ValueError('controlled body options must be bool or None')
            if option and fim_scope!='body':raise ValueError('controlled body options require explicit body scope')
        self.fim_context_mode=fim_context_mode
        self.body_boundary_extraction=self.safe_body_feedback if body_boundary_extraction is None else body_boundary_extraction
        self.source_none_probe=self.safe_body_feedback if source_none_probe is None else source_none_probe
        self.controlled_body_options_requested=body_boundary_extraction is not None or source_none_probe is not None or fim_context_mode!='local'
        self.operation_contract_preview = bool(operation_contract_preview)
        self.contract_feedback = bool(contract_feedback)
        from .domain_adapters import validate_adapters
        self.domain_adapters = validate_adapters(domain_adapters)
        if self.contract_feedback and not self.operation_contract_preview:
            raise ValueError('contract feedback requires operation contract preview')
        if self.contract_feedback and target_policy not in ('strict_top1','first_supported_ast','first_supported_ast_top5'):
            raise ValueError('contract feedback requires a primary-only predicted FL policy')
        if self.operation_contract_preview and fim_scope != 'body':
            raise ValueError('operation contract preview requires explicit body FIM scope')
        if self.safe_body_feedback and (fim_scope != 'body' or fixed_body_extraction or safe_function_extraction):
            raise ValueError('safe body feedback requires explicit body scope without legacy extraction')
        self.local_fim = bool(local_fim or fim_scope is not None)
        self.local_fim_expressions = bool(local_fim_expressions or fim_scope == 'expression')
        self.scope_adjustment = bool(scope_adjustment)
        self.public_behavior_enabled = (self.local_fim_expressions or fim_scope is not None) and not scope_adjustment
        if self.scope_adjustment and (not self.local_fim_expressions or candidates != 1 or not research_mode):
            raise ValueError('scope adjustment requires research mode, local FIM expressions and candidates=1')
        if self.local_fim_expressions and not self.local_fim:
            raise ValueError('expression FIM requires local FIM')
        if self.local_fim and (generation_mode != 'fim' or prompt_layout != 'compact_fim'
                or large_function_strategy != 'full_body' or safe_function_extraction or fixed_body_extraction
                or (helper_implementations and fim_scope!='body') or target_policy in {'related_targets_top5','called_helper_multisymbol'}):
            raise ValueError('local FIM requires compact_fim single-target full_body FIM without whole-body extraction/multi-function')
        self.fixed_signature_body = bool(fixed_signature_body or self.local_fim)
        self.fixed_body_extraction = bool(fixed_body_extraction)
        if self.fixed_body_extraction and not self.fixed_signature_body:
            raise ValueError("fixed body extraction requires fixed signature body")
        if self.fixed_signature_body and (generation_mode != "fim" or prompt_layout != "compact_fim"
                                         or large_function_strategy != "full_body"
                                         or target_policy == "called_helper_multisymbol"
                                         or safe_function_extraction):
            raise ValueError("fixed signature body requires compact_fim, full_body FIM, and no whole-function extraction/multisymbol caller")
        self.safe_function_extraction = bool(safe_function_extraction)
        if self.safe_function_extraction and generation_mode != "fim":
            raise ValueError("safe function extraction requires FIM mode")
        self.backend = backend or (
            OllamaRewrite() if generation_mode == "rewrite" else OllamaFIM())
        required_method = "rewrite" if generation_mode == "rewrite" else "infill"
        if not callable(getattr(self.backend, required_method, None)):
            raise ValueError(f"Backend does not support generation_mode={generation_mode}")
        if not 1 <= candidates <= 12:
            raise ValueError("candidates must be between 1 and 12")
        self.candidates, self.seed, self.validator = candidates, seed, validator
        self.research_mode, self.local_context = research_mode, local_context
        if target_policy not in TARGET_POLICY_DEPTH:
            raise ValueError("Unknown target policy")
        if target_policy in RESEARCH_ONLY_TARGET_POLICIES and not research_mode:
            raise ValueError("Alternative target selection requires research_mode")
        if (generation_mode == "rewrite"
                and target_policy in REWRITE_UNSUPPORTED_TARGET_POLICIES):
            raise ValueError("rewrite mode supports one primary target only")
        self.target_policy = target_policy
        self.target_depth = TARGET_POLICY_DEPTH[target_policy]
        # Default follows the policy: a single-target policy keeps the strict
        # coordinate equality it was written with; a scanning policy allows the
        # documented truncation repair. Either can be set explicitly.
        self.expand_truncated_end = (self.target_depth > 1 if expand_truncated_end is None
                                     else bool(expand_truncated_end))
        if not isinstance(max_body_lines, int) or isinstance(max_body_lines, bool) or max_body_lines < 0:
            raise ValueError("max_body_lines must be a non-negative integer")
        self.max_body_lines = max_body_lines
        if large_function_strategy not in LARGE_FUNCTION_STRATEGIES:
            raise ValueError("Unknown large_function_strategy")
        if generation_mode == "rewrite" and large_function_strategy != "full_body":
            raise ValueError("rewrite mode requires large_function_strategy=full_body")
        self.large_function_strategy = large_function_strategy
        if (not isinstance(large_function_line_threshold, int)
                or isinstance(large_function_line_threshold, bool)
                or large_function_line_threshold < 1):
            raise ValueError("large_function_line_threshold must be a positive integer")
        # Lowering this applies statement-level narrowing to ordinary functions,
        # not only oversized ones. That is the setting worth testing against
        # copying: a model asked for one statement has far less room to return
        # the original than one asked for a whole body. Defaults to the
        # oversize-only behaviour.
        self.large_function_line_threshold = large_function_line_threshold
        self.include_original_body = bool(include_original_body)
        self.explicit_argument_edit = bool(explicit_argument_edit)
        if not 0 <= float(min_body_retention) <= 1:
            raise ValueError("min_body_retention must be between 0 and 1")
        self.min_body_retention = float(min_body_retention)
        if (not isinstance(max_related_targets, int) or isinstance(max_related_targets, bool)
                or not 1 <= max_related_targets <= 5):
            raise ValueError("max_related_targets must be between 1 and 5")
        self.max_related_targets = max_related_targets
        # Extra commented instruction appended to the repair directive. It exists
        # for prompt experiments -- a worked demonstration of the expected edit,
        # for instance -- and is held to comments so an experiment can never
        # introduce code the reconstruction path did not produce. Reference
        # patches and hidden tests are outside the caller's reach either way:
        # the batch runner sanitizes the ticket before generation begins.
        if not isinstance(extra_directive, str):
            raise ValueError("extra_directive must be a string")
        block = extra_directive.replace("\r\n", "\n").strip("\n")
        if block:
            if any(marker in block for marker in ("<PRE>", "<SUF>", "<MID>", "<EOT>")):
                raise ValueError("FIM marker collision in source/evidence")
            offenders = [line for line in block.splitlines() if line.strip()
                         and not line.lstrip().startswith("#")]
            if offenders:
                raise ValueError("extra_directive must contain only comment lines")
            block += "\n"
        self.extra_directive = block
        # "task_last" is the rewritten layout: issue brief, then source, then the
        # instruction immediately above the generation point. "legacy" reproduces
        # the layout research-3* through baseline-T08 were generated with, so a
        # published number can still be regenerated.
        if prompt_layout not in PROMPT_LAYOUTS:
            raise ValueError("Unknown prompt_layout")
        self.prompt_layout = prompt_layout

    def generate(self, ticket_json, bug_location, repo_path, *, plan_only=False):
        result = {"patch_status": "blocked", "patch": "", "modified_files": [],
                  "generation_strategy": ("rewrite_function"
                                          if self.generation_mode == "rewrite"
                                          else "fim_psm"),
                  "generation_mode": self.generation_mode,
                  "apply_status": "not_run",
                  "validation_status": "not_run",
                  "requires_manual_patch": True, "requires_manual_review": True,
                  "recommend_patch_generation": False, "candidates": []}
        result["research_mode"] = self.research_mode
        from .domain_adapters import VERSIONS
        result['domain_adapters']={name:VERSIONS[name] for name in self.domain_adapters}
        result["target_policy"] = self.target_policy
        result["target_resolution"] = []
        result["expand_truncated_end"] = self.expand_truncated_end
        result["max_body_lines"] = self.max_body_lines
        result["large_function_strategy"] = self.large_function_strategy
        result["large_function_line_threshold"] = self.large_function_line_threshold
        result["include_original_body"] = (
            True if self.generation_mode == "rewrite" else self.include_original_body
        )
        result["explicit_argument_edit"] = self.explicit_argument_edit
        if self.helper_implementations:
            result["helper_implementations"] = "bounded-call-chain-v1"
        if self.helper_contracts:
            result["helper_contracts"] = "same-file-helper-contract-v1"
        if self.fixed_signature_body:
            result["fixed_signature_body"] = "fixed-signature-body-v1"
        if self.fixed_body_extraction:
            result["fixed_body_extraction"] = "fixed-body-boundary-v1"
        if self.safe_function_extraction:
            result["safe_function_extraction"] = "safe-function-extraction-v1"
        if self.local_fim:
            result["local_fim"] = "local-fim-v1"
        if self.local_fim_expressions:
            result['local_fim_expressions'] = 'local-fim-expression-v2'
        if self.fim_scope is not None:
            result.update(fim_scope=self.fim_scope,fim_scope_version='explicit-fim-scope-v1',
                          fim_output_ceiling=self.fim_output_ceiling)
        if self.safe_body_feedback:
            result['safe_body_feedback']='safe-body-feedback-v1'
            result['source_contract_probe']='source-none-probe-v2-symbolic'
        if self.controlled_body_options_requested:
            result['controlled_fim_options']=dict(version='controlled-fim-options-v1',context=self.fim_context_mode,
                body_boundary_extraction=self.body_boundary_extraction,source_none_probe=self.source_none_probe)
        if self.source_none_probe:result['source_contract_probe']='source-none-probe-v2-symbolic'
        if self.operation_contract_preview:
            result['operation_contract_preview']='operation-contract-preview-v1'
            result['operation_contract_diagnostic_version']='operation-contract-diagnostic-v1'
        result["prompt_layout"] = (None if self.generation_mode == "rewrite"
                                   else self.prompt_layout)
        result["min_body_retention"] = self.min_body_retention
        result['body_retention_metric']='executable-lines-v2'
        result["max_related_targets"] = self.max_related_targets
        result["target_rank"] = None
        result["target_fallback_used"] = False
        if not isinstance(ticket_json, dict) or not isinstance(bug_location, dict):
            result.update(patch_status="blocked_invalid_context", explanation="Ticket and localization must be JSON objects")
            return result
        production_eligible = not (bug_location.get("recommend_patch_generation") is not True or
                bug_location.get("should_manual_review") is not False or
                bug_location.get("confidence_level") != "high")
        if not production_eligible and not self.research_mode:
            result.update(patch_status="blocked_low_confidence", explanation="Explicit high-confidence localization required")
            return result
        rows = bug_location.get("stage3_ranked_symbols") or []
        try:
            if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
                raise ValueError("Stage-3 symbols required")
            selected = None
            # Select before model calls, using source/AST only, never test results.
            limit = self.target_depth
            if self.target_policy == "callable_candidates_top5":
                callable_rows = callable_candidate_rows(bug_location, limit)
                result["callable_pool_used"] = bool(callable_rows)
                if callable_rows:
                    rows = callable_rows
            selection_rows = list(enumerate(rows[:limit], 1))
            if self.target_policy in {"called_helper_top5", "called_helper_multisymbol",
                                      "related_targets_top5"}:
                selection_rows.sort(
                    key=lambda item: _called_helper_target_key(
                        ticket_json, item[0], item[1]
                    )
                )
            for rank, row in selection_rows:
                audit = {"rank": rank, "input": row, "status": "rejected"}
                result["target_resolution"].append(audit)
                try:
                    if not isinstance(row, dict):
                        raise ValueError("Stage-3 row must be a JSON object")
                    raw, commit, path = source_for(ticket_json, bug_location, repo_path, row)
                    span = function_span(raw, row,
                                         expand_truncated_end=self.expand_truncated_end,
                                         max_body_lines=0 if self.local_fim else self.max_body_lines,
                                         include_signature=(
                                             not self.fixed_signature_body and self.target_policy in {
                                                 "called_helper_multisymbol",
                                                 "related_targets_top5",
                                             }
                                         ))
                    if self.fixed_signature_body:
                        baseline_span = function_span(raw, row,
                            expand_truncated_end=self.expand_truncated_end,
                            max_body_lines=0 if self.local_fim else self.max_body_lines, include_signature=True)
                        # Keep the whole-function control's output cap unchanged.
                        span["fixed_output_budget"] = body_token_budget(baseline_span, self.backend.max_new_tokens)
                        # Anchor at the physical end of the complete original header,
                        # leaving leading body comments in Middle instead of Prefix.
                        source_text = raw.decode("utf-8-sig")
                        node = _target_function_node(source_text, span["symbol"])
                        lines = source_text.splitlines(keepends=True)
                        suite_line = node.lineno + _function_suite_start_line(
                            "".join(lines[node.lineno - 1:])) - 1
                        start = sum(len(line) for line in lines[:suite_line - 1])
                        span.update(start=start, line_start=suite_line,
                                    prefix=source_text[:start], original_middle=source_text[start:span["end"]],
                                    body_lines=span["line_end"] - suite_line + 1)
                        if not self.local_fim and self.max_body_lines and span["body_lines"] > self.max_body_lines:
                            raise ValueError("Fixed-signature body exceeds max_body_lines")
                    if self.local_fim and self.fim_scope is None:
                        from .local_fim import select_span
                        span, local_audit = select_span(raw, span, ticket_json, path)
                        audit['local_fim_selection'] = local_audit
                        if self.local_fim_expressions:
                            from .local_fim import anchor_span
                            span, audit['expression_fim'] = anchor_span(raw, span)
                        if self.max_body_lines and span['body_lines'] > self.max_body_lines:
                            raise ValueError('local_fim_fallback_exceeds_max_body_lines')
                    if (self.large_function_strategy == "issue_statement"
                            and span["body_lines"] >= self.large_function_line_threshold):
                        try:
                            span = issue_statement_span(raw, span, ticket_json)
                        except ValueError as narrowing_error:
                            # No statement matches the issue's own words. Repair
                            # the whole body rather than dropping a target that
                            # the full-body path can still handle -- otherwise
                            # lowering the threshold would silently cost
                            # coverage.
                            if "issue_statement_unavailable" not in str(narrowing_error):
                                raise
                            audit["narrowing_skipped"] = str(narrowing_error)

                    span["lock_signature"] = self.local_fim or self.target_policy == "related_targets_top5"
                    selected = (row, raw, commit, path, span, rank)
                    audit.update(status="selected", coordinates_expanded=span["coordinates_expanded"],
                                 body_start=span["line_start"], body_end=span["line_end"],
                                 body_lines=span["body_lines"],
                                 generation_scope=span.get("generation_scope", "function_body"),
                                 function_body_lines=span.get("function_body_lines", span["body_lines"]))
                    break
                except (ValueError, OSError, subprocess.SubprocessError, SyntaxError) as exc:
                    audit["reason"] = str(exc)
            if selected is None:
                raise ValueError("No supported target: " + "; ".join(a["reason"] for a in result["target_resolution"]))
            row, raw, commit, path, span, target_rank = selected
            if self.fim_scope is not None:
                from .local_fim import select_explicit_scope
                span, local_audit, expression_audit = select_explicit_scope(raw, span, ticket_json, path, self.fim_scope)
                if self.max_body_lines and span['body_lines'] > self.max_body_lines:
                    raise ValueError('fim_scope_unavailable: locked scope exceeds max_body_lines')
                native_budget = body_token_budget(span, self.backend.max_new_tokens)
                if self.fim_output_ceiling is not None:
                    if self.fim_output_ceiling > self.backend.max_new_tokens:
                        raise ValueError('fim_output_ceiling exceeds backend maximum')
                    span['fixed_output_budget'] = min(native_budget, self.fim_output_ceiling)
                audit.update(local_fim_selection=local_audit, expression_fim=expression_audit,
                             body_start=span['line_start'], body_end=span['line_end'],
                             body_lines=span['body_lines'], generation_scope=span.get('generation_scope','function_body'))
                result.update(fim_scope=self.fim_scope, fim_scope_version='explicit-fim-scope-v1',
                              fim_output_ceiling=self.fim_output_ceiling, native_output_budget=native_budget)
            result["target_rank"] = target_rank
            result["target_fallback_used"] = target_rank > 1
            if self.local_fim:
                result['local_fim_selection'] = dict(audit['local_fim_selection'])
            if self.local_fim_expressions:
                result['expression_fim'] = dict(audit['expression_fim'])
            if self.scope_adjustment:
                from .scope_planning import plan_scope
                from .scope_execution import VERSION as scope_version
                result['scope_execution'] = scope_version
                result['scope_plan'] = plan_scope(raw,span,ticket_json,path)
            if plan_only:
                # Select and verify the same primary without backend capability probes.
                result.update(patch_status="planned", base_commit=commit,
                              source_file_sha256={path: sha(raw)}, span=span,
                              bug_location={"file": path, "symbol": span["symbol"]})
                return result
            if self.scope_adjustment:
                from .scope_execution import execute_scope
                executed = execute_scope(self,ticket_json,bug_location,repo_path,row,
                                         raw,commit,path,span,result)
                if executed is not None:
                    return executed
            generation_scope = span.get("generation_scope", "function_body")
            rewrite_source_span = None
            if self.generation_mode == "rewrite":
                rewrite_source_span = function_span(
                    raw, row,
                    expand_truncated_end=self.expand_truncated_end,
                    max_body_lines=self.max_body_lines,
                    include_signature=True,
                )
                prompt_prefix = rewrite_prompt(
                    ticket_json, rewrite_source_span, rows, target_rank)
                prompt_suffix = None
                context_info = {"strategy": "complete_target_function",
                                "truncated": False}
            else:
                shown_body = span["original_middle"] if self.include_original_body else None
                helper_context = ""
                helper_blocks, helper_audit = [], []
                if self.helper_contracts:
                    from .helper_contracts import context
                    helper_context = context(raw.decode("utf-8-sig"), span["symbol"])
                    result["helper_context"] = helper_context
                helper_signature_context = helper_context
                if self.helper_implementations:
                    from .adaptive import implementation_blocks
                    helper_blocks, helper_audit = implementation_blocks(raw.decode("utf-8-sig"), span["symbol"],
                        public_text="\n".join(str(ticket_json.get(k) or "") for k in PUBLIC_REQUIREMENT_KEYS))
                    helper_context += ''.join(helper_blocks)
                guidance = localization_guidance(rows, target_rank)
                directive = repair_directive(
                    ticket_json, row, replacement_scope=generation_scope,
                    explicit_argument_edit=self.explicit_argument_edit,
                    include_signature=span.get("include_signature", False),domain_adapters=self.domain_adapters)
                directive += self.extra_directive
                if self.target_policy == "related_targets_top5":
                    directive += (
                        "# This function may be part of a multi-file repair. Keep interfaces "
                        "consistent with directly related functions and change only behavior "
                        "needed by the issue.\n"
                    )
                after_marker = "# AFTER (fixed):\n" if self.include_original_body else ""
                source_summary_context = ""
                if getattr(self, 'source_summary_enabled', False) and generation_scope == 'function_body':
                    from .source_summary import summarize
                    result['source_body_summary'] = summarize(raw, span['symbol'])
                    if getattr(self, 'requirement_conflicts_enabled', False):
                        from .requirement_conflicts import mark_conflicts
                        result['source_body_summary'] = mark_conflicts(result['source_body_summary'], ticket_json)
                    source_summary_context = result['source_body_summary']['rendered']
                requirement_limit = (2000 if self.local_fim else LOCAL_REQUIREMENT_CHAR_LIMIT
                                     if generation_scope == "issue_statement" else 0)

                def head_of(layout):
                    if layout == "compact_fim":
                        scope = ("complete function including its signature and body, retaining decorators"
                                 if span.get("include_signature") else
                                 "one Python expression only; no statements, comments or docstrings" if generation_scope == 'python_expression' else
                                 ("one complete replacement statement; no sibling statements or definitions" if self.fim_scope else "replacement statement") if generation_scope == "issue_statement"
                                 else "function body only; do not output its signature or sibling functions")
                        # Put evidence before source context; the infill boundary ends
                        # in real code rather than a long sequence of instructions.
                        return (issue_brief(ticket_json, max_chars=requirement_limit,
                                           infer_expected=self.public_behavior_enabled)
                                + helper_context
                                + f"# Repair {path}: {span['symbol']}. Output {scope}.\n"
                                + "# Fix the reported behaviour; preserve unrelated logic. Output Python only.\n"
                                + self.extra_directive
                                + (current_implementation_block(shown_body, "", span["symbol"])
                                   if shown_body else "")
                                + source_summary_context
                                + "# Source context follows; fill the missing code.\n")
                    if layout == "task_last":
                        return issue_brief(
                            ticket_json, max_chars=requirement_limit) + helper_context + guidance
                    return (helper_context + evidence(ticket_json, shown_body,
                                     max_requirement_chars=requirement_limit)
                            + directive + guidance + after_marker)

                def tail_of(layout):
                    """What follows the source prefix, at the body's indentation."""

                    if layout != "task_last":
                        return ""
                    try:
                        indent = " " * body_indent_of(span)
                    except ValueError:
                        indent = "    "
                    return (current_implementation_block(
                        shown_body, indent, span["symbol"]) if shown_body else ""
                    ) + task_block(directive, indent, span["symbol"],
                                   replacement_scope=generation_scope,
                                   include_signature=span.get("include_signature", False))

                def assemble(source_prefix):
                    """A comment tail only joins code at a line boundary."""

                    if generation_scope != 'python_expression' and source_prefix and not source_prefix.endswith("\n"):
                        source_prefix += "\n"
                    return (head_of(self.prompt_layout) + source_prefix
                            + tail_of(self.prompt_layout))

                prompt_source_prefix = span["prefix"].replace("\r\n", "\n")
                prompt_prefix = assemble(prompt_source_prefix)
                prompt_suffix = span["suffix"].replace("\r\n", "\n")
                context_info = {"strategy": "complete_file", "truncated": False}
                if self.local_context and not self.local_fim:
                    # Prompt-only line-aligned crop. Reconstruction still uses full source.
                    before = span["prefix"].replace("\r\n", "\n").splitlines(keepends=True)
                    after = prompt_suffix.splitlines(keepends=True)
                    prefix_lines = []
                    for line in reversed(before):
                        if sum(len(x.encode('utf-8')) for x in prefix_lines) + len(line.encode('utf-8')) > 4000:
                            break
                        prefix_lines.insert(0, line)
                    suffix_lines = []
                    for line in after:
                        if sum(len(x.encode('utf-8')) for x in suffix_lines) + len(line.encode('utf-8')) > 2000:
                            break
                        suffix_lines.append(line)
                    # Preserve the complete function signature/decorators in the prefix.
                    if len(before) - len(prefix_lines) >= int(row['start_line']):
                        raise ValueError('context_budget_exceeded: function signature cannot fit')
                    prompt_source_prefix = ''.join(prefix_lines)
                    prompt_prefix = assemble(prompt_source_prefix)
                    prompt_suffix = ''.join(suffix_lines)
                    context_info = {"strategy": "local_lines_4000_2000_bytes", "truncated":
                                    len(prefix_lines) < len(before) or len(suffix_lines) < len(after),
                                    "prefix_start_line": len(before)-len(prefix_lines)+1,
                                    "suffix_lines": len(suffix_lines)}
            if self.local_fim:
                from .local_fim import prompt_context
                if self.fim_context_mode=='file':
                    prompt_source_prefix=span['prefix'].replace('\r\n','\n')
                    prompt_suffix=span['suffix'].replace('\r\n','\n')
                    context_info=dict(strategy='complete_file_fim_context',truncated=False)
                else:
                    prompt_source_prefix, prompt_suffix, context_info = prompt_context(raw, span,
                        retain_docstring=self.local_fim_expressions or self.fim_scope is not None)
                prompt_prefix = assemble(prompt_source_prefix)
            caller_target = None
            related_targets = []
            if self.target_policy == "called_helper_multisymbol":
                caller_file = row.get("called_by_file")
                caller_symbol = row.get("called_by_symbol")
                candidate_rows = bug_location.get("stage3_candidate_symbols") or []
                caller_row = next((item for item in candidate_rows
                                   if isinstance(item, dict)
                                   and item.get("file_path") == caller_file
                                   and item.get("symbol_qualified_name") == caller_symbol), None)
                if caller_row is None or caller_file == path:
                    raise ValueError("Cross-file caller evidence is required for multisymbol repair")
                caller_raw, caller_commit, caller_path = source_for(
                    ticket_json, bug_location, repo_path, caller_row)
                if caller_commit != commit:
                    raise ValueError("Multisymbol targets do not share one base commit")
                caller_span = function_span(
                    caller_raw, caller_row,
                    expand_truncated_end=self.expand_truncated_end,
                    max_body_lines=self.max_body_lines,
                )
                caller_before = caller_span["prefix"].replace("\r\n", "\n").splitlines(keepends=True)
                caller_after = caller_span["suffix"].replace("\r\n", "\n").splitlines(keepends=True)
                if self.local_context:
                    kept_before = []
                    for line in reversed(caller_before):
                        if sum(len(x.encode("utf-8")) for x in kept_before) + len(line.encode("utf-8")) > 4000:
                            break
                        kept_before.insert(0, line)
                    kept_after = []
                    for line in caller_after:
                        if sum(len(x.encode("utf-8")) for x in kept_after) + len(line.encode("utf-8")) > 2000:
                            break
                        kept_after.append(line)
                    caller_before, caller_after = kept_before, kept_after
                caller_instruction = (
                    "# Update this cross-file caller so it propagates the issue's "
                    f"tolerance values to {row.get('symbol_qualified_name')}.\n"
                    "# Pass rtol=self.rtol and atol=self.atol in the selected helper call.\n"
                    "# Preserve every unrelated branch and reporting behavior.\n"
                    "# Output the complete replacement function, including its signature "
                    "and body; no prose, Markdown, or sibling functions.\n"
                )
                caller_prefix = (
                    evidence(ticket_json, caller_span["original_middle"])
                    + caller_instruction + "# AFTER (fixed):\n" + "".join(caller_before)
                )
                caller_suffix = "".join(caller_after)
                caller_target = {
                    "row": caller_row, "raw": caller_raw, "path": caller_path,
                    "span": caller_span, "prefix": caller_prefix,
                    "suffix": caller_suffix,
                }
            if self.target_policy == "related_targets_top5":
                candidate_rows = [
                    item for item in (
                        list(bug_location.get("stage3_candidate_symbols") or []) + rows
                    ) if isinstance(item, dict)
                ]
                related_rows = []
                caller_file = row.get("called_by_file")
                caller_symbol = row.get("called_by_symbol")
                for item in candidate_rows:
                    item_key = (item.get("file_path"), item.get("symbol_qualified_name"))
                    selected_key = (path, row.get("symbol_qualified_name"))
                    is_direct_caller = item_key == (caller_file, caller_symbol)
                    is_direct_callee = (
                        item.get("called_by_file"), item.get("called_by_symbol")
                    ) == selected_key
                    if ((is_direct_caller or is_direct_callee)
                            and item.get("file_path") != path
                            and item_key not in {
                                (x.get("file_path"), x.get("symbol_qualified_name"))
                                for x in related_rows
                            }):
                        related_rows.append(item)
                    if len(related_rows) >= self.max_related_targets - 1:
                        break
                if self.fixed_signature_body and related_rows:
                    raise ValueError("fixed_signature_body currently supports one primary target; related repair requires a separate experiment")
                contract_targets = [
                    f"{path}:{row.get('symbol_qualified_name')}"
                ] + [
                    f"{item.get('file_path')}:{item.get('symbol_qualified_name')}"
                    for item in related_rows
                ]
                repair_contract = (
                    "All generated edits form one repair. Preserve unrelated behavior and "
                    "keep calls, parameters, and return values consistent across these targets: "
                    + ", ".join(contract_targets)
                )
                for position, related_row in enumerate(related_rows, 1):
                    related_raw, related_commit, related_path = source_for(
                        ticket_json, bug_location, repo_path, related_row)
                    if related_commit != commit:
                        raise ValueError("Related targets do not share one base commit")
                    related_span = function_span(
                        related_raw, related_row,
                        expand_truncated_end=self.expand_truncated_end,
                        max_body_lines=self.max_body_lines,
                        include_signature=True,
                    )
                    related_span["lock_signature"] = True
                    related_before = related_span["prefix"].replace(
                        "\r\n", "\n").splitlines(keepends=True)
                    related_after = related_span["suffix"].replace(
                        "\r\n", "\n").splitlines(keepends=True)
                    if self.local_context:
                        kept_before = []
                        for line in reversed(related_before):
                            if (sum(len(x.encode("utf-8")) for x in kept_before)
                                    + len(line.encode("utf-8")) > 4000):
                                break
                            kept_before.insert(0, line)
                        kept_after = []
                        for line in related_after:
                            if (sum(len(x.encode("utf-8")) for x in kept_after)
                                    + len(line.encode("utf-8")) > 2000):
                                break
                            kept_after.append(line)
                        related_before, related_after = kept_before, kept_after
                    related_instruction = (
                        "# Related repair contract: " + repair_contract + "\n"
                        "# Rewrite only this target; the host will combine it with the other edits.\n"
                        "# Output the complete replacement function, including its signature "
                        "and body; preserve the signature and output no sibling functions.\n"
                    )
                    related_targets.append({
                        "row": related_row, "raw": related_raw,
                        "path": related_path, "span": related_span,
                        "prefix": evidence(
                            ticket_json,
                            related_span["original_middle"]
                            if self.include_original_body else None,
                        ) + related_instruction + after_marker + "".join(related_before),
                        "suffix": "".join(related_after),
                        "seed_offset": position * 10_000,
                    })
                result["repair_contract"] = repair_contract
            model = self.backend.describe()
            if self.helper_implementations:
                counter = getattr(self.backend, '_token_counter', None)
                limit = getattr(self.backend, 'num_ctx', None)
                budget = body_token_budget(span, self.backend.max_new_tokens)
                def input_size():
                    return (counter.count(prompt_prefix, prompt_suffix) if counter else
                            len((prompt_prefix + prompt_suffix).encode('utf-8')))
                while helper_blocks and limit and input_size() + budget + 8 > limit:
                    helper_blocks.pop()
                    included = [a for a in helper_audit if a['included']]
                    included[-1].update(included=False, reason='token_budget' if counter else 'byte_budget')
                    helper_context = helper_signature_context + ''.join(helper_blocks)
                    prompt_prefix = assemble(prompt_source_prefix)
                result['helper_implementation_audit'] = {'helpers':helper_audit,
                    'input_count':input_size(), 'output_budget':budget, 'context_limit':limit,
                    'measurement':'verified_tokens' if counter else 'utf8_bytes',
                    'fits':input_size()+budget+8<=limit if limit else None}
                result['helper_context'] = helper_context
        except (ValueError, OSError, subprocess.SubprocessError, SyntaxError) as exc:
            result.update(patch_status="blocked_invalid_context", explanation=str(exc))
            return result
        source_hashes = {path: sha(raw)}
        if caller_target:
            source_hashes[caller_target["path"]] = sha(caller_target["raw"])
        for target in related_targets:
            source_hashes[target["path"]] = sha(target["raw"])
        result.update(base_commit=commit, source_file_sha256=source_hashes, model=model,
                      backend_kind=(model.get("backend", "")
                                    if isinstance(model, dict) else ""),
                      bug_location={"file": path, "function": span["symbol"],
                                    "line_start": span["line_start"], "line_end": span["line_end"]},
                      localization_context=rows[:5], recommend_patch_generation=production_eligible,
                      production_eligible=production_eligible, prompt_context=context_info,
                      span={k: v for k, v in span.items() if k not in ("prefix", "suffix", "original_middle")},
                      prompt_version=(
                          REWRITE_PROMPT_VERSION
                          if self.generation_mode == "rewrite" else
                          fim_prompt_version(
                              generation_scope=generation_scope,
                              include_original_body=self.include_original_body,
                              prompt_layout=self.prompt_layout))
                      + ("-explicit-argument-edit" if self.explicit_argument_edit
                         and self.generation_mode == "fim" else "")
                      + ("-fixed-signature-body-v1" if self.fixed_signature_body else "")
                      + ("-helper-contracts-v1" if self.helper_contracts else "")
                      + ("-bounded-call-chain-v1" if self.helper_implementations else ""))
        result["target_set"] = [
            {"file": path, "function": span["symbol"], "role": "primary"}
        ] + [
            {"file": target["path"], "function": target["span"]["symbol"],
             "role": "directly_related"}
            for target in related_targets
        ]
        seen = set()
        if self.local_fim:
            result['prompt_version'] += '-local-fim-v1'
        if self.local_fim_expressions:
            result['prompt_version'] += '-local-fim-expression-v2'
        if self.fim_scope is not None:
            result['prompt_version'] += '-explicit-fim-scope-v1-'+self.fim_scope
        if self.public_behavior_enabled:
            from .public_behavior import VERSION, expectation_evidence
            result['prompt_version'] += '-'+VERSION
            result['public_behavior'] = {'version':VERSION, 'evidence':expectation_evidence(ticket_json)}
        if self.safe_body_feedback:
            result['prompt_version'] += '-safe-body-feedback-v1'
        result['output_normalization'] = ('single-expression-v2' if generation_scope == 'python_expression'
                                          else 'trailing-newline-v1')
        eligible = []
        for i in range(self.candidates):
            candidate = {"candidate_id": i + 1, "status": "rejected", "seed": self.seed + i}
            result["candidates"].append(candidate)
            try:
                if self.generation_mode == "rewrite":
                    generated = self.backend.rewrite(
                        prompt_prefix, seed=self.seed + i,
                        max_new_tokens=body_token_budget(
                            rewrite_source_span, self.backend.max_new_tokens))
                    candidate.update(generated)
                    parsed = parse_rewrite_response(
                        generated["raw_response"], rewrite_source_span)
                    candidate["rewrite_status"] = parsed["status"]
                    if parsed["status"] == "no_change":
                        raise ValueError("rewrite_no_change")
                    middle = parsed["middle"]
                    candidate["body_trimmed"] = False
                else:
                    recovered_backend_error = False
                    try:
                        generated = self.backend.infill(
                            prompt_prefix, prompt_suffix, seed=self.seed + i,
                            max_new_tokens=body_token_budget(span, self.backend.max_new_tokens))
                    except GenerationError as exc:
                        reply=exc.response
                        if not (self.body_boundary_extraction and str(exc)=='non_code_output' and reply.get('done')
                                and isinstance(reply.get('response'),str)):
                            raise
                        # Raw recovery is provisional: a witnessed boundary and
                        # every gate must still pass before a candidate exists.
                        candidate.update(backend_response=reply,backend_rejection=str(exc))
                        recovered_backend_error=True
                        generated=dict(middle=reply['response'],raw_response=reply['response'],seed=self.seed+i,
                            stop_reason='length' if reply.get('done_reason')=='length' else 'visible_eot' if '<EOT>' in reply['response'] else 'unverified_stop',
                            token_budget=body_token_budget(span,self.backend.max_new_tokens),prompt=prompt_prefix,suffix=prompt_suffix,
                            transport=getattr(self.backend,'transport','unknown'),
                            metrics={key:reply.get(key) for key in ('done_reason','prompt_eval_count','eval_count','total_duration')})
                    candidate.update(generated)
                    middle = generated["middle"]
                    witnessed_body = False
                    if self.body_boundary_extraction:
                        from .body_boundary import extract_body_with_echo
                        try:
                            extracted,boundary=extract_body_with_echo(middle,span['prefix'],
                                _target_function_node(raw.decode('utf-8-sig'),span['symbol']).col_offset,prompt_prefix)
                        except (ValueError,SyntaxError) as exc:
                            candidate['body_extraction']={'status':'not_extracted','reason':str(exc)}
                            if recovered_backend_error:
                                raise ValueError('unsafe_body_boundary: '+str(exc)) from exc
                        else:
                            candidate.update(untrimmed_middle=middle,body_extraction={'status':'extracted',**boundary})
                            middle,witnessed_body=extracted,True
                    elif self.fixed_body_extraction and generated.get("stop_reason") in {"length", "visible_eot", "model_eos"}:
                        from .body_boundary import extract_fixed_body
                        try:
                            extracted, boundary = extract_fixed_body(middle, span["prefix"],
                                _target_function_node(raw.decode("utf-8-sig"), span["symbol"]).col_offset)
                        except (ValueError, SyntaxError) as exc:
                            candidate["body_extraction"] = {"status": "not_extracted", "reason": str(exc)}
                        else:
                            candidate["untrimmed_middle"] = middle
                            candidate["body_extraction"] = {"status": "extracted", **boundary}
                            middle, witnessed_body = extracted, True
                    if generated.get("stop_reason") not in {"visible_eot", "model_eos"} and not witnessed_body:
                        if generation_scope == 'python_expression':
                            raise ValueError('incomplete_expression: expression generation requires model end marker')
                        if self.fixed_signature_body:
                            raise ValueError("incomplete_body: fixed-signature generation requires model end marker")
                        if span.get("include_signature"):
                            raise ValueError("incomplete_function: whole-function generation requires visible_eot")
                        middle = (trim_generated_statement(middle)
                                  if generation_scope == "issue_statement"
                                  else trim_generated_body(middle, span))
                        candidate.update(body_trimmed=True,
                                         trimmed_lines=len(middle.splitlines()))
                    elif (generation_scope not in {"issue_statement", "python_expression"}
                          and not span.get("include_signature")
                          and body_overruns(middle, span)):
                        # Closed by the model, but only after running into the
                        # next function. Cut it back to the body's own end
                        # rather than splicing the overrun in and losing the
                        # candidate to the scope gate.
                        if self.fixed_signature_body:
                            raise ValueError("body_contract_violation: output escaped the fixed function body")
                        middle = trim_generated_body(middle, span)
                        candidate.update(body_trimmed=True, overran_past_stop=True,
                                         trimmed_lines=len(middle.splitlines()))
                    else:
                        candidate["body_trimmed"] = False
                        if generation_scope == "issue_statement":
                            check_single_statement_replacement(middle)
                        elif generation_scope == 'python_expression':
                            from .local_fim import normalize_expression
                            normalized = normalize_expression(middle)
                            candidate['expression_normalized'] = normalized != middle
                            middle = normalized
                if (self.safe_function_extraction and self.generation_mode == "fim"
                        and span.get("include_signature")
                        and generated.get("stop_reason") in {"visible_eot", "model_eos"}):
                    # No synthesized closing code: require a witnessed sibling or
                    # end comment, then rerun every existing gate on the result.
                    from .repair_diagnostics import extract_function
                    try:
                        extracted, boundary = extract_function(middle, span["symbol"].split(".")[-1])
                    except (ValueError, SyntaxError) as exc:
                        candidate["function_extraction"] = {"status": "not_extracted", "reason": str(exc)}
                    else:
                        if extracted != middle:
                            candidate["untrimmed_middle"] = middle
                            middle = extracted
                            candidate["function_extraction"] = {
                                "status": "extracted", "version": "safe-function-extraction-v1", **boundary}
                        else:
                            candidate["function_extraction"] = {"status": "unchanged", **boundary}
                candidate["middle"] = middle
                if generation_scope != 'python_expression' and middle and not middle.endswith("\n") and span["suffix"]:
                    middle += "\n"
                    candidate.update(middle=middle, trailing_newline_added=True)
                if generation_scope != 'python_expression':
                    check_body_retention(middle, span, self.min_body_retention)
                patch, after = build_patch(raw, span, middle, path)
                if self.helper_contracts:
                    from .helper_contracts import check_calls
                    candidate["helper_call_check"] = check_calls(raw.decode("utf-8-sig"),
                        after.decode("utf-8-sig"), span["symbol"])
                from .domain_adapters import tolerance_matches,check_tolerance_change
                tolerance_repair = tolerance_matches(self.domain_adapters,ticket_json,str(row.get('code_text') or ''))
                if tolerance_repair:
                    check_tolerance_change(middle)
                modified_files = [path]
                logic_sources = [(path, after)]
                if caller_target:
                    if tolerance_repair:
                        caller_middle = propagate_tolerance_keywords(
                            caller_target["raw"], caller_target["span"],
                            row.get("symbol_qualified_name"),
                        )
                        caller_generated = {
                            "strategy": "ast_call_keyword_injection",
                            "stop_reason": "host_transform",
                        }
                    else:
                        caller_generated = self.backend.infill(
                            caller_target["prefix"], caller_target["suffix"],
                            seed=self.seed + i + 10_000,
                            max_new_tokens=body_token_budget(
                                caller_target["span"], self.backend.max_new_tokens),
                        )
                        caller_middle = caller_generated["middle"]
                    candidate.update(
                        caller_middle=caller_middle,
                        caller_generation=caller_generated,
                    )
                    if caller_generated.get("stop_reason") not in {
                        "visible_eot", "model_eos", "host_transform"
                    }:
                        caller_middle = trim_generated_body(
                            caller_middle, caller_target["span"])
                    check_body_retention(
                        caller_middle, caller_target["span"],
                        self.min_body_retention,
                    )
                    if caller_middle and not caller_middle.endswith("\n") and caller_target["span"]["suffix"]:
                        caller_middle += "\n"
                        candidate.update(caller_middle=caller_middle, caller_trailing_newline_added=True)
                    if tolerance_repair:
                        compact_caller = re.sub(r"\s+", "", caller_middle)
                        if not (
                            "rtol=self.rtol" in compact_caller
                            and "atol=self.atol" in compact_caller
                        ):
                            raise ValueError("caller_did_not_propagate_tolerance")
                    caller_patch, caller_after_bytes = build_patch(
                        caller_target["raw"], caller_target["span"],
                        caller_middle, caller_target["path"],
                    )
                    patch += caller_patch
                    modified_files.append(caller_target["path"])
                    candidate["caller_after_sha256"] = sha(caller_after_bytes)
                    logic_sources.append((caller_target['path'], caller_after_bytes))
                related_generations = []
                combined_after = [after]
                for target in related_targets:
                    related_generated = self.backend.infill(
                        target["prefix"], target["suffix"],
                        seed=self.seed + i + target["seed_offset"],
                        max_new_tokens=body_token_budget(
                            target["span"], self.backend.max_new_tokens),
                    )
                    related_middle = related_generated["middle"]
                    if related_generated.get("stop_reason") not in {
                        "visible_eot", "model_eos"
                    }:
                        related_middle = trim_generated_body(
                            related_middle, target["span"])
                    check_body_retention(
                        related_middle, target["span"], self.min_body_retention)
                    if related_middle and not related_middle.endswith("\n") and target["span"]["suffix"]:
                        related_middle += "\n"
                        related_generated['trailing_newline_added'] = True
                    related_patch, related_after = build_patch(
                        target["raw"], target["span"], related_middle,
                        target["path"],
                    )
                    patch += related_patch
                    modified_files.append(target["path"])
                    combined_after.append(related_after)
                    logic_sources.append((target['path'], related_after))
                    related_generations.append({
                        "file": target["path"],
                        "function": target["span"]["symbol"],
                        "middle": related_middle,
                        "generation": related_generated,
                        "after_sha256": sha(related_after),
                    })
                if related_generations:
                    candidate["related_generations"] = related_generations
                combined_digest = sha(b"\0".join(combined_after))
                if combined_digest in seen:
                    candidate["reason"] = "duplicate"
                    continue
                seen.add(combined_digest)
                # Recheck snapshot before applying/testing.
                source_for(ticket_json, bug_location, repo_path, row)
                git(repo_path, "apply", "--check", "--whitespace=nowarn", data=patch.encode("utf-8"))
                candidate.update(patch=patch, status="generated", static_status="passed", after_sha256=sha(after),
                                 apply_check_status="passed",
                                 combined_after_sha256=combined_digest,
                                 modified_files=list(dict.fromkeys(modified_files)))
                from .generation_records import logic_fingerprint, LOGIC_VERSION
                candidate.update(logic_sha256=logic_fingerprint(logic_sources),
                                 executable_logic_sha256=logic_fingerprint(logic_sources,executable=True),
                                 executable_fingerprint_version='python-executable-ast-v1',
                                 logic_fingerprint_version=LOGIC_VERSION)
                if self.operation_contract_preview:
                    from .operation_contract import capture
                    diagnostic, preview = capture(raw.decode('utf-8-sig'),after.decode('utf-8-sig'),
                        span['symbol'],ticket_json,prompt_prefix,prompt_suffix,
                        body_token_budget(span,self.backend.max_new_tokens),
                        counter=getattr(self.backend,'_token_counter',None),
                        context_limit=getattr(self.backend,'num_ctx',None),adapters=self.domain_adapters)
                    diagnostic.update(source_file=path,target_symbol=span['symbol'],base_commit=commit,
                                      source_before_sha256=sha(raw),source_after_sha256=sha(after))
                    candidate.update(operation_contract_diagnostic=diagnostic,operation_contract_preview=preview)
                if self.source_none_probe:
                    from .none_contract import assess_none_contract,failure_feedback
                    check=assess_none_contract(raw.decode('utf-8-sig'),after.decode('utf-8-sig'),span['symbol'])
                    candidate['source_contract_check']=check
                    if check['status']=='failed':
                        candidate.update(status='rejected',reason='source_none_regression',failure_feedback=failure_feedback(check),
                            validation={'status':'failed','reason':'source_none_regression','kind':'synthetic_source_none_probe','official_resolved':None})
                        continue
                from .source_api_integration import check_candidate
                if not check_candidate(self,candidate,patch):continue
                if self.validator:
                    report = self.validator(patch, repo_path)
                    candidate["validation"] = report
                    if report.get("status") != "plausible":
                        candidate.update(status="rejected", reason="validation_failed")
                        continue
                eligible.append(candidate)
            except (ValueError, OSError, subprocess.SubprocessError, SyntaxError) as exc:
                candidate["reason"] = str(exc)
                if isinstance(exc, GenerationError):
                    candidate["backend_response"] = exc.response
        if any(candidate.get("apply_check_status") == "passed"
               for candidate in result["candidates"]):
            result["apply_status"] = "passed"
        if eligible:
            chosen = min(eligible, key=lambda c: (changed_line_count(c["patch"]),
                                                 c["candidate_id"]))
            result.update(patch_status="generated", patch=chosen["patch"],
                          apply_status="passed",
                          modified_files=chosen.get("modified_files", [path]),
                          selected_candidate_id=chosen["candidate_id"], requires_manual_patch=False,
                          validation_status="plausible" if self.validator else "not_run",
                          explanation=(
                              "Rewrite body rebuilt into a host-generated diff; review before merging."
                              if self.generation_mode == "rewrite" else
                              "FIM body rebuilt into a host-generated diff; review before merging."))
        else:
            result.update(patch_status="no_valid_candidate", explanation="No candidate passed the required gates")
        from .generation_records import annotate_generation
        return annotate_generation(result)
