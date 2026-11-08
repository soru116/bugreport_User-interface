from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.verify_swebench_repository_commits import safe_name, verify_base_commits


class VerifySWEbenchRepositoryCommitsTest(unittest.TestCase):
    def test_counts_reachable_commits_without_reading_gold(self) -> None:
        rows = [
            {"repo": "owner/repo", "base_commit": "aaa"},
            {"repo": "owner/repo", "base_commit": "bbb"},
            {"repo": "owner/repo", "base_commit": "aaa"},
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            cache_dir = Path(temporary_directory)
            (cache_dir / "owner__repo" / ".git").mkdir(parents=True)
            result = verify_base_commits(
                rows,
                cache_dir,
                commit_exists=lambda _path, commit: commit == "aaa",
            )
        self.assertEqual(result["ticket_count"], 3)
        self.assertEqual(result["base_commit_count"], 2)
        self.assertEqual(result["reachable_count"], 1)
        self.assertEqual(result["missing_count"], 1)

    def test_reports_all_commits_missing_when_repository_is_absent(self) -> None:
        rows = [{"repo": "owner/repo", "base_commit": "aaa"}]
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = verify_base_commits(rows, Path(temporary_directory))
        self.assertEqual(result["repositories"][0]["status"], "repository_missing")
        self.assertEqual(result["missing_count"], 1)

    def test_requires_repo_and_base_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(ValueError, "repo and base_commit"):
                verify_base_commits([{"repo": "owner/repo"}], Path(temporary_directory))

    def test_safe_name_matches_repository_cache_layout(self) -> None:
        self.assertEqual(safe_name("owner/repo"), "owner__repo")


if __name__ == "__main__":
    unittest.main()
