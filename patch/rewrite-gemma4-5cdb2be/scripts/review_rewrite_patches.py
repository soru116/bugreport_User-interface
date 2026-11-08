"""Offline review of the patches a run generated. Zero model calls.

Two jobs:

1. A compact summary of every generated patch, so a batch can be read in one
   pass instead of opening one file per ticket.

2. Cheap diff-only flags for the shapes that have bitten this project before:
   a change that touches only comments or docstrings, a change to the def line,
   and an added statement that directly follows a return at the same indent
   inside the same hunk.

WHAT THIS DOES NOT DO. STAGE4_METHOD2_REWRITE_EXPERIMENT_ZH.md asked for a gate
against unreachable additions, after finding candidates whose only change was a
statement appended once every branch of an if/elif/else had already returned.
That gate cannot be built from a diff: the reason such a line is unreachable is
that every branch above it returns, and a unified diff carries three lines of
context, not the whole chain. It needs the patched file's AST. `add_after_return`
below catches only the case visible inside one hunk, so a clean report here is
neither that gate nor evidence that a patch is correct.

Usage, from the repository root:
    python review_rewrite_patches.py runs/rewrite36-gemma4-12b-20261024
    python review_rewrite_patches.py runs/rewrite36-gemma4-12b-20261024 -o report.md
"""
import argparse
import io
import re
from pathlib import Path

TERMINATORS = ("return", "raise", "continue", "break", "pass")
DEF_RE = re.compile(r"^\s*(async\s+)?def\s")
STRING_START = ('"""', "'''", '"', "'", 'r"""', "r'''", 'f"""', "f'''")


def indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


def is_blank(text: str) -> bool:
    return not text.strip()


def is_comment(text: str) -> bool:
    return text.strip().startswith("#")


def is_string_line(text: str) -> bool:
    stripped = text.strip()
    return any(stripped.startswith(p) for p in STRING_START)


def is_terminator(text: str) -> bool:
    stripped = text.strip()
    for word in TERMINATORS:
        if stripped == word or stripped.startswith(word + " ") or stripped.startswith(word + "("):
            return True
    return False


def parse_hunks(diff_text: str):
    """Yield (file_path, [(kind, text)]) per hunk. kind is ' ', '+' or '-'."""
    path = None
    hunk = None
    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            path = raw[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            continue
        if raw.startswith("--- ") or raw.startswith("diff --git"):
            continue
        if raw.startswith("@@"):
            if hunk:
                yield path, hunk
            hunk = []
            continue
        if hunk is None:
            continue
        if raw[:1] in (" ", "+", "-"):
            hunk.append((raw[0], raw[1:]))
        elif raw == "":
            hunk.append((" ", ""))
    if hunk:
        yield path, hunk


def add_after_return(hunk):
    """Added code lines whose nearest same-indent sibling inside this hunk is a
    terminator. Narrow by construction -- see the module docstring."""
    after = [(kind, text) for kind, text in hunk if kind in (" ", "+")]
    flagged = []
    for i, (kind, text) in enumerate(after):
        if kind != "+" or is_blank(text) or is_comment(text) or is_string_line(text):
            continue
        here = indent_of(text)
        for j in range(i - 1, -1, -1):
            prev = after[j][1]
            if is_blank(prev) or is_comment(prev):
                continue
            prev_indent = indent_of(prev)
            if prev_indent < here:
                break          # we are inside prev's block, so reachable
            if prev_indent > here:
                continue       # deeper sibling detail, keep looking up
            if is_terminator(prev):
                flagged.append(text.rstrip())
            break
    return flagged


def review_diff(diff_text: str) -> dict:
    added, removed, files = [], [], []
    after_return = []
    for path, hunk in parse_hunks(diff_text):
        if path and path not in files:
            files.append(path)
        for kind, text in hunk:
            if kind == "+":
                added.append(text)
            elif kind == "-":
                removed.append(text)
        after_return.extend(add_after_return(hunk))

    def meaningful(lines):
        return [l for l in lines if not is_blank(l) and not is_comment(l) and not is_string_line(l)]

    flags = []
    if not meaningful(added) and not meaningful(removed):
        flags.append("COMMENTS_OR_DOCSTRING_ONLY")
    if after_return:
        flags.append(f"ADD_AFTER_RETURN x{len(after_return)}")
    if any(DEF_RE.match(l) for l in added + removed):
        flags.append("TOUCHES_DEF_LINE")
    if added and not removed:
        flags.append("PURE_ADDITION")
    if removed and not added:
        flags.append("PURE_DELETION")
    return {
        "files": files,
        "added": added,
        "removed": removed,
        "after_return": after_return,
        "flags": flags,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="a run directory containing tickets/<id>/selected.diff")
    parser.add_argument("-o", "--output", type=Path, default=None)
    args = parser.parse_args()

    diffs = sorted(args.run.glob("tickets/*/selected.diff"))
    diffs = [p for p in diffs if p.stat().st_size > 0]
    if not diffs:
        print(f"no non-empty selected.diff under {args.run}")
        return 1

    out = [f"# Patch review: {args.run.as_posix()}", "", f"{len(diffs)} non-empty patches.", ""]
    tally = {}
    rows = []
    for path in diffs:
        ticket = path.parent.name
        text = io.open(path, "r", encoding="utf-8", newline="").read()
        info = review_diff(text)
        for f in info["flags"]:
            key = f.split(" x")[0]
            tally[key] = tally.get(key, 0) + 1
        rows.append((ticket, len(info["added"]), len(info["removed"]),
                     ", ".join(info["flags"]) or "-"))
        out.append(f"## {ticket}")
        out.append("")
        out.append(f"- files: {', '.join(info['files']) or '?'}")
        out.append(f"- added {len(info['added'])} / removed {len(info['removed'])}")
        out.append(f"- flags: {', '.join(info['flags']) or 'none'}")
        if info["after_return"]:
            out.append("- added directly after a return at the same indent:")
            for line in info["after_return"]:
                out.append(f"      {line}")
        out.append("")
        out.append("```diff")
        out.append(text.rstrip("\n"))
        out.append("```")
        out.append("")

    summary = ["", "## Flag tally", ""]
    for key in sorted(tally):
        summary.append(f"- {key}: {tally[key]}")
    summary.append("")
    summary.append("| ticket | +added | -removed | flags |")
    summary.append("|---|---:|---:|---|")
    for ticket, a, r, f in rows:
        summary.append(f"| {ticket} | {a} | {r} | {f} |")
    summary.append("")
    summary.append("No flags does not mean correct. This script reads only the diff, so it")
    summary.append("cannot see whether an added line is reachable, whether the change")
    summary.append("addresses the issue, or whether the tests pass. Those need a human and")
    summary.append("then official_resolved from the SWE-bench harness.")

    text = "\n".join(out[:4] + summary + out[4:]) + "\n"
    if args.output:
        io.open(args.output, "w", encoding="utf-8", newline="\n").write(text)
        print(f"wrote {args.output}")
    print("\n".join(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
