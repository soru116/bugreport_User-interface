#!/usr/bin/env python3
"""One astropy dataset that exercises all seven workspace steps.

SWE-bench keeps only the issue text, the commit and the fix, so on its own it has no assignees and no
duplicate links.  astropy's GitHub tracker has both, so this script joins the two:

* **history** (``history_astropy_github.jsonl``): astropy's GitHub issues created before 2023-07-01.
  ``assignee`` is the issue's GitHub assignee, otherwise the author of the merged pull request that
  closed it ("Fixes #N" in the PR text), which is what Bugzilla's "assigned to" usually records too
  (``assignee_source`` says which one).  Issues closed as duplicates keep ``resolution: DUPLICATE``.
* **new tickets** (``new_tickets_astropy_github.jsonl``):
  - the 6 SWE-bench development tickets the patch run fixed (text and ``base_commit`` identical to
    ``patch_demo``), for steps 1 and 3-7;
  - 3 issues that astropy maintainers closed as duplicates of an older issue, for step 2.
* **answer key** (``answer_key_astropy_github.md/json``, not imported): the duplicate target, who fixed
  each ticket, the files the fix changed and the patch run's official verdict.

Left out of the history so nothing answers itself: each new ticket's own issue, and every issue that
belongs to a SWE-bench validation or frozen-holdout instance (matched by title).

The GitHub data is read through the search API (``--fetch``, set ``GITHUB_TOKEN`` for the rate limit)
or from earlier downloads (``--issues``/``--prs``: one search result item per line).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

MAIN_ROOT = Path(__file__).resolve().parents[1]
PROJECT = "astropy/astropy"
HISTORY_BEFORE = "2023-07-01"
BODY_LIMIT = 4000

# The 6 tickets of demo_data/patch_demo: SWE-bench instance -> (GitHub issue it comes from, fixed files).
# Fixed files are the non-test, non-changelog files of the merged pull request.
PATCH_TICKETS = {
    "astropy__astropy-6938": (6921, ["astropy/io/fits/fitsrec.py"]),
    "astropy__astropy-7336": (7335, ["astropy/units/decorators.py"]),
    "astropy__astropy-7606": (7603, ["astropy/units/core.py"]),
    "astropy__astropy-7671": (7670, ["astropy/utils/introspection.py"]),
    "astropy__astropy-13572": (6583, ["astropy/coordinates/earth_orientation.py"]),
    "astropy__astropy-14995": (14978, ["astropy/nddata/mixins/ndarithmetic.py"]),
}
# Issues closed as duplicates, with the original a maintainer named in the issue's comments.
DUPLICATE_TICKETS = {
    11571: (8812, "pllim: \"duplicate of #8812\""),
    11104: (8091, "adrn: \"duplicate of #8091\""),
    9412: (9173, "bsipocz: links issues/9173"),
}
# SWE-bench astropy instances outside the development split (validation + frozen holdout).  Their
# issues are not used at all.
HELD_OUT_TITLES = {
    "inheritdocstrings metaclass doesn't work for properties", "qtable: group keys don't have a unit",
    "should `quantity.__array_ufunc__()` return `notimplemented` instead of raising `valueerror` if the inputs are incompatible?",
    "can't convert a list of astropy tables to a numpy array of tables", "problem with the `littleh` part of unit equivalencies?",
    "a direct approach to itrs to observed transformations that stays within the itrs.",
    "`time` parses fractional days in year-month-day format incorrectly", "float32 representation of pi/2 is rejected by `latitude`",
    "`io.fits.card` may use a string representation of floats that is larger than necessary",
    "inconsistent behavior of `world_to_pixel` in `slicedlowlevelwcs`",
    "'wcs.all_world2pix' failed to converge when plotting wcs with non linear distortions",
    "biweight_location of a constant array returns nan", "space between value and unit",
    "silence warnings by default when reading in vo tables", "float16 quantities get upgraded to float64 automatically",
    "no longer able to read binnedtimeseries with datetime column saved as ecsv after upgrading from 4.2.1 -> 5.0+",
    "subclassed skycoord gives misleading attribute access message", "add __array_func__ for astropy.time.time",
    "[security] jquery 3.1.1 is vulnerable to untrusted code execution",
    "blackbody bolometric flux is wrong if scale has units of dimensionless_unscaled",
    "writing a table to fits fails if the table contains objects", "angle bug for (d, m, s) tuple input (deprecate dms_to_degrees)",
    "add helpers to convert between different types of uncertainties", "cosmologies used in equivalencies",
    "add option to input/output column units for fixed width tables", "regarding fits standard definition for 'jy/beam'",
    "creating a mixin column in a new table from columns of another table renames columns in original table.",
    "timeseries: misleading exception when required column check fails.",
    "register format ``html`` to ``cosmology.write`` with nice mathjax", "new quantity warning starting with yesterday's numpy-dev",
    "ascii table output to html does not support supplied \"formats\"", "add ``atol`` argument to function ``is_o3``",
    "bug: table test failures with np 1.23.0rc3", "make getting a topocentric itrs position easier",
    "model evaluation fails if any model parameter is a `magunit` type value", "can table masking be turned off?",
    "problem with \"decimalyear\" applied to maskedcolumn type", "units: 's / m' and 's / m' are not convertible",
    "tst: test_table_group_by[true] and test_group_by_masked[true] failed with numpy 1.25rc1",
    "fits-standard unit parsing fails on some types of exponents", "add registration label ‘ascii.latex’ to cosmology io",
    "presence of sip keywords leads to ignored pv keywords.", "time from astropy.time not precise",
    "header.fromstring does not accept python 3 bytes",
}
SUBPACKAGE = re.compile(r"^(config|constants|convolution|coordinates|cosmology|io\.[a-z.]+|modeling|nddata|samp|stats|table|time|timeseries|uncertainty|units|utils(\.[a-z]+)?|visualization(\.[a-z]+)?|wcs(\.[a-z]+)?|vo(\.[a-z]+)?)$")
CLOSING = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b[:\s]+(?:(?:https://github\.com/)?astropy/astropy(?:/issues/|#)|#)(\d+)")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def fetch(kind: str, out: Path) -> None:
    """Every astropy issue (or merged PR) through the search API, four-month windows (<1000 each)."""

    headers = {"Accept": "application/vnd.github+json", "User-Agent": "bug-llm-workspace-demo"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    keep = ["number", "title", "body", "state", "state_reason", "created_at", "closed_at", "labels", "assignees", "user", "html_url"]
    seen: set[int] = set()
    with out.open("w", encoding="utf-8") as handle:
        for year in range(2011, datetime.now(timezone.utc).year + 1):
            for start, end in (("01-01", "04-30"), ("05-01", "08-31"), ("09-01", "12-31")):
                page = 1
                while True:
                    query = f"repo:{PROJECT} {kind} created:{year}-{start}..{year}-{end}"
                    url = "https://api.github.com/search/issues?" + urllib.parse.urlencode({"q": query, "per_page": 100, "page": page})
                    for attempt in range(6):
                        try:
                            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
                                data = json.load(response)
                            break
                        except Exception as exc:  # rate limits: wait and retry
                            print(f"  {query} p{page}: {exc}; 60 秒後重試")
                            time.sleep(60)
                    else:
                        raise SystemExit(f"GitHub 搜尋一直失敗：{query}")
                    items = data.get("items", [])
                    for item in items:
                        if item["number"] not in seen:
                            seen.add(item["number"])
                            handle.write(json.dumps({key: item.get(key) for key in keep}, ensure_ascii=False) + "\n")
                    print(f"  {kind} {year}-{start}: {len(seen)}")
                    if len(items) < 100 or page >= 10:
                        break
                    page += 1
                    time.sleep(6 if "Authorization" not in headers else 2.5)
                time.sleep(6 if "Authorization" not in headers else 2.5)


def component(labels: list[dict]) -> str:
    names = [label["name"] for label in labels or []]
    return next((name for name in names if SUBPACKAGE.match(name)), "")


def description(issue: dict) -> str:
    body = (issue.get("body") or "").replace("\r\n", "\n").strip()
    if len(body) > BODY_LIMIT:
        body = body[:BODY_LIMIT] + "\n…（以下省略）"
    return body


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--issues", type=Path, default=None, help="已下載的 issue（每行一筆搜尋結果）")
    parser.add_argument("--prs", type=Path, default=None, help="已下載的已合併 PR（每行一筆搜尋結果）")
    parser.add_argument("--fetch", action="store_true", help="從 GitHub 搜尋 API 下載（可設 GITHUB_TOKEN）")
    parser.add_argument("--patch-tickets", type=Path, default=MAIN_ROOT / "demo_data" / "patch_demo" / "new_tickets_patch_demo.jsonl")
    parser.add_argument("--patch-answers", type=Path, default=MAIN_ROOT / "demo_data" / "patch_demo" / "answer_key_patch_demo.json")
    parser.add_argument("--output-dir", type=Path, default=MAIN_ROOT / "data" / "real_datasets" / "astropy_github")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    issues_path = args.issues or args.output_dir / "github_issues_raw.jsonl"
    prs_path = args.prs or args.output_dir / "github_merged_prs_raw.jsonl"
    if args.fetch:
        fetch("is:issue", issues_path)
        fetch("is:pr is:merged", prs_path)
    issues = {row["number"]: row for row in read_jsonl(issues_path)}
    prs = read_jsonl(prs_path)

    fixers: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for pr in prs:
        for match in CLOSING.finditer(pr.get("body") or ""):
            number = int(match.group(1))
            if number in issues:
                fixers[number].append((pr["number"], pr["user"]["login"]))

    excluded = {issue for issue, _ in PATCH_TICKETS.values()} | set(DUPLICATE_TICKETS)
    duplicate_of = {number: original for number, (original, _) in DUPLICATE_TICKETS.items()}
    history, held_out = [], 0
    for number, issue in sorted(issues.items()):
        if issue["created_at"] >= HISTORY_BEFORE or number in excluded:
            continue
        if issue["title"].strip().lower() in HELD_OUT_TITLES:
            held_out += 1
            continue
        assignees = [user["login"] for user in issue.get("assignees") or []]
        fixed_by = fixers.get(number, [])
        if assignees:
            assignee, source = assignees[0], "github_assignee"
        elif fixed_by:
            assignee, source = fixed_by[0][1], f"author_of_closing_pr_#{fixed_by[0][0]}"
        else:
            assignee, source = "", ""
        labels = [label["name"] for label in issue.get("labels") or []]
        is_duplicate = issue.get("state_reason") == "duplicate" or "Duplicate" in labels
        resolution = "DUPLICATE" if is_duplicate else {"completed": "FIXED", "not_planned": "WONTFIX"}.get(issue.get("state_reason") or "", "")
        history.append({
            "ticket_id": f"astropy#{number}",
            "project": PROJECT,
            "title": issue["title"],
            "description": description(issue),
            "product": "astropy",
            "component": component(issue.get("labels")),
            "labels": labels,
            "severity": "",
            "priority": "",
            "reporter": (issue.get("user") or {}).get("login", ""),
            "assignee": assignee,
            "assignee_source": source,
            "status": issue["state"].upper(),
            "resolution": resolution,
            "duplicate_of": f"astropy#{duplicate_of[number]}" if number in duplicate_of else "",
            "created_at": issue["created_at"],
            "url": issue["html_url"],
            "source": "astropy GitHub issues",
        })

    patch_rows = {row["ticket_id"]: row for row in read_jsonl(args.patch_tickets)}
    patch_answers = {row["ticket_id"]: row for row in json.loads(args.patch_answers.read_text(encoding="utf-8"))["answers"]}
    new_tickets, answers = [], []
    for number, (original, evidence) in DUPLICATE_TICKETS.items():
        issue = issues[number]
        new_tickets.append({
            "ticket_id": f"astropy#{number}", "project": PROJECT, "title": issue["title"],
            "description": description(issue), "product": "astropy", "component": component(issue.get("labels")),
            "reporter": (issue.get("user") or {}).get("login", ""), "created_at": issue["created_at"], "url": issue["html_url"],
        })
        answers.append({
            "ticket_id": f"astropy#{number}", "title": issue["title"], "kind": "duplicate",
            "duplicate_of": f"astropy#{original}", "duplicate_of_title": issues[original]["title"],
            "duplicate_evidence": evidence, "url": issue["html_url"],
        })
    for ticket_id, (issue_number, fixed_files) in PATCH_TICKETS.items():
        row = patch_rows[ticket_id]
        new_tickets.append(row)
        pr_number = int(ticket_id.rsplit("-", 1)[1])
        fixer = next((login for pr, login in fixers.get(issue_number, []) if pr == pr_number), "")
        patch = patch_answers.get(ticket_id, {})
        answers.append({
            "ticket_id": ticket_id, "title": row["title"], "kind": "patch", "duplicate_of": "",
            "github_issue": f"astropy#{issue_number}", "fixed_by": fixer, "fix_pull_request": f"https://github.com/{PROJECT}/pull/{pr_number}",
            "fixed_files": fixed_files, "experiment_patch_sha256": patch.get("experiment_patch_sha256", ""),
            "official_resolved": patch.get("official_resolved", ""), "official_run_id": patch.get("official_run_id", ""),
        })

    write_jsonl(args.output_dir / "history_astropy_github.jsonl", history)
    write_jsonl(args.output_dir / "new_tickets_astropy_github.jsonl", new_tickets)
    with_assignee = sum(1 for row in history if row["assignee"])
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "history_rows": len(history), "history_with_assignee": with_assignee,
        "history_duplicates": sum(1 for row in history if row["resolution"] == "DUPLICATE"),
        "held_out_issues_removed": held_out, "answers": answers,
    }
    (args.output_dir / "answer_key_astropy_github.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# astropy 完整示範：答案",
        "",
        "這份只用來核對，**不要匯入**。",
        "",
        f"歷史：{len(history)} 筆 astropy GitHub issue（{HISTORY_BEFORE} 以前建立），其中 {with_assignee} 筆有負責人、"
        f"{summary['history_duplicates']} 筆被標成重複。已移除新 Ticket 自己的 issue，以及 SWE-bench 驗證集／保留測試集的 {held_out} 筆 issue。",
        "",
        "## 重複偵測（第 2 步）",
        "",
        "| Ticket | 標題 | 真正重複的對象 | 依據 |",
        "|---|---|---|---|",
    ]
    for answer in answers:
        if answer["kind"] == "duplicate":
            lines.append(f"| {answer['ticket_id']} | {answer['title'][:60]} | {answer['duplicate_of']}：{answer['duplicate_of_title'][:60]} | {answer['duplicate_evidence']} |")
    lines += [
        "",
        "## 負責人、錯誤定位、補丁（第 4–7 步）",
        "",
        "這 6 張不是重複（重複偵測請按「不是這張」）。",
        "",
        "| Ticket | 標題 | 修好它的人 | 真正修改的檔案 | 補丁實驗官方判定 |",
        "|---|---|---|---|---|",
    ]
    for answer in answers:
        if answer["kind"] == "patch":
            verdict = "✅ 修好" if str(answer["official_resolved"]) == "True" else "—"
            lines.append(f"| {answer['ticket_id']} | {answer['title'][:50]} | {answer['fixed_by']} | {'、'.join(answer['fixed_files'])} | {verdict}（`{answer['experiment_patch_sha256'][:12]}`） |")
    lines += [
        "",
        "`astropy__astropy-13572` 的重複候選可能會出現 astropy#10680（Invalid rotation_matrix creation in earth_orientation.py）："
        "維護者把它標成和這張同一個 issue（#6583）重複，所以排在前面是合理的。這張是要修的那一張，請按「不是這張」繼續。",
        "",
        "## 解讀時要注意",
        "",
        "- 「負責人」：issue 有 GitHub assignee 就用它，否則用關閉這個 issue 的 PR 作者（`assignee_source` 有標）。工作台只用比這張 Ticket 早建立的歷史推薦。",
        "- 優先級沒有標準答案：astropy 不標優先級，PRI 只用 Eclipse 訓練，這裡是跨資料集推測。",
        "- 重複偵測的 SBERT 用 Firefox 訓練，套在 astropy 是跨資料集。",
        "- 6 張補丁 Ticket 來自 SWE-bench 開發集，補丁方法在這 36 題上調過，不能當泛化證據；工作台重新產生的補丁只有 sha256 相同才引用官方判定。",
        "- 以上是示範，不是評估。",
    ]
    (args.output_dir / "answer_key_astropy_github.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"歷史 {len(history)} 筆（有負責人 {with_assignee}）、新 Ticket {len(new_tickets)} 張 → {args.output_dir}")


if __name__ == "__main__":
    main()
