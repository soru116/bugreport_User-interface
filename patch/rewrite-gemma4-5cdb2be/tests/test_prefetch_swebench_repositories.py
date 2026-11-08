from __future__ import annotations

import unittest

from scripts.prefetch_swebench_repositories import (
    collect_repositories,
    collect_repository_commits,
    collect_repository_pull_numbers,
    safe_name,
)


class PrefetchSWEbenchRepositoriesTest(unittest.TestCase):
    def test_collects_explicit_or_inferred_pull_numbers(self) -> None:
        rows = [
            {"repo": "org/a", "ticket_id": "org__a-12", "pull_number": ""},
            {"repo": "org/a", "ticket_id": "org__a-13", "pull_number": "99"},
            {"repo": "org/b", "ticket_id": "not-numeric"},
        ]
        self.assertEqual(
            collect_repository_pull_numbers(rows),
            {"org/a": ["12", "99"], "org/b": []},
        )

    def test_collects_unique_required_commits_by_repository(self) -> None:
        rows = [
            {"repo": "org/a", "base_commit": "aaa"},
            {"repo": "org/a", "base_commit": "bbb"},
            {"repo": "org/a", "base_commit": "aaa"},
            {"repo": "org/b", "base_commit": "ccc"},
        ]
        self.assertEqual(
            collect_repository_commits(rows),
            {"org/a": ["aaa", "bbb"], "org/b": ["ccc"]},
        )

    def test_collects_unique_repositories_without_gold(self) -> None:
        repositories = collect_repositories(
            [
                {"repo": "owner/repo", "repository_url": "https://github.com/owner/repo"},
                {"repo": "owner/repo", "repository_url": "https://github.com/owner/repo"},
                {"repo": "other/project"},
            ]
        )
        self.assertEqual(len(repositories), 2)
        self.assertEqual(repositories["other/project"], "https://github.com/other/project")

    def test_safe_name_matches_runner_layout(self) -> None:
        self.assertEqual(safe_name("owner/repo"), "owner__repo")

    def test_rejects_conflicting_repository_urls(self) -> None:
        with self.assertRaisesRegex(ValueError, "conflicting URLs"):
            collect_repositories(
                [
                    {"repo": "owner/repo", "repository_url": "https://example.com/a"},
                    {"repo": "owner/repo", "repository_url": "https://example.com/b"},
                ]
            )


if __name__ == "__main__":
    unittest.main()
