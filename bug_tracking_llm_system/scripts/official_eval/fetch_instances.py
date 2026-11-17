#!/usr/bin/env python3
"""Download SWE-bench evaluation rows for selected instances (evaluation only).

Runs inside the runner container (see official_eval.ps1). Writes:

  data/official_eval/instances.jsonl          full dataset rows, read by the harness
  data/official_eval/instances/<id>/spec.json readable summary (tests, image, commits)
  data/official_eval/instances/<id>/test.patch the official test patch
  data/official_eval/manifest.json            provenance (dataset, split, swebench version)

Everything written here is ground truth. It must never be read by ticket import,
duplicate detection, fault localization or patch generation.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import sys

DEFAULT_IDS = [
    "astropy__astropy-6938",
    "astropy__astropy-7336",
    "astropy__astropy-7606",
    "astropy__astropy-7671",
    "astropy__astropy-13572",
    "astropy__astropy-14995",
]
REQUIRED_KEYS = ("instance_id", "repo", "version", "base_commit", "image",
                 "eval_script", "log_parser", "eval_type",
                 "FAIL_TO_PASS", "PASS_TO_PASS", "test_patch")


def _as_list(value):
    if isinstance(value, str):
        return json.loads(value)
    return list(value or [])


def _write_text_lf(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", default="SWE-bench/SWE-bench",
                    help="Same dataset the GitHub Actions evaluation used")
    ap.add_argument("--split", default="test")
    ap.add_argument("--ids", nargs="+", default=DEFAULT_IDS)
    ap.add_argument("--out", default="data/official_eval")
    args = ap.parse_args()

    import swebench
    from swebench.harness.utils import load_swebench_dataset

    out = pathlib.Path(args.out)
    rows = load_swebench_dataset(args.dataset, args.split, args.ids)
    rows = [json.loads(json.dumps(dict(r), default=str)) for r in rows]
    found = {r["instance_id"] for r in rows}
    missing_ids = sorted(set(args.ids) - found)
    if missing_ids:
        print("Not found in dataset:", " ".join(missing_ids), file=sys.stderr)
        return 1

    problems = []
    lines = []
    images = {}
    for row in sorted(rows, key=lambda r: r["instance_id"]):
        iid = row["instance_id"]
        lacking = [k for k in REQUIRED_KEYS if k not in row]
        if lacking:
            problems.append(f"{iid}: missing {lacking}")
            continue
        f2p, p2p = _as_list(row["FAIL_TO_PASS"]), _as_list(row["PASS_TO_PASS"])
        spec = {
            "instance_id": iid,
            "repo": row["repo"],
            "version": row["version"],
            "base_commit": row["base_commit"],
            "environment_setup_commit": row.get("environment_setup_commit"),
            "image": row["image"],
            "log_parser": row["log_parser"],
            "eval_type": row["eval_type"],
            "FAIL_TO_PASS": f2p,
            "PASS_TO_PASS": p2p,
            "fail_to_pass_count": len(f2p),
            "pass_to_pass_count": len(p2p),
            "test_patch_sha256": hashlib.sha256(row["test_patch"].encode("utf-8")).hexdigest(),
        }
        inst_dir = out / "instances" / iid
        _write_text_lf(inst_dir / "spec.json", json.dumps(spec, ensure_ascii=False, indent=2) + "\n")
        _write_text_lf(inst_dir / "test.patch", row["test_patch"])
        lines.append(json.dumps(row, ensure_ascii=False))
        images[iid] = row["image"]
        print(f"{iid}: image={row['image']} F2P={len(f2p)} P2P={len(p2p)}")

    if problems:
        print("Dataset rows are not in the format swebench "
              f"{swebench.__version__} expects:", *problems, sep="\n  ", file=sys.stderr)
        return 1

    body = "\n".join(lines) + "\n"
    _write_text_lf(out / "instances.jsonl", body)
    manifest = {
        "purpose": "evaluation only; never an input to localization or patch generation",
        "dataset": args.dataset,
        "split": args.split,
        "swebench_version": swebench.__version__,
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "instance_ids": sorted(images),
        "images": images,
        "instances_jsonl_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }
    _write_text_lf(out / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(f"Wrote {len(lines)} instances to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
