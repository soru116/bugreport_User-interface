"""Put two or more Stage-4 batch runs side by side.

    python scripts/compare_stage4_summaries.py runs/A runs/B

Reads each run's run_config.json and summary.json. Candidate-level counters
are shown as they are recorded; nothing is merged or re-bucketed, so a
"generated" count here is still NOT a repair rate.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

CONFIG_KEYS = ("model", "fim_transport", "include_original_body", "ticket_count",
               "candidates", "seed", "num_ctx", "max_new_tokens")
COUNTER_KEYS = ("patch_status_counts", "candidate_stop_reasons",
                "candidate_rejection_reasons")
SCALAR_KEYS = ("attempted", "generated", "candidate_bodies_trimmed",
               "body_lines_median")


def load(run: Path) -> tuple[dict, dict]:
    def read(name):
        path = run / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return read("run_config.json"), read("summary.json")


def main() -> None:
    runs = [Path(a) for a in sys.argv[1:]]
    if len(runs) < 2:
        sys.exit(__doc__)
    loaded = [load(r) for r in runs]
    width = max(22, *(len(r.name) + 2 for r in runs))

    def row(label, values):
        print(f"{label:<46}" + "".join(f"{str(v):<{width}}" for v in values))

    row("run", [r.name for r in runs])
    print("-" * (46 + width * len(runs)))
    for key in CONFIG_KEYS:
        row(key, [c.get(key, "-") for c, _ in loaded])
    print()
    for key in SCALAR_KEYS:
        row(key, [s.get(key, "-") for _, s in loaded])
    for key in COUNTER_KEYS:
        print(f"\n{key}")
        labels = sorted({k for _, s in loaded for k in (s.get(key) or {})})
        for label in labels:
            row("  " + label[:43], [(s.get(key) or {}).get(label, 0) for _, s in loaded])
        if not labels:
            row("  (none)", ["" for _ in loaded])


if __name__ == "__main__":
    main()
