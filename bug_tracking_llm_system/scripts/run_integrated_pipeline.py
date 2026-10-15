#!/usr/bin/env python3
"""Command-line entry for the integrated ticket workflow.

Examples (run from bug_tracking_llm_system/):

    python3 scripts/run_integrated_pipeline.py --config integration_config.json check
    python3 scripts/run_integrated_pipeline.py --config integration_config.json import-history --input data/historical_tickets.jsonl
    python3 scripts/run_integrated_pipeline.py --config integration_config.json build-index --project smoke
    python3 scripts/run_integrated_pipeline.py --config integration_config.json run --input tickets.jsonl --project smoke
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from integration.config import load_integration_config  # noqa: E402
from integration.importers import parse_import_payload  # noqa: E402
from integration.pipeline import STAGE_LABELS, STAGES, IntegratedPipeline  # noqa: E402
from integration.status import readiness_report  # noqa: E402


STATUS_TEXT = {"done": "完成", "fallback": "備援", "skipped": "略過", "failed": "失敗", "pending": "-", "running": "執行中"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the integrated ticket workflow with the team's implemented modules.")
    parser.add_argument("--config", default=None, help="integration_config.json path (default: built-in defaults).")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="Show which modules / trained artifacts are available.")

    history = sub.add_parser("import-history", help="Add historical tickets used by duplicate detection and assignee routing.")
    history.add_argument("--input", required=True)

    index = sub.add_parser("build-index", help="Pre-build the code index of a configured project.")
    index.add_argument("--project", required=True)

    run = sub.add_parser("run", help="Import tickets and process them.")
    run.add_argument("--input", required=True, help="JSON / JSONL / CSV with tickets.")
    run.add_argument("--project", default="", help="Project name from the config (repository used for localization).")
    run.add_argument(
        "--assume-not-duplicate",
        action="store_true",
        help="Continue tickets waiting for duplicate review as non-duplicates (decision is recorded as made by the CLI).",
    )
    run.add_argument("--output", default="", help="Optional JSON summary output path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_integration_config(args.config)
    pipeline = IntegratedPipeline(config)

    if args.command == "check":
        report = readiness_report(config, store_history_path=pipeline.store.history_path)
        for name in STAGES:
            stage = report["stages"][name]
            print(f"\n[{'就緒' if stage['ready'] else '未就緒'}] {stage['title']}")
            for check in stage["checks"]:
                mark = "OK " if check["ok"] else ("-- " if not check["required"] else "NG ")
                print(f"  {mark}{check['label']}  {check['detail']}")
        return 0

    if args.command == "import-history":
        path = Path(args.input)
        rows = parse_import_payload(path.read_text(encoding="utf-8"), filename=path.name)
        total = pipeline.import_history(rows)
        print(f"匯入 {len(rows)} 筆歷史 Ticket，工作台歷史資料共 {total} 筆：{pipeline.store.history_path}")
        return 0

    if args.command == "build-index":
        project = config.project(args.project)
        if project is None:
            print(f"設定檔中沒有專案：{args.project}", file=sys.stderr)
            return 2
        print(json.dumps(pipeline.localizer.build_index(project), ensure_ascii=False, indent=2))
        return 0

    path = Path(args.input)
    raws = parse_import_payload(path.read_text(encoding="utf-8"), filename=path.name)
    records = pipeline.import_raw(raws, source=f"cli:{path.name}", project=args.project)
    print(f"匯入 {len(records)} 筆 Ticket（store：{pipeline.store.root}）")
    final = pipeline.process_batch(
        [record["key"] for record in records],
        assume_not_duplicate=args.assume_not_duplicate,
        progress=print,
    )

    print("\n" + " | ".join(["Ticket", "狀態", *[STAGE_LABELS[name] for name in STAGES], "結果摘要"]))
    for record in final:
        stages = record["stages"]
        priority = (stages["priority"].get("output") or {}).get("predicted_priority", "")
        assignee_output = stages["assignee"].get("output") or {}
        assignee = assignee_output.get("suggested_assignee") or assignee_output.get("assignee") or ""
        symbols = (stages["localization"].get("output") or {}).get("ranked_symbols") or []
        top = f"{symbols[0]['file_path']}::{symbols[0].get('symbol_qualified_name', '')}" if symbols else ""
        summary = " ".join(item for item in (priority, assignee, top) if item)
        print(
            " | ".join(
                [record["ticket_id"], record["status"], *[STATUS_TEXT.get(stages[name].get("status"), "?") for name in STAGES], summary]
            )
        )
    waiting = [record["ticket_id"] for record in final if record["status"] == "awaiting_duplicate_review"]
    if waiting:
        print(
            f"\n{len(waiting)} 張 Ticket 有相似的歷史 Ticket，正等待確認是否重複：{', '.join(waiting)}。"
            "\n請在網頁介面（workspace/server.py）確認；批次測試可加 --assume-not-duplicate。"
        )
    if args.output:
        Path(args.output).write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n完整結果：{args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
