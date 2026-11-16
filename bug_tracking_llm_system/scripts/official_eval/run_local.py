#!/usr/bin/env python3
"""Run the official SWE-bench harness locally on a workspace patch.

Runs inside the runner container (see official_eval.ps1), cwd = bug_tracking_llm_system.

  python scripts/official_eval/run_local.py --ticket astropy__astropy-7336
  python scripts/official_eval/run_local.py --instance-id astropy__astropy-7336 --patch-file x.diff
  python scripts/official_eval/run_local.py --instance-id astropy__astropy-7336 --gold

The patch comes only from generated output (workspace patch stage or a file).
Ground truth (data/official_eval/instances.jsonl) is read only by the harness,
after generation. Results:

  data/official_eval/runs/<run_id>/verdict.json      full verdict incl. per-test status
  data/official_eval/local_verdicts.jsonl            one line per run
  data/workspace/artifacts/<ticket>/official_local/<run_id>.json
                                                     summary only (no test names), so
                                                     nothing from the official tests can
                                                     flow back into a later generation prompt
Status values: resolved / unresolved / patch_apply_failed / harness_error / dry_run.
harness_error is never an "unresolved" verdict.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import re
import subprocess
import sys

MODEL_NAME = "bugflow-workspace"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _write_json(path: pathlib.Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _append_jsonl(path: pathlib.Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(data, ensure_ascii=False) + "\n")


def load_instances(eval_root: pathlib.Path) -> dict:
    path = eval_root / "instances.jsonl"
    if not path.exists():
        raise SystemExit(f"{path} not found. Run 'official_eval.ps1 fetch' first.")
    rows = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            rows[row["instance_id"]] = row
    return rows


def patch_from_ticket(workspace: pathlib.Path, key: str) -> tuple[str, dict]:
    """Latest generated patch of a workspace ticket (never the reference patch)."""
    ticket_path = workspace / "tickets" / f"{key}.json"
    output = {}
    if ticket_path.exists():
        ticket = json.loads(ticket_path.read_text(encoding="utf-8"))
        output = (ticket.get("stages", {}).get("patch") or {}).get("output") or {}
    if not output.get("patch"):
        result_path = workspace / "artifacts" / key / "patch_result.json"
        if result_path.exists():
            output = json.loads(result_path.read_text(encoding="utf-8")).get("output") or {}
    patch = output.get("patch") or ""
    if not patch.strip():
        raise SystemExit(f"Ticket {key} has no generated patch "
                         f"(patch_status={output.get('patch_status')!r}).")
    info = {
        "patch_source": "workspace_patch_stage",
        "workspace_patch_sha256": output.get("patch_sha256"),
        "generated_at": output.get("generated_at"),
        "workspace_run_dir": output.get("run_dir"),
    }
    return patch, info


def summarize_report(report: dict, iid: str) -> dict:
    entry = report.get(iid, {})
    tests = entry.get("tests_status") or {}

    def count(group: str, kind: str) -> int:
        return len((tests.get(group) or {}).get(kind) or [])

    return {
        "resolved": bool(entry.get("resolved")),
        "patch_successfully_applied": bool(entry.get("patch_successfully_applied")),
        "fail_to_pass": {"passed": count("FAIL_TO_PASS", "success"),
                         "failed": count("FAIL_TO_PASS", "failure")},
        "pass_to_pass": {"passed": count("PASS_TO_PASS", "success"),
                         "failed": count("PASS_TO_PASS", "failure")},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--ticket", help="Workspace ticket key (= SWE-bench instance id)")
    src.add_argument("--patch-file", help="Unified diff to evaluate (needs --instance-id)")
    src.add_argument("--gold", action="store_true",
                     help="Environment sanity check with the reference patch (needs --instance-id)")
    ap.add_argument("--instance-id")
    ap.add_argument("--eval-root", default="data/official_eval")
    ap.add_argument("--workspace", default="data/workspace")
    ap.add_argument("--timeout", type=int, default=1800, help="Same as the GitHub Actions workflow")
    ap.add_argument("--dry-run", action="store_true",
                    help="Write predictions and print the harness command without running it")
    args = ap.parse_args()

    eval_root = pathlib.Path(args.eval_root).resolve()
    workspace = pathlib.Path(args.workspace).resolve()
    instances = load_instances(eval_root)

    iid = args.ticket or args.instance_id
    if not iid:
        ap.error("--instance-id is required with --patch-file/--gold")
    if iid not in instances:
        raise SystemExit(f"{iid} is not in {eval_root / 'instances.jsonl'}. "
                         "Add it with: official_eval.ps1 fetch -Ids <id>")

    started = _now()
    short = re.sub(r"[^A-Za-z0-9.-]+", "-", iid.split("__", 1)[-1])
    kind = "gold" if args.gold else "model"
    run_id = f"local-{kind}-{short}-{started:%Y%m%d-%H%M%S}"
    run_dir = eval_root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    patch_info: dict = {}
    if args.gold:
        predictions_arg = "gold"
        model_name = "gold"
        patch_sha = None
        patch_info = {"patch_source": "reference_patch_sanity_check"}
    else:
        if args.ticket:
            patch, patch_info = patch_from_ticket(workspace, args.ticket)
        else:
            patch = pathlib.Path(args.patch_file).read_text(encoding="utf-8")
            patch_info = {"patch_source": "file", "patch_file": str(args.patch_file)}
        if "\r\n" in patch:
            patch_info["warning"] = "patch contains CRLF line endings"
        if not patch.endswith("\n"):
            patch += "\n"
            patch_info["trailing_newline_added"] = True
        patch_sha = hashlib.sha256(patch.encode("utf-8")).hexdigest()
        model_name = MODEL_NAME
        with open(run_dir / "predictions.jsonl", "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps({"instance_id": iid, "model_name_or_path": model_name,
                                 "model_patch": patch}, ensure_ascii=False) + "\n")
        predictions_arg = str(run_dir / "predictions.jsonl")

    cmd = [sys.executable, "-m", "swebench.harness.run_evaluation",
           "--dataset_name", str(eval_root / "instances.jsonl"),
           "--split", "test",
           "--predictions_path", predictions_arg,
           "--instance_ids", iid,
           "--max_workers", "1",
           "--timeout", str(args.timeout),
           "--run_id", run_id]

    try:
        import swebench
        swebench_version = swebench.__version__
    except ImportError:
        swebench_version = None

    verdict = {
        "run_id": run_id,
        "kind": "gold_sanity_check" if args.gold else "model_patch",
        "instance_id": iid,
        "ticket": args.ticket,
        "patch_sha256": patch_sha,
        **patch_info,
        "image": instances[iid].get("image"),
        "evaluator": "local swebench harness (runner container, Docker Desktop)",
        "swebench_version": swebench_version,
        "started_at": started.isoformat(timespec="seconds"),
    }

    if args.dry_run:
        print("Would run (cwd = %s):\n  %s" % (run_dir, " ".join(cmd)))
        verdict["status"] = "dry_run"
        _write_json(run_dir / "verdict.json", verdict)
        return 0

    log_path = run_dir / "harness_stdout.txt"
    with open(log_path, "w", encoding="utf-8", newline="\n") as log:
        proc = subprocess.run(cmd, cwd=run_dir, stdout=log, stderr=subprocess.STDOUT, text=True)
    verdict["harness_exit_code"] = proc.returncode
    verdict["finished_at"] = _now().isoformat(timespec="seconds")

    instance_log_dir = run_dir / "logs" / "run_evaluation" / run_id / model_name / iid
    report_path = instance_log_dir / "report.json"
    verdict["log_dir"] = str(instance_log_dir.relative_to(eval_root))
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        summary = summarize_report(report, iid)
        verdict.update(summary)
        verdict["tests_status"] = (report.get(iid) or {}).get("tests_status")
        if summary["resolved"]:
            verdict["status"] = "resolved"
        elif not summary["patch_successfully_applied"]:
            verdict["status"] = "patch_apply_failed"
        else:
            verdict["status"] = "unresolved"
    else:
        verdict["status"] = "harness_error"
        verdict["resolved"] = None
        verdict["error"] = f"report.json not produced; see {log_path.relative_to(eval_root)}"

    _write_json(run_dir / "verdict.json", verdict)
    line = {k: verdict.get(k) for k in ("run_id", "kind", "instance_id", "ticket", "patch_sha256",
                                       "status", "resolved", "fail_to_pass", "pass_to_pass",
                                       "swebench_version", "finished_at")}
    _append_jsonl(eval_root / "local_verdicts.jsonl", line)

    if args.ticket and not args.gold:
        public = {k: v for k, v in line.items() if k != "kind"}
        public["verdict_file"] = str((run_dir / "verdict.json").relative_to(eval_root.parent.parent))
        _write_json(workspace / "artifacts" / args.ticket / "official_local" / f"{run_id}.json", public)

    print(f"{iid}: {verdict['status']}"
          + (f"  F2P {verdict['fail_to_pass']}  P2P {verdict['pass_to_pass']}"
             if verdict.get("fail_to_pass") else ""))
    print(f"verdict: {run_dir / 'verdict.json'}")
    return 0 if verdict["status"] != "harness_error" else 1


if __name__ == "__main__":
    sys.exit(main())
