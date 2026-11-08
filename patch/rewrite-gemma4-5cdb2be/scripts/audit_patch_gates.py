"""Would the gate chain have accepted this patch? Run before spending a harness run.

The gate chain in ``fim_patch.generator`` runs while a candidate is being built.
A patch that arrived some other way -- a different generation script, a
hand-written fix, a teammate's batch on another branch -- has never met it. This
script closes that gap: give it a ``predictions.jsonl`` and it reports, per
instance, which gates the patch would have failed and why.

It exists because of three patches in ``reports/model-only-patches-20260929/``.
Two of them were already known to be broken, and both are caught by gates that
have been on ``wp2-target-selection`` the whole time:

    astropy__astropy-13572   undefined_name: units
    astropy__astropy-13465   the signature changed, and self.rtol / self.atol
                             are still read 18 times in the same file

Finding that out cost seconds here. Finding it out from the official harness
costs a container build and a workflow run per instance, and the verdict that
comes back is ``not resolved`` with no reason attached.

What this script cannot tell you is whether a patch that passes every gate is
correct. Nothing static can: a patch that deletes one load-bearing character
from a regular expression parses, changes executable code, and applies cleanly,
and it makes the function raise on every call. ``passes_gates`` means "worth
spending a harness run on", never "resolved".

ANALYSIS ONLY, and it runs after generation, never inside it. The commits file
is read for ``instance_id``, ``repo`` and ``base_commit`` and for nothing else:
if it also holds reference patches or test lists, those fields are not touched
here and could not reach a prompt from here in any case, because this script
never builds one.

Usage::

    python scripts/audit_patch_gates.py \
        --predictions reports/model-only-patches-20260929/predictions.jsonl \
        --commits data/fault_localization/swebench_full/test_gold.jsonl \
        --repo-cache-dir ../data/repositories
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for _extra in (REPO_ROOT / "src", Path(__file__).resolve().parent):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from fim_patch import generator as G  # noqa: E402
from utils.repo_snapshot import (  # noqa: E402
    RepoSnapshotError,
    materialize_commit_snapshot,
    resolve_repository_path,
)
from audit_stage4_target_vs_gold import enclosing_symbols  # noqa: E402

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def changed_lines(patch: str) -> dict[str, set[int]]:
    """Old-file line numbers a hunk actually CHANGES, per file.

    Not the same as ``audit_stage4_target_vs_gold.patched_lines``, which returns
    every line in the hunk's range -- context included. Three lines of context
    are enough to reach into the next function, and then this audit ran the gates
    on a function the patch never touched: ``write`` alongside ``get_writer`` in
    13073, ``FITSDiff._diff`` alongside ``FITSDiff._report`` in 13465. In that
    function the before and after are identical, so ``check_executable_change``
    reported "only comments or docstrings differ" -- a rejection describing a
    function nobody had edited.

    The other tool is deliberately left alone. Its published hit/miss numbers
    were measured with the wider definition, and quietly changing what they mean
    would be worse than the narrow duplication here. Whether it has the same
    over-inclusion is a separate question, on a separate metric.

    A hunk that only inserts has no removed line to name, so the line before the
    insertion point is used instead: it is inside the function receiving the
    insertion, which is what the caller needs.
    """

    files: dict[str, set[int]] = {}
    path = None
    old_line = 0
    in_hunk = False
    saw_removal = False
    insert_anchor = 0
    for line in patch.splitlines():
        if line.startswith("--- a/"):
            path, in_hunk = line[6:].strip(), False
        elif line.startswith("--- "):
            path, in_hunk = None, False
        elif line.startswith("+++"):
            continue
        elif line.startswith("@@"):
            match = HUNK.match(line)
            in_hunk = bool(match) and path is not None
            if in_hunk:
                old_line = int(match.group(1))
                saw_removal = False
                insert_anchor = max(old_line - 1, 1)
        elif in_hunk and path:
            if line.startswith("-"):
                files.setdefault(path, set()).add(old_line)
                saw_removal = True
                old_line += 1
            elif line.startswith("+"):
                if not saw_removal:
                    files.setdefault(path, set()).add(insert_anchor)
            elif line.startswith("\\"):
                continue
            else:
                insert_anchor = old_line
                old_line += 1
    return files

#: Where the batch runner keeps detached checkouts, reused here rather than
#: rebuilt. ``git show COMMIT:path`` against the cached clone is not enough: a
#: clone fetched for one ticket set does not necessarily carry another ticket's
#: base commit, and two of the first three instances audited failed exactly
#: that way.
SNAPSHOT_SUBDIR = "_stage4_snapshots"

def read_source(path: Path) -> str:
    """Read without letting Python translate line endings."""

    with open(path, encoding="utf-8-sig", newline="") as handle:
        return handle.read()


def to_lf(text: str) -> str:
    """One line-ending convention for everything the gates see.

    Pinning ``core.autocrlf`` and ``core.eol`` on the ``git apply`` call was the
    first attempt at this and it did not hold: on Windows the applied text still
    came back CRLF, and the bug was invisible on Linux, so a Linux test run
    "confirming" the fix confirmed nothing. Normalizing here does not depend on
    any git configuration, gitattributes file, or platform default.

    Nothing is lost by it. Every gate reasons over ``ast`` or ``symtable``, both
    of which ignore line endings, and the audit only ever compares the before and
    after of the same file -- so the only requirement is that both sides use the
    same convention, not which one it is.
    """

    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_json(path: Path):
    """Parse a run artifact, treating an unreadable one as absent.

    A run directory that was interrupted can hold a truncated candidates.json.
    Harvesting must skip it rather than end the audit of the other 23 runs.
    """

    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def harvest_runs(directories: list[Path]) -> tuple[list[dict], dict]:
    """Every candidate a past run recorded as clearing its gates.

    The batch runner writes one prediction per ticket -- the candidate it
    selected -- so a predictions file shows a fraction of what a run produced.
    The taxonomy counted 81 candidates across 24 runs that cleared every gate,
    and the other 75 of them have never been looked at.

    Most gates will pass again by construction: they ran while the candidate was
    being built. The point is the two that did not exist then, ``orphaned_attribute``
    and the outside-body check, and any candidate whose run predates a gate.

    Identical patches are collapsed. Runs share tickets and settings, so the same
    diff is generated repeatedly, and counting it once per run would inflate
    every number here.
    """

    harvested: dict[tuple[str, str], dict] = {}
    duplicates = 0
    for directory in directories:
        tickets = directory / "tickets"
        if not tickets.is_dir():
            continue
        for ticket_dir in sorted(p for p in tickets.iterdir() if p.is_dir()):
            rows = read_json(ticket_dir / "candidates.json")
            for candidate in rows if isinstance(rows, list) else []:
                patch = str(candidate.get("patch") or "")
                if candidate.get("status") != "generated" or not patch.strip():
                    continue
                key = (ticket_dir.name, hashlib.sha256(patch.encode()).hexdigest())
                if key in harvested:
                    harvested[key]["seen_in"].append(directory.name)
                    duplicates += 1
                    continue
                harvested[key] = {
                    "instance_id": ticket_dir.name,
                    "model_patch": patch,
                    "seen_in": [directory.name],
                    "candidate_id": candidate.get("candidate_id"),
                }
    rows = [harvested[key] for key in sorted(harvested)]
    return rows, {"distinct_patches": len(rows), "collapsed_duplicates": duplicates}


#: The only fields read from the commits file.
COMMIT_FIELDS = ("instance_id", "repo", "base_commit")


def load_commits(path: Path) -> dict[str, dict]:
    """instance_id -> {repo, base_commit}, and nothing else from the row."""

    table = {}
    with path.open(encoding="utf-8-sig") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            key = row.get("instance_id") or row.get("ticket_id")
            if key:
                table[key] = {"repo": row.get("repo"),
                              "base_commit": row.get("base_commit")}
    return table


def snapshot_for(commit: dict, cache: Path, *, fetch: bool) -> Path:
    """A checkout of this instance's base commit, fetching it if the clone lacks it."""

    ticket = {"repo": commit.get("repo"), "base_commit": commit.get("base_commit")}
    source = resolve_repository_path(ticket, repo_cache_dir=cache, clone_missing=False)
    return Path(materialize_commit_snapshot(
        source, repo=str(ticket["repo"]), base_commit=str(ticket["base_commit"]),
        snapshot_cache_dir=cache / SNAPSHOT_SUBDIR, fetch_missing_commits=fetch))


def apply_patch(before: dict[str, str], patch: str) -> dict[str, str] | str:
    """Apply the diff to the pre-patch sources, out of tree.

    ``git apply`` rather than a hand-rolled hunk applier: a patch that does not
    apply is itself a verdict, and reimplementing that judgement would be a
    second thing to get wrong.
    """

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for relative, text in before.items():
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(to_lf(text), encoding="utf-8", newline="")
        diff = to_lf(patch)
        (root / "_audit.diff").write_text(diff if diff.endswith("\n") else diff + "\n",
                                          encoding="utf-8", newline="")
        # core.autocrlf / core.eol are pinned off as well, so the diff applies
        # against what was written rather than against a converted copy. The
        # guarantee that the gates see one convention is to_lf, not this config:
        # see its docstring for why the config alone was not enough.
        done = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.eol=lf",
                               "apply", "--unsafe-paths", "_audit.diff"],
                              cwd=root, capture_output=True)
        if done.returncode:
            return "git_apply_failed: " + done.stderr.decode(errors="replace").strip()
        return {relative: to_lf(read_source(root / relative)) for relative in before}


def signature_changed(before: str, after: str, qualified_name: str) -> bool:
    """Did anything outside the function body move?

    The generator can only ever replace a body, so a candidate that changes a
    signature, a decorator or a default is one it could not have produced. Our
    own pipeline rejects it by comparing the function node with its body blanked
    out; the same comparison is what catches 13465 here.
    """

    before_node = copy.deepcopy(G._target_function_node(before, qualified_name))
    after_node = copy.deepcopy(G._target_function_node(after, qualified_name))
    before_node.body = [ast.Pass()]
    after_node.body = [ast.Pass()]
    return (ast.dump(before_node, include_attributes=False)
            != ast.dump(after_node, include_attributes=False))


def _self_attribute_writes(node) -> set[str]:
    names = set()
    for child in ast.walk(node):
        if not isinstance(child, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            continue
        targets = child.targets if isinstance(child, ast.Assign) else [child.target]
        for target in targets:
            if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                    and target.value.id == "self"):
                names.add(target.attr)
    return names


def orphaned_attributes(before: str, after: str, qualified_name: str) -> list[str]:
    """Attributes the patch stopped assigning that the rest of the file still reads.

    This is the 13465 failure generalized. Removing ``self.rtol = rtol`` is
    legal Python, changes executable code, and applies cleanly; every later
    ``self.rtol`` then raises ``AttributeError``. No existing gate looks for it,
    because every existing gate reasons about the target function alone.
    """

    before_node = G._target_function_node(before, qualified_name)
    after_node = G._target_function_node(after, qualified_name)
    dropped = _self_attribute_writes(before_node) - _self_attribute_writes(after_node)
    if not dropped:
        return []
    after_tree = ast.parse(after)
    still_read = set()
    for child in ast.walk(after_tree):
        if (isinstance(child, ast.Attribute) and isinstance(child.value, ast.Name)
                and child.value.id == "self" and child.attr in dropped
                and isinstance(child.ctx, ast.Load)):
            still_read.add(child.attr)
    return sorted(still_read)


def run_gates(before: str, after: str, qualified_name: str) -> list[dict]:
    """Every gate's verdict, not just the first failure.

    Stopping at the first rejection is right during generation and wrong during
    an audit: a patch that trips three gates is a different kind of wrong from
    one that trips a single retention floor, and the taxonomy needs both.
    """

    verdicts = []

    # Resolve the target first. Every gate but one calls _target_function_node,
    # which raises ValueError when a dotted name does not match exactly one
    # function -- and ValueError is also how a gate says "this patch is bad". Left
    # conflated, a nested function the resolver cannot pin down was reported as
    # four separate patch defects, which is how this audit first claimed 14 of 37
    # patches were rejected. An analysis failure is not a verdict on the patch.
    resolution_error = None
    for label, text in (("before", before), ("after", after)):
        try:
            G._target_function_node(text, qualified_name)
        except (ValueError, SyntaxError) as exc:
            resolution_error = f"cannot analyse {qualified_name} in the {label} text: {exc}"
            break

    def record(name, fn):
        try:
            fn()
            verdicts.append({"gate": name, "verdict": "pass"})
        except ValueError as exc:
            verdicts.append({"gate": name, "verdict": "reject", "reason": str(exc)})
        except Exception as exc:  # noqa: BLE001 - an audit must not stop on one gate
            verdicts.append({"gate": name, "verdict": "error",
                             "reason": f"{type(exc).__name__}: {exc}"})

    def outside_body():
        if signature_changed(before, after, qualified_name):
            raise ValueError("signature_changed: the patch edits more than the body, "
                             "which this generator cannot produce")

    def attributes():
        orphans = orphaned_attributes(before, after, qualified_name)
        if orphans:
            raise ValueError("orphaned_attribute: stopped assigning self."
                             + ", self.".join(orphans)
                             + " while the file still reads it")

    # This one reasons over whole-file statements, so it stands whether or not the
    # target can be pinned down.
    record("check_executable_change", lambda: G.check_executable_change(before, after))
    if resolution_error is not None:
        verdicts.append({"gate": "target_resolution", "verdict": "error",
                         "reason": resolution_error})
        return verdicts
    record("outside_body_equal", outside_body)
    record("check_new_undefined_names",
           lambda: G.check_new_undefined_names(before, after, qualified_name))
    record("check_statement_reorder_only",
           lambda: G.check_statement_reorder_only(before, after, qualified_name))
    record("check_reachable", lambda: G.check_reachable(before, after, qualified_name))
    record("orphaned_attribute", attributes)
    return verdicts


def audit_one(instance_id: str, patch: str, commit: dict, cache: Path,
              *, fetch: bool = False) -> dict:
    record = {"instance_id": instance_id, "repo": commit.get("repo"),
              "base_commit": commit.get("base_commit")}
    if not commit.get("repo") or not commit.get("base_commit"):
        return dict(record, verdict="unknown",
                    error="the commits file has no repo/base_commit for this instance")
    touched = changed_lines(patch)
    if not touched:
        return dict(record, verdict="unknown", error="the patch names no files")
    try:
        snapshot = snapshot_for(commit, cache, fetch=fetch)
    except (RepoSnapshotError, ValueError, OSError) as exc:
        return dict(record, verdict="unknown", error=f"snapshot: {exc}")

    before = {}
    for relative in touched:
        source = snapshot / relative
        if not source.is_file():
            return dict(record, verdict="unknown",
                        error=f"{relative} is not in the snapshot at "
                              f"{str(commit['base_commit'])[:8]}")
        before[relative] = to_lf(read_source(source))

    after = apply_patch(before, patch)
    if isinstance(after, str):
        return dict(record, verdict="rejected", files=sorted(touched),
                    findings=[{"gate": "git_apply", "verdict": "reject", "reason": after}])

    findings = []
    symbols = []
    for relative, lines in sorted(touched.items()):
        try:
            ast.parse(after[relative])
        except SyntaxError as exc:
            findings.append({"file": relative, "gate": "ast_parse", "verdict": "reject",
                             "reason": f"unparseable: {exc}"})
            continue
        names = sorted(enclosing_symbols(before[relative], lines))
        if not names:
            findings.append({"file": relative, "gate": "target_resolution",
                             "verdict": "error",
                             "reason": "the patch edits no function body "
                                       "(module level, class body, or imports)"})
            continue
        for name in names:
            symbols.append(f"{relative}::{name}")
            for verdict in run_gates(before[relative], after[relative], name):
                findings.append(dict(verdict, file=relative, symbol=name))

    rejected = [f for f in findings if f["verdict"] == "reject"]
    errored = [f for f in findings if f["verdict"] == "error"]
    if rejected:
        verdict = "rejected"
    elif errored:
        # Not "passes_gates": a gate that could not run has not approved anything.
        verdict = "unknown"
    else:
        verdict = "passes_gates"
    record.update(files=sorted(touched), symbols=symbols, findings=findings,
                  verdict=verdict, rejected_by=[f["gate"] for f in rejected],
                  not_analysed_by=[f["gate"] for f in errored])
    return record


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--predictions", type=Path, default=None,
                        help="JSONL with instance_id and model_patch per row")
    parser.add_argument("--run", type=Path, action="append", default=[],
                        help="Audit every gate-clearing candidate in this run "
                             "directory rather than one prediction per ticket. "
                             "Repeatable.")
    parser.add_argument("--runs-root", type=Path, default=None,
                        help="Harvest candidates from every subdirectory of this path")
    parser.add_argument("--commits", type=Path, required=True,
                        help="JSONL supplying repo and base_commit per instance_id")
    parser.add_argument("--repo-cache-dir", type=Path, required=True)
    parser.add_argument("--fetch-missing-commits", action="store_true",
                        help="Let the snapshot step fetch a base commit the local "
                             "clone does not carry (needs network)")
    parser.add_argument("--output", type=Path, default=None,
                        help="Write the full findings as JSON here")
    args = parser.parse_args(argv)

    if not args.predictions and not args.run and not args.runs_root:
        parser.error("pass --predictions, --run, or --runs-root")
    commits = load_commits(args.commits)
    rows = []
    harvest_note = {}
    if args.predictions:
        with args.predictions.open(encoding="utf-8-sig") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    directories = list(args.run)
    if args.runs_root:
        directories += sorted(p for p in args.runs_root.iterdir() if p.is_dir())
    if directories:
        harvested, harvest_note = harvest_runs(directories)
        rows.extend(harvested)
    if not rows:
        print("Nothing to audit.")
        return 2

    if harvest_note:
        print(f"{harvest_note['distinct_patches']} distinct gate-clearing patch(es) "
              f"harvested from {len(directories)} run director(ies); "
              f"{harvest_note['collapsed_duplicates']} repeat(s) collapsed")
    print(f"{len(rows)} patch(es) to audit; gates from fim_patch.generator\n")
    audited = []
    for row in rows:
        instance_id = row.get("instance_id") or ""
        patch = row.get("model_patch") or row.get("patch") or ""
        commit = commits.get(instance_id, {})
        result = audit_one(instance_id, patch, commit, args.repo_cache_dir,
                           fetch=args.fetch_missing_commits)
        audited.append(result)
        mark = {"rejected": "REJECT ", "passes_gates": "PASS   ",
                "unknown": "UNKNOWN"}[result["verdict"]]
        print(f"{mark} {instance_id}"
              + (f"  [{', '.join(row['seen_in'][:3])}"
                 + (f" +{len(row['seen_in']) - 3}" if len(row["seen_in"]) > 3 else "")
                 + f" #{row.get('candidate_id')}]" if row.get("seen_in") else ""))
        if result.get("error"):
            print(f"         {result['error']}")
        for name in result.get("symbols", []):
            print(f"         target {name}")
        for finding in result.get("findings", []):
            if finding["verdict"] != "pass":
                where = f" [{finding.get('symbol') or finding.get('file')}]"
                print(f"         {finding['verdict']}: {finding['gate']}{where}")
                print(f"           {finding.get('reason', '')}")
        print()

    counts = {"rejected": 0, "passes_gates": 0, "unknown": 0}
    for result in audited:
        counts[result["verdict"]] += 1
    summary = {
        "predictions": len(audited),
        "harvest": harvest_note or None,
        "rejected": counts["rejected"],
        "passes_gates": counts["passes_gates"],
        "unknown": counts["unknown"],
        "worth_a_harness_run": [r["instance_id"] for r in audited
                                if r["verdict"] == "passes_gates"],
        "caveat": ("passes_gates means no static gate objects. It is not "
                   "plausible, and it is not resolved. Only the official harness "
                   "decides that."),
        "results": audited,
    }
    print(f"rejected {counts['rejected']} | passes gates {counts['passes_gates']} "
          f"| unknown {counts['unknown']}")
    worth = summary["worth_a_harness_run"]
    if worth:
        by_ticket = {}
        for instance_id in worth:
            by_ticket[instance_id] = by_ticket.get(instance_id, 0) + 1
        print(f"Worth a harness run: {len(worth)} patch(es) over "
              f"{len(by_ticket)} ticket(s) -- "
              + ", ".join(f"{k} x{v}" if v > 1 else k for k, v in sorted(by_ticket.items())))
    else:
        print("Nothing here is worth a harness run yet.")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        print(f"Findings: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
