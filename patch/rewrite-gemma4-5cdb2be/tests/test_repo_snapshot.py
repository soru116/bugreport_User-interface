"""Tests for utils.repo_snapshot: everything here uses a local temp git repo
(created with plain `git init`/`git commit`) -- no network access, matching
the rest of this project's offline-test policy.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"

for import_path in (str(SRC_ROOT), str(PROJECT_ROOT)):
    if import_path not in sys.path:
        sys.path.insert(0, import_path)

from utils.repo_snapshot import (
    RepoSnapshotError,
    _safe_name,
    _verbatim_marker_path,
    materialize_commit_snapshot,
    read_file_text,
    resolve_repository_path,
)


def _run(args: list[str], *, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _init_repo_with_one_commit(repo_dir: Path, *, file_name: str = "module.py", file_text: str = "x = 1\n") -> str:
    repo_dir.mkdir(parents=True, exist_ok=True)
    _run(["init"], cwd=repo_dir)
    _run(["config", "user.email", "test@example.com"], cwd=repo_dir)
    _run(["config", "user.name", "Test"], cwd=repo_dir)
    target_file = repo_dir / file_name
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text(file_text, encoding="utf-8")
    _run(["add", "."], cwd=repo_dir)
    _run(["commit", "-m", "initial"], cwd=repo_dir)
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_dir), capture_output=True, text=True, check=True)
    return result.stdout.strip()


class ResolveRepositoryPathTests(unittest.TestCase):
    def test_uses_local_repo_path_when_present(self) -> None:
        with TemporaryDirectory() as tmp:
            repo_dir = Path(tmp) / "somewhere" / "checked-out"
            _init_repo_with_one_commit(repo_dir)
            resolved = resolve_repository_path(
                {"local_repo_path": str(repo_dir)}, repo_cache_dir=Path(tmp) / "unused-cache"
            )
            self.assertEqual(resolved, repo_dir.resolve())

    def test_local_repo_path_missing_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            with self.assertRaises(RepoSnapshotError):
                resolve_repository_path(
                    {"local_repo_path": str(Path(tmp) / "does-not-exist")},
                    repo_cache_dir=Path(tmp) / "cache",
                )

    def test_falls_back_to_repo_cache_dir(self) -> None:
        with TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            repo_dir = cache_dir / "owner__project"
            _init_repo_with_one_commit(repo_dir)
            resolved = resolve_repository_path({"repo": "owner/project"}, repo_cache_dir=cache_dir)
            self.assertEqual(resolved, repo_dir.resolve())

    def test_missing_cache_without_clone_missing_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            with self.assertRaises(RepoSnapshotError):
                resolve_repository_path(
                    {"repo": "owner/project"}, repo_cache_dir=Path(tmp) / "empty-cache", clone_missing=False
                )

    def test_no_repo_field_and_no_local_path_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            with self.assertRaises(RepoSnapshotError):
                resolve_repository_path({}, repo_cache_dir=Path(tmp) / "cache")


class MaterializeCommitSnapshotTests(unittest.TestCase):
    def test_checks_out_the_requested_commit_detached(self) -> None:
        with TemporaryDirectory() as tmp:
            source_repo = Path(tmp) / "source"
            commit = _init_repo_with_one_commit(source_repo, file_text="value = 1\n")

            snapshot_path = materialize_commit_snapshot(
                source_repo,
                repo="owner/project",
                base_commit=commit,
                snapshot_cache_dir=Path(tmp) / "snapshots",
            )

            self.assertTrue((snapshot_path / "module.py").exists())
            self.assertEqual((snapshot_path / "module.py").read_text(encoding="utf-8"), "value = 1\n")

    def test_reuses_existing_snapshot_at_the_same_commit(self) -> None:
        with TemporaryDirectory() as tmp:
            source_repo = Path(tmp) / "source"
            commit = _init_repo_with_one_commit(source_repo)
            snapshot_cache_dir = Path(tmp) / "snapshots"

            first = materialize_commit_snapshot(
                source_repo, repo="owner/project", base_commit=commit, snapshot_cache_dir=snapshot_cache_dir
            )
            second = materialize_commit_snapshot(
                source_repo, repo="owner/project", base_commit=commit, snapshot_cache_dir=snapshot_cache_dir
            )
            self.assertEqual(first, second)

    def test_short_commit_prefix_resolves_via_rev_parse(self) -> None:
        with TemporaryDirectory() as tmp:
            source_repo = Path(tmp) / "source"
            commit = _init_repo_with_one_commit(source_repo)

            snapshot_path = materialize_commit_snapshot(
                source_repo,
                repo="owner/project",
                base_commit=commit[:10],
                snapshot_cache_dir=Path(tmp) / "snapshots",
            )
            self.assertTrue(snapshot_path.exists())

    def test_unknown_commit_raises_without_fetch_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            source_repo = Path(tmp) / "source"
            _init_repo_with_one_commit(source_repo)

            with self.assertRaises(RepoSnapshotError):
                materialize_commit_snapshot(
                    source_repo,
                    repo="owner/project",
                    base_commit="0" * 40,
                    snapshot_cache_dir=Path(tmp) / "snapshots",
                    fetch_missing_commits=False,
                )

    def test_empty_base_commit_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            source_repo = Path(tmp) / "source"
            _init_repo_with_one_commit(source_repo)
            with self.assertRaises(RepoSnapshotError):
                materialize_commit_snapshot(
                    source_repo, repo="owner/project", base_commit="", snapshot_cache_dir=Path(tmp) / "snapshots"
                )

    def test_not_a_git_checkout_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            not_a_repo = Path(tmp) / "plain-dir"
            not_a_repo.mkdir()
            with self.assertRaises(RepoSnapshotError):
                materialize_commit_snapshot(
                    not_a_repo,
                    repo="owner/project",
                    base_commit="deadbeef",
                    snapshot_cache_dir=Path(tmp) / "snapshots",
                )


class ReadFileTextTests(unittest.TestCase):
    def test_reads_file_content_from_snapshot(self) -> None:
        with TemporaryDirectory() as tmp:
            source_repo = Path(tmp) / "source"
            commit = _init_repo_with_one_commit(source_repo, file_name="pkg/module.py", file_text="def f():\n    pass\n")
            snapshot_path = materialize_commit_snapshot(
                source_repo, repo="owner/project", base_commit=commit, snapshot_cache_dir=Path(tmp) / "snapshots"
            )
            text = read_file_text(snapshot_path, "pkg/module.py")
            self.assertEqual(text, "def f():\n    pass\n")

    def test_missing_file_raises_repo_snapshot_error_not_os_error(self) -> None:
        with TemporaryDirectory() as tmp:
            source_repo = Path(tmp) / "source"
            commit = _init_repo_with_one_commit(source_repo)
            snapshot_path = materialize_commit_snapshot(
                source_repo, repo="owner/project", base_commit=commit, snapshot_cache_dir=Path(tmp) / "snapshots"
            )
            with self.assertRaises(RepoSnapshotError):
                read_file_text(snapshot_path, "does_not_exist.py")

    def test_path_traversal_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            source_repo = Path(tmp) / "source"
            commit = _init_repo_with_one_commit(source_repo)
            snapshot_path = materialize_commit_snapshot(
                source_repo, repo="owner/project", base_commit=commit, snapshot_cache_dir=Path(tmp) / "snapshots"
            )
            with self.assertRaises(RepoSnapshotError):
                read_file_text(snapshot_path, "../../../../etc/passwd")


class VerbatimCheckoutTests(unittest.TestCase):
    """A snapshot must hold the commit's bytes, not a line-ending translation.

    A snapshot exists so that downstream stages can say "this is exactly the
    source that commit contains". Git for Windows defaults to
    ``core.autocrlf=true``, which rewrites LF to CRLF on checkout, so every file
    in the tree then differs from the blob the commit stores. Any consumer that
    checks the file against ``git show <commit>:<path>`` rejects the whole tree,
    and any consumer that does not check it silently patches CRLF source and
    produces a diff that touches every line it rebuilds.

    Found while running the Stage-4 pipeline on Windows: 15 of 15 candidate
    targets were rejected with "Source differs from base_commit", which reads
    like modified source rather than a checkout setting.
    """

    def _blob(self, repo: Path, commit: str, path: str) -> bytes:
        return subprocess.run(["git", "-C", str(repo), "show", f"{commit}:{path}"],
                              capture_output=True, check=True).stdout

    def test_snapshot_disables_line_ending_translation(self) -> None:
        with TemporaryDirectory() as tmp:
            source_repo = Path(tmp) / "source"
            commit = _init_repo_with_one_commit(source_repo, file_text="a = 1\nb = 2\n")
            snapshot = materialize_commit_snapshot(
                source_repo, repo="owner/project", base_commit=commit,
                snapshot_cache_dir=Path(tmp) / "snapshots")
            for key, expected in (("core.autocrlf", "false"), ("core.eol", "lf")):
                value = subprocess.run(["git", "-C", str(snapshot), "config", "--get", key],
                                       capture_output=True, text=True, check=True).stdout.strip()
                self.assertEqual(value, expected, key)

    def test_worktree_bytes_equal_the_stored_blob(self) -> None:
        with TemporaryDirectory() as tmp:
            source_repo = Path(tmp) / "source"
            commit = _init_repo_with_one_commit(source_repo, file_text="a = 1\nb = 2\n")
            snapshot = materialize_commit_snapshot(
                source_repo, repo="owner/project", base_commit=commit,
                snapshot_cache_dir=Path(tmp) / "snapshots")
            self.assertEqual((snapshot / "module.py").read_bytes(),
                             self._blob(snapshot, commit, "module.py"))

    def _make_stale_snapshot(self, tmp: Path, text: str = "a = 1\nb = 2\n"):
        """Build the exact state a Windows run leaves behind.

        The snapshot is checked out by git itself under core.autocrlf=true, so
        the worktree holds CRLF and the index holds the stat data git wrote for
        those converted files. Git therefore considers the tree clean, and keeps
        saying so after the setting is flipped, because a matching stat lets it
        skip reading the file at all. Writing CRLF by hand would not reproduce
        this: git detects a hand-edited file immediately.
        """

        source_repo = tmp / "source"
        cache = tmp / "snapshots"
        commit = _init_repo_with_one_commit(source_repo, file_text=text)
        snapshot = cache / f"{_safe_name('owner/project')}__{commit[:16]}"
        cache.mkdir(parents=True, exist_ok=True)
        _run(["clone", "--shared", "--no-checkout", str(source_repo), str(snapshot)], cwd=cache)
        _run(["-C", str(snapshot), "config", "core.autocrlf", "true"], cwd=cache)
        _run(["-C", str(snapshot), "checkout", "--detach", commit], cwd=cache)

        # Age the file, then let git re-record its stat, so the racily-clean
        # heuristic stops forcing a content read on every status.
        os.utime(snapshot / "module.py", (946684800, 946684800))
        subprocess.run(["git", "-C", str(snapshot), "update-index", "--refresh"],
                       capture_output=True)
        (snapshot / ".git" / "index").touch()
        return source_repo, cache, commit, snapshot

    def test_git_status_alone_cannot_detect_the_problem(self) -> None:
        """Documents why the repair is unconditional rather than status-driven."""

        with TemporaryDirectory() as tmp:
            _, _, commit, snapshot = self._make_stale_snapshot(Path(tmp))
            status = subprocess.run(["git", "-C", str(snapshot), "status", "--porcelain"],
                                    capture_output=True, text=True, check=True).stdout
            self.assertEqual(status.strip(), "", "precondition: git must claim the tree is clean")
            self.assertNotEqual((snapshot / "module.py").read_bytes(),
                                self._blob(snapshot, commit, "module.py"))

    def test_reused_snapshot_without_a_marker_is_rewritten(self) -> None:
        with TemporaryDirectory() as tmp:
            source_repo, cache, commit, snapshot = self._make_stale_snapshot(Path(tmp))
            again = materialize_commit_snapshot(
                source_repo, repo="owner/project", base_commit=commit, snapshot_cache_dir=cache)
            self.assertEqual(again, snapshot)
            self.assertEqual((snapshot / "module.py").read_bytes(),
                             self._blob(snapshot, commit, "module.py"))

    def test_repair_writes_the_marker_so_the_next_reuse_is_cheap(self) -> None:
        with TemporaryDirectory() as tmp:
            source_repo, cache, commit, snapshot = self._make_stale_snapshot(Path(tmp))
            self.assertFalse(_verbatim_marker_path(snapshot).exists())
            materialize_commit_snapshot(source_repo, repo="owner/project",
                                        base_commit=commit, snapshot_cache_dir=cache)
            marker = _verbatim_marker_path(snapshot)
            self.assertTrue(marker.is_file())
            self.assertEqual(marker.read_text(encoding="utf-8").strip(), commit)

    def test_a_fresh_snapshot_is_marked_immediately(self) -> None:
        with TemporaryDirectory() as tmp:
            source_repo = Path(tmp) / "source"
            commit = _init_repo_with_one_commit(source_repo, file_text="a = 1\n")
            snapshot = materialize_commit_snapshot(
                source_repo, repo="owner/project", base_commit=commit,
                snapshot_cache_dir=Path(tmp) / "snapshots")
            self.assertEqual(_verbatim_marker_path(snapshot).read_text(encoding="utf-8").strip(),
                             commit)

    def test_the_marker_is_not_a_worktree_file(self) -> None:
        """An untracked file would fail the caller's clean-repository check."""

        with TemporaryDirectory() as tmp:
            source_repo = Path(tmp) / "source"
            commit = _init_repo_with_one_commit(source_repo, file_text="a = 1\n")
            snapshot = materialize_commit_snapshot(
                source_repo, repo="owner/project", base_commit=commit,
                snapshot_cache_dir=Path(tmp) / "snapshots")
            status = subprocess.run(["git", "-C", str(snapshot), "status", "--porcelain"],
                                    capture_output=True, text=True, check=True).stdout
            self.assertEqual(status.strip(), "")

    def test_repaired_snapshot_is_clean_for_the_pipeline_gate(self) -> None:
        """A consumer refuses a dirty tree, so the repair must leave none."""

        with TemporaryDirectory() as tmp:
            source_repo, cache, commit, snapshot = self._make_stale_snapshot(Path(tmp))
            materialize_commit_snapshot(source_repo, repo="owner/project",
                                        base_commit=commit, snapshot_cache_dir=cache)
            status = subprocess.run(["git", "-C", str(snapshot), "status", "--porcelain"],
                                    capture_output=True, text=True, check=True).stdout
            self.assertEqual(status.strip(), "")

    def test_untracked_leftovers_are_reported_not_silently_accepted(self) -> None:
        """A reset cannot remove untracked files; say so rather than proceed."""

        with TemporaryDirectory() as tmp:
            source_repo, cache, commit, snapshot = self._make_stale_snapshot(Path(tmp))
            (snapshot / "stray_output.py").write_text("leftover\n", encoding="utf-8")
            with self.assertRaises(RepoSnapshotError) as ctx:
                materialize_commit_snapshot(source_repo, repo="owner/project",
                                            base_commit=commit, snapshot_cache_dir=cache)
            self.assertIn("byte-identical", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
