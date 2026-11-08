from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from scripts.prepare_stage3_symbol_gold_source import (  # noqa: E402
    join_development_patches,
    patch_features,
    select_source_tickets,
)


class PrepareStage3SymbolGoldSourceTests(unittest.TestCase):
    def test_join_rejects_snapshot_identity_mismatch(self) -> None:
        tickets = [{"ticket_id": "t1", "repo": "r/a", "base_commit": "a" * 40}]
        patches = [
            {
                "instance_id": "t1",
                "repo": "r/a",
                "base_commit": "b" * 40,
                "patch": _patch("a.py"),
            }
        ]
        with self.assertRaisesRegex(ValueError, "Base commit mismatch"):
            join_development_patches(tickets, patches)

    def test_selection_is_reproducible_balanced_and_feature_covered(self) -> None:
        rows = []
        for repo_index in range(3):
            for ticket_index in range(5):
                rows.append(
                    {
                        "ticket_id": f"t-{repo_index}-{ticket_index}",
                        "repo": f"repo/{repo_index}",
                        "base_commit": f"{repo_index + 1:040x}",
                        "patch": _patch(
                            f"src/{ticket_index}.py",
                            second_path=(f"tests/{ticket_index}.py" if ticket_index == 0 else ""),
                            pure_addition=ticket_index == 1,
                        ),
                    }
                )
        first, manifest = select_source_tickets(
            rows,
            seed=7,
            per_repository=2,
            min_feature_tickets=2,
        )
        second, _ = select_source_tickets(
            list(reversed(rows)),
            seed=7,
            per_repository=2,
            min_feature_tickets=2,
        )
        self.assertEqual(
            [row["ticket_id"] for row in first],
            [row["ticket_id"] for row in second],
        )
        self.assertGreaterEqual(manifest["selected_repository_count"], 3)
        self.assertGreaterEqual(manifest["feature_counts"]["pure_addition"], 2)
        self.assertGreaterEqual(manifest["feature_counts"]["multi_file"], 2)

    def test_patch_feature_detection_uses_hunk_evidence(self) -> None:
        features = patch_features(
            _patch("src/a.py", second_path="src/b.py", pure_addition=True)
        )
        self.assertIn("pure_addition", features)
        self.assertIn("multi_file", features)
        self.assertIn("deletion_evidence", features)


def _patch(path: str, *, second_path: str = "", pure_addition: bool = False) -> str:
    body = (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        + ("@@ -1 +1,2 @@\n line\n+added\n" if pure_addition else "@@ -1 +1 @@\n-old\n+new\n")
    )
    if second_path:
        body += (
            f"diff --git a/{second_path} b/{second_path}\n"
            f"--- a/{second_path}\n"
            f"+++ b/{second_path}\n"
            "@@ -1 +1 @@\n-old\n+new\n"
        )
    return body


if __name__ == "__main__":
    unittest.main()
