#!/usr/bin/env python3
"""Prepare SWE-bench issues as workspace tickets, with the repository version each one needs.

Fault localization was developed and evaluated on SWE-bench (see fault_localization_feature_files).
This script turns SWE-bench rows into tickets the Ticket workspace can process end to end:

1. reads the SWE-bench Lite parquet (download it in a browser from
   https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite/resolve/main/data/test-00000-of-00001.parquet);
2. clones the selected GitHub repositories (full history, once);
3. exports the source tree at every ticket's ``base_commit`` into ``snapshots/<owner__repo>/<commit>/``
   (``git archive``; the snapshot has no .git, so the workspace never needs to run git);
4. writes ``new_tickets_swebench.jsonl`` with only the public issue text (problem_statement,
   created_at, repo, base_commit) - patch, test_patch, hints_text, FAIL_TO_PASS and PASS_TO_PASS are
   never written into a ticket;
5. writes ``answer_key_swebench.md/json`` with the files the real fix changed;
6. writes ``history_swebench.jsonl``: the other issues of the same repositories (same id filter),
   public text only, as comparison history for duplicate detection. With ``--pick latest`` (default)
   the new tickets are the most recent issues, so the history holds the earlier ones - the workspace
   never compares a ticket with reports filed after it;
7. optionally adds one project per repository to integration_config.json (a .bak copy is kept).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAIN_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPOS = ("psf/requests", "pallets/flask")
PUBLIC_FIELDS = ("instance_id", "repo", "base_commit", "problem_statement", "created_at", "version")
DIFF_FILE = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.MULTILINE)


def load_rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() in {".jsonl", ".json"}:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            value = json.loads(text)
            return value if isinstance(value, list) else value.get("rows", [])
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    try:
        import pandas as pd

        frame = pd.read_parquet(path)
    except ImportError as exc:
        raise SystemExit(f"讀取 parquet 需要 pandas 與 pyarrow：pip install pandas pyarrow（{exc}）") from exc
    return frame.to_dict(orient="records")


def read_ids(path: Path) -> set[str]:
    ids: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("{"):
            row = json.loads(line)
            value = row.get("instance_id") or row.get("ticket_id")
            if value:
                ids.add(str(value))
        else:
            ids.add(line)
    return ids


def git(args: list[str], cwd: Path | None = None) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失敗：{done.stderr.strip()[-500:]}")
    return done.stdout


def ensure_clone(repo: str, clone_dir: Path, url_template: str) -> Path:
    target = clone_dir / repo.replace("/", "__")
    if (target / ".git").is_dir() or (target / "HEAD").is_file():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    url = url_template.format(repo=repo)
    print(f"  clone {url} → {target}")
    git(["clone", "--quiet", url, str(target)])
    return target


def has_commit(clone: Path, commit: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"{commit}^{{commit}}"], cwd=clone, capture_output=True).returncode == 0


def export_snapshot(clone: Path, commit: str, snapshot: Path) -> None:
    """Source tree at ``commit`` without .git (git archive + zipfile works on Windows too)."""

    if snapshot.is_dir() and any(snapshot.iterdir()):
        return
    if not has_commit(clone, commit):
        git(["fetch", "--quiet", "origin", commit], cwd=clone)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "snapshot.zip"
        git(["archive", "--format=zip", "-o", str(archive), commit], cwd=clone)
        partial = snapshot.with_name(snapshot.name + ".partial")
        if partial.exists():
            shutil.rmtree(partial)
        with zipfile.ZipFile(archive) as handle:
            handle.extractall(partial)
        partial.rename(snapshot)


def split_issue(text: str) -> tuple[str, str]:
    lines = [line.rstrip() for line in str(text or "").replace("\r\n", "\n").split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    title = (lines[0].strip() if lines else "").lstrip("# ").strip() or "(no title)"
    return title[:200], "\n".join(lines[1:]).strip()[:8000]


def iso(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value or "")


def relative(path: Path, base: Path) -> str:
    try:
        return Path(path).resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return str(Path(path).resolve())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, required=True, help="SWE-bench parquet（或 jsonl）")
    parser.add_argument("--repos", default=",".join(DEFAULT_REPOS), help="要使用的 repository，逗號分隔（預設 psf/requests,pallets/flask，兩個都很小）")
    parser.add_argument("--limit-per-repo", type=int, default=0, help="每個 repository 最多幾張（0 = 全部）")
    parser.add_argument(
        "--pick",
        choices=("latest", "earliest"),
        default="latest",
        help="搭配 --limit-per-repo：新 Ticket 取每個 repository 最新（預設）或最早的幾題；其餘寫進 history_swebench.jsonl",
    )
    parser.add_argument(
        "--only-ids",
        type=Path,
        default=None,
        help="只用這個清單裡的題目（jsonl 的 ticket_id／instance_id，或每行一個 id）。"
        "用錯誤定位的 development_tickets.jsonl 可避開保留測試集。",
    )
    parser.add_argument("--work-dir", type=Path, default=MAIN_ROOT / "data" / "swebench", help="clone 與版本快照存放處")
    parser.add_argument("--output-dir", type=Path, default=MAIN_ROOT / "data" / "real_datasets")
    parser.add_argument("--config", type=Path, default=None, help="把每個 repository 加成專案（例如 integration_config.json）")
    parser.add_argument("--repo-url", default="https://github.com/{repo}.git", help="clone 網址樣板（測試用）")
    args = parser.parse_args()

    wanted = [item.strip() for item in args.repos.split(",") if item.strip()]
    rows = [row for row in load_rows(args.input) if row.get("repo") in wanted]
    if args.only_ids:
        allowed = read_ids(args.only_ids)
        rows = [row for row in rows if str(row.get("instance_id")) in allowed]
        print(f"只使用 {args.only_ids.name} 裡的題目：{len(rows)} 張")
    if not rows:
        raise SystemExit(f"{args.input} 裡沒有 {', '.join(wanted)} 的資料。")
    rows.sort(key=lambda row: (row["repo"], iso(row.get("created_at"))))
    pool = rows
    if args.limit_per_repo > 0:
        ordered = rows if args.pick == "earliest" else list(reversed(rows))
        kept: dict[str, int] = {}
        limited = []
        for row in ordered:
            if kept.get(row["repo"], 0) < args.limit_per_repo:
                limited.append(row)
                kept[row["repo"]] = kept.get(row["repo"], 0) + 1
        rows = sorted(limited, key=lambda row: (row["repo"], iso(row.get("created_at"))))
    chosen = {str(row["instance_id"]) for row in rows}
    history = []
    for row in pool:
        if str(row["instance_id"]) in chosen:
            continue
        title, description = split_issue(row.get("problem_statement"))
        history.append({
            "ticket_id": row["instance_id"],
            "project": row["repo"],
            "title": title,
            "description": description,
            "created_at": iso(row.get("created_at")),
            "product": row["repo"],
            "source": "SWE-bench（歷史 issue）",
        })

    clones, snapshots_root = args.work_dir / "clones", args.work_dir / "snapshots"
    tickets, answers = [], []
    for repo in wanted:
        repo_rows = [row for row in rows if row["repo"] == repo]
        if not repo_rows:
            continue
        print(f"{repo}：{len(repo_rows)} 張")
        clone = ensure_clone(repo, clones, args.repo_url)
        for row in repo_rows:
            commit = str(row["base_commit"])
            export_snapshot(clone, commit, snapshots_root / repo.replace("/", "__") / commit)
            title, description = split_issue(row.get("problem_statement"))
            tickets.append({
                "ticket_id": row["instance_id"],
                "project": repo,
                "title": title,
                "description": description,
                "created_at": iso(row.get("created_at")),
                "repo": repo,
                "base_commit": commit,
                "version": str(row.get("version") or ""),
                "source": "SWE-bench Lite",
            })
            files = sorted({match.group(2) for match in DIFF_FILE.finditer(str(row.get("patch") or ""))})
            number = str(row["instance_id"]).rsplit("-", 1)[-1]
            answers.append({
                "ticket_id": row["instance_id"],
                "project": repo,
                "title": title,
                "fixed_files": files,
                "fix_pull_request": f"https://github.com/{repo}/pull/{number}" if number.isdigit() else "",
            })

    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / "new_tickets_swebench.jsonl").write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in tickets), encoding="utf-8")
    (out / "history_swebench.jsonl").write_text("".join(json.dumps(h, ensure_ascii=False) + "\n" for h in history), encoding="utf-8")
    meta = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source": str(args.input), "repos": wanted}
    (out / "answer_key_swebench.json").write_text(json.dumps({**meta, "answers": answers}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# SWE-bench 示範：答案",
        "",
        "這份只用來核對錯誤定位結果，不要匯入。真正修改的檔案來自 SWE-bench 的 gold patch；新 Ticket 裡沒有 patch、測試與提示。",
        "",
        "| 專案 | Ticket | 標題 | 真正修改的檔案 |",
        "|---|---|---|---|",
    ]
    lines += [f"| {a['project']} | {a['ticket_id']} | {a['title'][:70].replace('|', '/')} | {'<br>'.join(a['fixed_files'])} |" for a in answers]
    lines += [
        "",
        "## 解讀時要注意",
        "",
        "- 錯誤定位團隊在 SWE-bench 上調過方法，這批資料不是沒看過的測試集，結果只能當示範。",
        "- `history_swebench.jsonl` 是同一個 repository 的其他 issue（只有標題、內文、時間），可以當重複偵測的比對歷史；SWE-bench 沒有重複標記，所以候選都應該判為「不是重複」。",
        "- SWE-bench 沒有負責人資料，負責人推薦會建議人工分派。",
        "- PRI 優先級模型是用 Eclipse 訓練的，套在 GitHub issue 上屬於跨資料集推測。",
    ]
    (out / "answer_key_swebench.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    if args.config:
        config_path = args.config
        data = json.loads(config_path.read_text(encoding="utf-8"))
        shutil.copyfile(config_path, config_path.with_suffix(config_path.suffix + ".bak"))
        base = config_path.parent
        projects = data.setdefault("projects", {})
        for repo in wanted:
            if not any(t["project"] == repo for t in tickets):
                continue
            projects[repo] = {
                "repo_path": relative(clones / repo.replace("/", "__"), base),
                "snapshots_dir": relative(snapshots_root / repo.replace("/", "__"), base),
                "description": f"SWE-bench：{repo}（每張 Ticket 用自己 base_commit 的程式碼定位）",
            }
        config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"已把 {', '.join(repo for repo in wanted if repo in projects)} 加進 {config_path}（原檔備份為 .bak）")

    print(f"新 Ticket：{out / 'new_tickets_swebench.jsonl'}（{len(tickets)} 張）")
    print(f"比對歷史：{out / 'history_swebench.jsonl'}（{len(history)} 筆，匯入時勾選「這是歷史 Ticket」）")
    print(f"答案：{out / 'answer_key_swebench.md'}")


if __name__ == "__main__":
    main()
