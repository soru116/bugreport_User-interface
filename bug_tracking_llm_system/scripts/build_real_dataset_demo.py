#!/usr/bin/env python3
"""Turn the team's real bug-report datasets into files the Ticket workspace can import.

Sources (already used by the sub-projects, read-only here):

* Mozilla Firefox (bug-duplicate-detection/data/mozilla_firefox_*.csv): real Bugzilla reports with
  duplicate labels, component, severity, reporter and assignee.
* Eclipse (bug-priority-drone/data/raw/eclipse_bugzilla_raw_with_dupe.csv): real Bugzilla reports
  with P1-P5 labels. New tickets come from PRI's natural_test split, which the priority model
  never trained on.

Outputs (default: bug_tracking_llm_system/data/real_datasets/, git-ignored):

* history.jsonl - labelled reports for the duplicate / assignee history ("這是歷史 Ticket").
* new_tickets_firefox.jsonl, new_tickets_eclipse.jsonl - reports to process; every label
  (duplicate_of, priority, assignee, status, resolution) is removed.
* answer_key.json / answer_key.md - the removed labels, for checking the workspace's results.

Each row carries ``project`` ("Mozilla Firefox" / "Eclipse"), so the workspace files them into
project folders and only compares duplicates and assignees within the same project.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAIN_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = MAIN_ROOT.parent
FIREFOX_PROJECT = "Mozilla Firefox"
ECLIPSE_PROJECT = "Eclipse"
FIREFOX_PREFIX = "FF-"  # Firefox and Eclipse Bugzilla ids overlap; Eclipse keeps its raw id so PRI can match it.
FIREFOX_FILES = (
    "mozilla_firefox_duplicates.csv",
    "mozilla_firefox_non_duplicates.csv",
    "mozilla_firefox_non_duplicates_extra.csv",
    "mozilla_firefox_decision_eval.csv",
)

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


def first_existing(candidates: list[Path]) -> Path | None:
    return next((path for path in candidates if path.exists()), None)


def default_dup_dir() -> Path | None:
    return first_existing([
        WORKSPACE_ROOT / "bug-duplicate-detection" / "bug-duplicate-detection" / "data",
        WORKSPACE_ROOT / "bug-duplicate-detection" / "data",
    ])


def default_pri_dir() -> Path | None:
    candidates = []
    for name in ("bug-priority-drone", "bug-priority-drone(1)"):
        candidates += [WORKSPACE_ROOT / name / "bug-priority-drone" / "data", WORKSPACE_ROOT / name / "data"]
    return first_existing([path for path in candidates if (path / "raw" / "eclipse_bugzilla_raw_with_dupe.csv").exists()])


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def clean_id(value: Any) -> str:
    text = str(value or "").strip()
    if text.endswith(".0"):
        text = text[:-2]
    return "" if text.lower() in {"", "nan", "none", "--"} else text


def usable_text(row: dict[str, str], *, title_key: str, min_len: int = 80, max_len: int = 1800) -> bool:
    description = (row.get("description") or "").strip()
    return bool((row.get(title_key) or "").strip()) and min_len <= len(description) <= max_len


def pick_spread(rows: list[dict[str, str]], count: int, rng: random.Random, *, key: str = "component") -> list[dict[str, str]]:
    """Random picks that prefer components not chosen yet, so the demo is not ten 'General' bugs."""

    pool = rows[:]
    rng.shuffle(pool)
    chosen: list[dict[str, str]] = []
    seen: Counter[str] = Counter()
    while pool and len(chosen) < count:
        pool.sort(key=lambda row: seen[row.get(key, "")])
        row = pool.pop(0)
        chosen.append(row)
        seen[row.get(key, "")] += 1
    return chosen


# --------------------------------------------------------------------------- Firefox
def build_firefox(dup_dir: Path, *, duplicates: int, non_duplicates: int, rng: random.Random) -> tuple[list, list, list]:
    union: dict[str, dict[str, str]] = {}
    for name in FIREFOX_FILES:
        path = dup_dir / name
        if not path.exists():
            continue
        for row in read_csv(path):
            ticket_id = clean_id(row.get("ticket_id"))
            if ticket_id and (ticket_id not in union or (clean_id(row.get("duplicate_of")) and not clean_id(union[ticket_id].get("duplicate_of")))):
                union[ticket_id] = row
    if not union:
        raise SystemExit(f"在 {dup_dir} 找不到 Firefox 資料（{', '.join(FIREFOX_FILES)}）。")

    def master_ok(row: dict[str, str]) -> bool:
        master = union.get(clean_id(row.get("duplicate_of")))
        return master is not None and (master.get("created_at") or "") < (row.get("created_at") or "")

    dup_pool = [row for row in union.values() if clean_id(row.get("duplicate_of")) and master_ok(row) and usable_text(row, title_key="title")]
    chosen_dups: list[dict[str, str]] = []
    for row in pick_spread(dup_pool, len(dup_pool), rng):
        if len(chosen_dups) >= duplicates:
            break
        taken = {clean_id(r["ticket_id"]) for r in chosen_dups} | {clean_id(r["duplicate_of"]) for r in chosen_dups}
        if clean_id(row["ticket_id"]) in taken or clean_id(row["duplicate_of"]) in taken:
            continue
        chosen_dups.append(row)
    taken = {clean_id(r["ticket_id"]) for r in chosen_dups} | {clean_id(r["duplicate_of"]) for r in chosen_dups}
    masters = {clean_id(row.get("duplicate_of")) for row in union.values()} - {""}
    non_pool = [
        row for row in union.values()
        if not clean_id(row.get("duplicate_of")) and clean_id(row.get("ticket_id")) not in taken
        and clean_id(row.get("ticket_id")) not in masters  # nothing in the history duplicates it either
        and (row.get("resolution") or "").upper() == "FIXED" and usable_text(row, title_key="title")
    ]
    chosen_non = pick_spread(non_pool, non_duplicates, rng)
    new_rows = chosen_dups + chosen_non
    new_ids = {clean_id(row["ticket_id"]) for row in new_rows}

    def fid(value: Any) -> str:
        value = clean_id(value)
        return f"{FIREFOX_PREFIX}{value}" if value else ""

    history = []
    for ticket_id, row in union.items():
        if ticket_id in new_ids:
            continue
        history.append({
            "ticket_id": fid(ticket_id),
            "project": FIREFOX_PROJECT,
            "title": row.get("title", ""),
            "description": row.get("description", ""),
            "product": row.get("product", ""),
            "component": row.get("component", ""),
            "severity": row.get("severity", ""),
            "priority": "" if (row.get("priority") or "").strip() == "--" else row.get("priority", ""),
            "reporter": row.get("reporter", ""),
            "assignee": row.get("assignee", ""),
            "status": row.get("status", ""),
            "resolution": row.get("resolution", ""),
            "duplicate_of": fid(row.get("duplicate_of")),
            "created_at": row.get("created_at", ""),
            "url": f"https://bugzilla.mozilla.org/show_bug.cgi?id={ticket_id}",
            "source": "mozilla_firefox (bug-duplicate-detection)",
        })

    tickets, answers = [], []
    for row in new_rows:
        ticket_id = clean_id(row["ticket_id"])
        tickets.append({
            "ticket_id": fid(ticket_id),
            "project": FIREFOX_PROJECT,
            "title": row.get("title", ""),
            "description": row.get("description", ""),
            "product": row.get("product", ""),
            "component": row.get("component", ""),
            "severity": row.get("severity", ""),
            "reporter": row.get("reporter", ""),
            "created_at": row.get("created_at", ""),
            "url": f"https://bugzilla.mozilla.org/show_bug.cgi?id={ticket_id}",
        })
        master = union.get(clean_id(row.get("duplicate_of")))
        answers.append({
            "ticket_id": fid(ticket_id),
            "project": FIREFOX_PROJECT,
            "title": row.get("title", ""),
            "duplicate_of": fid(row.get("duplicate_of")),
            "duplicate_of_title": master.get("title", "") if master else "",
            "priority": "" if (row.get("priority") or "").strip() == "--" else row.get("priority", ""),
            "assignee": row.get("assignee", ""),
            "resolution": row.get("resolution", ""),
        })
    return history, tickets, answers


# --------------------------------------------------------------------------- Eclipse
def build_eclipse(pri_dir: Path, *, per_priority: int, duplicates: int, history_size: int, rng: random.Random) -> tuple[list, list, list]:
    raw_rows = read_csv(pri_dir / "raw" / "eclipse_bugzilla_raw_with_dupe.csv")
    natural = read_csv(pri_dir / "processed" / "experiment_splits_with_dupe" / "natural_test_clean.csv")
    by_id = {clean_id(row.get("id")): row for row in raw_rows if clean_id(row.get("id"))}
    natural_ids = [clean_id(row.get("id")) for row in natural]
    pool = [by_id[i] for i in natural_ids if i in by_id and usable_text(by_id[i], title_key="summary")]

    def master_ok(row: dict[str, str]) -> bool:
        master = by_id.get(clean_id(row.get("dupe_of")))
        return master is not None and (master.get("creation_time") or "") < (row.get("creation_time") or "")

    all_masters = {clean_id(row.get("dupe_of")) for row in raw_rows} - {""}
    chosen: list[dict[str, str]] = []
    dup_pool = [row for row in pool if clean_id(row.get("dupe_of")) and master_ok(row)]
    chosen += pick_spread(dup_pool, duplicates, rng)
    for level in ("P1", "P2", "P3", "P4", "P5"):
        level_pool = [
            row for row in pool
            if row.get("priority") == level and row not in chosen and not clean_id(row.get("dupe_of")) and clean_id(row.get("id")) not in all_masters
        ]
        chosen += pick_spread(level_pool, per_priority, rng)
    new_ids = {clean_id(row["id"]) for row in chosen}

    # Duplicate-detection history: the masters of the chosen duplicates plus a random sample, so each
    # ticket compares against a realistic number of reports without re-encoding all 10,000 every time.
    masters = {clean_id(row.get("dupe_of")) for row in chosen} - {""}
    others = [i for i in by_id if i not in new_ids and i not in masters]
    rng.shuffle(others)
    history_ids = list(masters) + others[: max(0, history_size - len(masters))]

    def history_row(row: dict[str, str]) -> dict[str, Any]:
        ticket_id = clean_id(row.get("id"))
        return {
            "ticket_id": ticket_id,
            "project": ECLIPSE_PROJECT,
            "title": row.get("summary", ""),
            "description": row.get("description", ""),
            "product": row.get("product", ""),
            "component": row.get("component", ""),
            "severity": row.get("severity", ""),
            "priority": row.get("priority", ""),
            "reporter": row.get("creator", ""),
            "status": row.get("status", ""),
            "resolution": row.get("resolution", ""),
            "duplicate_of": clean_id(row.get("dupe_of")),
            "created_at": row.get("creation_time", ""),
            "url": f"https://bugs.eclipse.org/bugs/show_bug.cgi?id={ticket_id}",
            "source": "eclipse_bugzilla (bug-priority-drone)",
        }

    history = [history_row(by_id[i]) for i in history_ids]
    tickets, answers = [], []
    for row in chosen:
        ticket_id = clean_id(row["id"])
        tickets.append({
            "ticket_id": ticket_id,
            "project": ECLIPSE_PROJECT,
            "title": row.get("summary", ""),
            "description": row.get("description", ""),
            "product": row.get("product", ""),
            "component": row.get("component", ""),
            "severity": row.get("severity", ""),
            "reporter": row.get("creator", ""),
            "created_at": row.get("creation_time", ""),
            "url": f"https://bugs.eclipse.org/bugs/show_bug.cgi?id={ticket_id}",
        })
        master = by_id.get(clean_id(row.get("dupe_of")))
        answers.append({
            "ticket_id": ticket_id,
            "project": ECLIPSE_PROJECT,
            "title": row.get("summary", ""),
            "duplicate_of": clean_id(row.get("dupe_of")),
            "duplicate_of_title": master.get("summary", "") if master else "",
            "duplicate_in_history": bool(master) and clean_id(row.get("dupe_of")) in set(history_ids),
            "priority": row.get("priority", ""),
            "assignee": "",
            "resolution": row.get("resolution", ""),
        })
    return history, tickets, answers


# --------------------------------------------------------------------------- output
def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def answer_markdown(answers: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    lines = [
        "# 真實資料示範：答案",
        "",
        f"產生時間：{meta['generated_at']}，亂數種子 {meta['seed']}。這份檔案只用來核對工作台的結果，不要匯入。",
        "",
        "| 專案 | Ticket | 標題 | 真正的重複對象 | 真正的優先級 | 真正的負責人 |",
        "|---|---|---|---|---|---|",
    ]
    for item in answers:
        dup = f"{item['duplicate_of']}（{item['duplicate_of_title'][:40]}）" if item["duplicate_of"] else "不是重複"
        lines.append(f"| {item['project']} | {item['ticket_id']} | {item['title'][:60].replace('|', '/')} | {dup.replace('|', '/')} | {item['priority'] or '（資料沒有）'} | {item['assignee'] or '（資料沒有）'} |")
    lines += ["", "## 解讀時要注意", ""] + [f"- {note}" for note in meta["notes"]]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dup-data-dir", type=Path, default=None, help="bug-duplicate-detection 的 data 資料夾（預設自動尋找）")
    parser.add_argument("--pri-data-dir", type=Path, default=None, help="bug-priority-drone 的 data 資料夾（預設自動尋找）")
    parser.add_argument("--output-dir", type=Path, default=MAIN_ROOT / "data" / "real_datasets")
    parser.add_argument("--firefox-duplicates", type=int, default=4, help="Firefox 新 Ticket 中，真的是重複的張數")
    parser.add_argument("--firefox-non-duplicates", type=int, default=2)
    parser.add_argument("--eclipse-per-priority", type=int, default=1, help="Eclipse 每個優先級（P1–P5）各取幾張")
    parser.add_argument("--eclipse-duplicates", type=int, default=1)
    parser.add_argument("--eclipse-history-size", type=int, default=1500, help="Eclipse 重複偵測歷史資料的筆數")
    parser.add_argument("--seed", type=int, default=20261019)
    args = parser.parse_args()

    dup_dir = args.dup_data_dir or default_dup_dir()
    pri_dir = args.pri_data_dir or default_pri_dir()
    rng = random.Random(args.seed)
    history, tickets, answers = [], {}, []
    notes = [
        "新 Ticket 已移除 duplicate_of、priority、assignee、status、resolution；這些答案只在這份檔案裡。",
        "專案沒有對應的 Repository，所以錯誤定位會顯示「略過」，這是預期的結果。",
    ]
    if dup_dir and dup_dir.exists():
        h, t, a = build_firefox(dup_dir, duplicates=args.firefox_duplicates, non_duplicates=args.firefox_non_duplicates, rng=rng)
        history += h
        tickets["firefox"] = t
        answers += a
        notes += [
            "SBERT 重複偵測模型是用 mozilla_firefox_duplicates.csv 訓練的，Firefox 的重複 Ticket 都在訓練資料裡，所以重複偵測結果會偏好看，不能當成準確率。",
            "PRI 優先級模型只用 Eclipse 資料訓練；Firefox Ticket 的優先級是跨資料集的推測，而且 Firefox 原始資料大多沒有填優先級，無法核對。",
            "Firefox 的負責人有一半以上是 bugzilla@blakeross.com，負責人推薦會明顯偏向他，這是資料本身的分布。",
        ]
    else:
        print("找不到 bug-duplicate-detection 的 data 資料夾，略過 Firefox。", file=sys.stderr)
    if pri_dir and pri_dir.exists():
        h, t, a = build_eclipse(pri_dir, per_priority=args.eclipse_per_priority, duplicates=args.eclipse_duplicates, history_size=args.eclipse_history_size, rng=rng)
        history += h
        tickets["eclipse"] = t
        answers += a
        notes += [
            "Eclipse 新 Ticket 來自 PRI 的 natural_test，PRI 模型訓練時沒看過；工作台計算優先級時會把同一張報告從歷史資料中排除。",
            f"Eclipse 的重複偵測歷史只取 {args.eclipse_history_size} 筆（含正確答案），比完整的 1 萬筆容易；SBERT 是用 Firefox 訓練的，用在 Eclipse 屬於跨資料集。",
            "Eclipse 資料沒有負責人欄位，所以 Eclipse Ticket 的負責人推薦沒有可用的歷史，會建議人工分派。",
        ]
    else:
        print("找不到 bug-priority-drone 的 data 資料夾，略過 Eclipse。", file=sys.stderr)
    if not tickets:
        raise SystemExit("兩份資料都找不到，請用 --dup-data-dir／--pri-data-dir 指定。")

    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    write_jsonl(out / "history.jsonl", history)
    for name, rows in tickets.items():
        write_jsonl(out / f"new_tickets_{name}.jsonl", rows)
    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seed": args.seed,
        "sources": {"firefox": str(dup_dir or ""), "eclipse": str(pri_dir or "")},
        "notes": notes,
    }
    (out / "answer_key.json").write_text(json.dumps({**meta, "answers": answers}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "answer_key.md").write_text(answer_markdown(answers, meta), encoding="utf-8")

    by_project = Counter(row["project"] for row in history)
    print(f"輸出到 {out}")
    print(f"  history.jsonl：{len(history)} 筆（{', '.join(f'{k} {v}' for k, v in by_project.items())}）")
    for name, rows in tickets.items():
        print(f"  new_tickets_{name}.jsonl：{len(rows)} 張")
    print("  answer_key.md：答案（不要匯入）")


if __name__ == "__main__":
    main()
