from __future__ import annotations

import csv
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
for import_path in (str(PROJECT_ROOT), str(SRC_ROOT)):
    if import_path not in sys.path:
        sys.path.insert(0, import_path)


from scripts.build_symbol_gold import (  # noqa: E402
    build_symbol_gold_records,
    main,
    safe_name,
)
from utils.symbol_gold import SymbolGoldRecordV1  # noqa: E402


REPO_NAME = "example/project"


class BuildSymbolGoldTests(unittest.TestCase):
    def test_cli_reads_base_commit_and_writes_gold_and_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_cache = root / "repos"
            repository = repo_cache / safe_name(REPO_NAME)
            repository.mkdir(parents=True)
            _git(repository, "init")
            _git(repository, "config", "user.email", "tests@example.com")
            _git(repository, "config", "user.name", "Tests")
            source_path = repository / "src" / "service.py"
            source_path.parent.mkdir()
            source_path.write_text(
                "class Service:\n"
                "    def run(self):\n"
                "        return 1\n",
                encoding="utf-8",
            )
            _git(repository, "add", "src/service.py")
            _git(repository, "commit", "-m", "base")
            commit = _git(repository, "rev-parse", "HEAD").stdout.strip()
            patch = (
                "diff --git a/src/service.py b/src/service.py\n"
                "--- a/src/service.py\n"
                "+++ b/src/service.py\n"
                "@@ -1,3 +1,3 @@\n"
                " class Service:\n"
                "     def run(self):\n"
                "-        return 1\n"
                "+        return 2\n"
                "diff --git a/src/new.py b/src/new.py\n"
                "new file mode 100644\n"
                "--- /dev/null\n"
                "+++ b/src/new.py\n"
                "@@ -0,0 +1 @@\n"
                "+VALUE = 1\n"
            )
            tickets_path = root / "tickets.jsonl"
            output_path = root / "symbol-gold.jsonl"
            audit_path = root / "audit.csv"
            tickets_path.write_text(
                json.dumps(
                    {
                        "ticket_id": "ticket-1",
                        "repo": REPO_NAME,
                        "base_commit": commit,
                        "patch": patch,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                main(
                    [
                        "--tickets",
                        str(tickets_path),
                        "--repo-cache-dir",
                        str(repo_cache),
                        "--output",
                        str(output_path),
                        "--audit-csv",
                        str(audit_path),
                    ]
                )

            payloads = [
                json.loads(line)
                for line in output_path.read_text(encoding="utf-8").splitlines()
            ]
            records = [SymbolGoldRecordV1.from_dict(row) for row in payloads]
            with audit_path.open("r", encoding="utf-8", newline="") as handle:
                audit_rows = list(csv.DictReader(handle))
            summary = json.loads(stdout.getvalue())

        self.assertEqual(len(records), 2)
        by_status = {record.mapping_status: record for record in records}
        self.assertEqual(by_status["mapped"].qualified_name, "Service.run")
        self.assertEqual(by_status["mapped"].base_commit, commit)
        self.assertEqual(by_status["mapped"].provenance.matched_old_lines, (3,))
        self.assertEqual(by_status["excluded"].exclusion_reason, "new_file")
        self.assertEqual(len(audit_rows), 2)
        self.assertTrue(all(row["patch_sha256"].startswith("sha256:") for row in audit_rows))
        self.assertTrue(all(row["review_status"] == "" for row in audit_rows))
        self.assertIn("qualified_name_correct", audit_rows[0])
        self.assertIn("review_note", audit_rows[0])
        self.assertEqual(summary["ticket_count"], 1)
        self.assertEqual(summary["mapped_count"], 1)
        self.assertEqual(summary["excluded_count"], 1)

    def test_missing_repository_produces_explicit_excluded_gold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_cache = Path(tmp) / "repos"
            repo_cache.mkdir()
            tickets = [
                {
                    "ticket_id": "ticket-missing",
                    "repo": REPO_NAME,
                    "base_commit": "a" * 40,
                    "patch": (
                        "diff --git a/src/missing.py b/src/missing.py\n"
                        "--- a/src/missing.py\n"
                        "+++ b/src/missing.py\n"
                        "@@ -1 +1 @@\n"
                        "-old\n"
                        "+new\n"
                    ),
                }
            ]

            records, summary = build_symbol_gold_records(
                tickets,
                repo_cache_dir=repo_cache,
            )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].mapping_status, "excluded")
        self.assertEqual(records[0].exclusion_reason, "missing_base_file")
        self.assertEqual(summary["exclusion_reasons"], {"missing_base_file": 1})

    def test_invalid_ticket_commit_is_rejected_before_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_cache = Path(tmp) / "repos"
            repo_cache.mkdir()
            tickets = [
                {
                    "ticket_id": "ticket-invalid",
                    "repo": REPO_NAME,
                    "base_commit": "HEAD; remove-everything",
                    "patch": "diff --git a/a.py b/a.py\n",
                }
            ]
            with self.assertRaisesRegex(ValueError, "invalid Git base_commit"):
                build_symbol_gold_records(tickets, repo_cache_dir=repo_cache)


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )


if __name__ == "__main__":
    unittest.main()
