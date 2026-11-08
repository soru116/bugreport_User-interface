#!/usr/bin/env python3
"""Prepare the patch-generation demo: the 36-ticket development set tickets that the 6/36 run fixed.

The tickets come from the vendored patch generator (``patch/rewrite-gemma4-5cdb2be``):

* ``experiments/eval36_v7/tickets.jsonl`` holds the public ticket text the run used (no patch, tests
  or hints);
* ``official_verdicts.jsonl`` holds the official SWE-bench verdicts of that run's patches.

By default only the tickets whose patch the official harness judged resolved are taken (6 tickets);
``--all`` takes all 36.  For each repository the script

1. clones it (``--clone-from`` an existing local clone avoids downloading again; the source clone is
   only read), and exports the source at every ticket's ``base_commit`` into
   ``snapshots/<owner__repo>/<commit>/`` for fault localization (same layout as build_swebench_demo.py);
2. writes ``new_tickets_patch_demo.jsonl`` and ``answer_key_patch_demo.md/json`` (the run's patch and
   its official verdict, for checking the workspace result afterwards);
3. optionally adds the repository as a project to integration_config.json (a .bak copy is kept).

Patch generation itself needs the clone (a git checkout), not the snapshots: the workspace creates a
git checkout at ``base_commit`` from it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_swebench_demo import export_snapshot, git, relative, split_issue  # noqa: E402

MAIN_ROOT = Path(__file__).resolve().parents[1]
PATCH_DIR = MAIN_ROOT.parent / "patch" / "rewrite-gemma4-5cdb2be"
EXPERIMENT = "rewrite-gemma4-c3-20261106"


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def experiment_patches(patch_dir: Path) -> dict[str, dict]:
    """instance_id -> {patch, patch_sha256, official} for the 6/36 run."""

    verdicts = {row["patch_sha256"]: row for row in read_jsonl(patch_dir / "official_verdicts.jsonl")}
    rows = {}
    for row in read_jsonl(patch_dir / "experiments" / "eval36_v7" / "results" / EXPERIMENT / "predictions.jsonl"):
        patch = row.get("model_patch") or ""
        sha = hashlib.sha256(patch.encode("utf-8")).hexdigest()
        rows[row["instance_id"]] = {"patch": patch, "patch_sha256": sha, "official": verdicts.get(sha)}
    return rows


def ensure_clone(repo: str, clone_dir: Path, *, clone_from: Path | None, url_template: str) -> Path:
    target = clone_dir / repo.replace("/", "__")
    if (target / ".git").is_dir():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    if clone_from is not None:
        print(f"  clone {clone_from} → {target}（從本機 clone 複製，來源不會被修改）")
        git(["clone", "--quiet", "--no-hardlinks", str(clone_from), str(target)])
    else:
        url = url_template.format(repo=repo)
        print(f"  clone {url} → {target}")
        git(["clone", "--quiet", url, str(target)])
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--patch-dir", type=Path, default=PATCH_DIR, help="補丁生成程式所在資料夾")
    parser.add_argument("--all", action="store_true", help="匯出全部 36 題（預設只有官方判定修好的題目）")
    parser.add_argument("--ids", default="", help="只匯出這些題號，逗號分隔（例如 astropy__astropy-7606）")
    parser.add_argument("--clone-from", type=Path, default=None,
                        help="已經存在的本機 clone（例如專題0908錯誤定位\\fault-localization-feature-files\\data\\repositories\\astropy__astropy），只讀取")
    parser.add_argument("--repo-url", default="https://github.com/{repo}.git", help="沒有 --clone-from 時的 clone 網址樣板")
    parser.add_argument("--work-dir", type=Path, default=MAIN_ROOT / "data" / "swebench", help="clone 與版本快照存放處")
    parser.add_argument("--output-dir", type=Path, default=MAIN_ROOT / "data" / "real_datasets" / "patch_demo")
    parser.add_argument("--config", type=Path, default=None, help="把 repository 加成專案（例如 integration_config.json）")
    args = parser.parse_args()

    tickets = read_jsonl(args.patch_dir / "experiments" / "eval36_v7" / "tickets.jsonl")
    patches = experiment_patches(args.patch_dir)
    if args.ids:
        wanted = {item.strip() for item in args.ids.split(",") if item.strip()}
    elif args.all:
        wanted = {row["ticket_id"] for row in tickets}
    else:
        wanted = {tid for tid, row in patches.items() if row["official"] and row["official"].get("official_resolved") is True}
    selected = [row for row in tickets if row["ticket_id"] in wanted]
    if not selected:
        raise SystemExit("沒有選到任何題目。")
    repos = sorted({row["repo"] for row in selected})
    if args.clone_from is not None and len(repos) > 1:
        raise SystemExit("--clone-from 只能用在單一 repository；請用 --ids 分開匯出。")

    clones, snapshots_root = args.work_dir / "clones", args.work_dir / "snapshots"
    out_tickets, answers = [], []
    for repo in repos:
        rows = [row for row in selected if row["repo"] == repo]
        print(f"{repo}：{len(rows)} 張")
        clone = ensure_clone(repo, clones, clone_from=args.clone_from, url_template=args.repo_url)
        for row in sorted(rows, key=lambda item: item.get("created_at") or ""):
            commit = str(row["base_commit"])
            export_snapshot(clone, commit, snapshots_root / repo.replace("/", "__") / commit)
            # The ticket text is kept byte for byte as the 6/36 run saw it (no re-splitting or trimming).
            text = row.get("description") or row.get("bug_report") or ""
            title = row.get("title") or split_issue(text)[0]
            out_tickets.append({
                "ticket_id": row["ticket_id"],
                "project": repo,
                "title": title,
                "description": text,
                "bug_report": row.get("bug_report") or text,
                "product": row.get("product") or repo,
                "component": row.get("component") or "",
                "created_at": row.get("created_at") or "",
                "repo": repo,
                "base_commit": commit,
                "version": str(row.get("version") or ""),
                "source": "SWE-bench（36 題補丁實驗）",
            })
            record = patches.get(row["ticket_id"])
            answers.append({
                "ticket_id": row["ticket_id"],
                "project": repo,
                "title": title,
                "experiment": EXPERIMENT,
                "experiment_generated": record is not None,
                "experiment_patch_sha256": record["patch_sha256"] if record else "",
                "official_resolved": ((record or {}).get("official") or {}).get("official_resolved"),
                "official_run_id": ((record or {}).get("official") or {}).get("run_id", ""),
            })

    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / "new_tickets_patch_demo.jsonl").write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in out_tickets), encoding="utf-8")
    meta = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "experiment": EXPERIMENT, "patch_dir": str(args.patch_dir)}
    (out / "answer_key_patch_demo.json").write_text(json.dumps({**meta, "answers": answers}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def verdict(answer: dict) -> str:
        if not answer["experiment_generated"]:
            return "那一輪沒有產生補丁"
        if answer["official_resolved"] is True:
            return "✅ 修好"
        if answer["official_resolved"] is False:
            return "❌ 沒修好"
        return "未送官方測試"

    lines = [
        "# 補丁生成示範：36 題實驗的紀錄",
        "",
        f"實驗：`{EXPERIMENT}`（對話式改寫 + gemma4:12b，3 個候選）。這份只用來核對，不要匯入。",
        "",
        "| 專案 | Ticket | 標題 | 那一輪的補丁 sha256 | 官方判定 |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| {a['project']} | {a['ticket_id']} | {a['title'][:60].replace('|', '/')} | `{a['experiment_patch_sha256'][:12]}` | {verdict(a)} |"
        for a in answers
    ]
    lines += [
        "",
        "## 解讀時要注意",
        "",
        "- 工作台重新產生的補丁，只有 sha256 和上表完全相同時，才能引用那個官方判定；工作台會自動比對。",
        "- 同樣的設定重跑不一定得到同一個補丁（模型輸出不是逐位元可重現），錯誤定位也是工作台重新算的。",
        "- 這 36 題是開發集，被反覆用來調方法，不能當泛化證據。",
        "- `official_resolved: true` 的定義是 FAIL_TO_PASS 全部通過且 PASS_TO_PASS 沒有退步，不等於「改得對」。",
    ]
    (out / "answer_key_patch_demo.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    if args.config:
        config_path = args.config
        data = json.loads(config_path.read_text(encoding="utf-8"))
        shutil.copyfile(config_path, config_path.with_suffix(config_path.suffix + ".bak"))
        base = config_path.parent
        projects = data.setdefault("projects", {})
        for repo in repos:
            projects[repo] = {
                "repo_path": relative(clones / repo.replace("/", "__"), base),
                "snapshots_dir": relative(snapshots_root / repo.replace("/", "__"), base),
                "description": f"SWE-bench：{repo}（36 題補丁實驗；每張 Ticket 用自己 base_commit 的程式碼）",
            }
        config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"已把 {', '.join(repos)} 加進 {config_path}（原檔備份為 .bak）")

    print(f"新 Ticket：{out / 'new_tickets_patch_demo.jsonl'}（{len(out_tickets)} 張）")
    print(f"實驗紀錄：{out / 'answer_key_patch_demo.md'}")


if __name__ == "__main__":
    main()
