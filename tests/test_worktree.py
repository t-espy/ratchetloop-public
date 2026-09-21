"""Worktree path, repo lock, orphan sweep, isolation from the primary checkout, and never resetting
an existing task branch. Carried from predecessor's p1-worktree tests; its kernel-driven cases return
with ratchetloop's own loop in Phase 3."""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from ratchetloop.gitops import GitError, git
from ratchetloop.worktree import (
    _acquire_repo_lock, _add_worktree, _release_repo_lock, _remove_worktree, _sweep_orphans,
    _worktree_path, repo_lock,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=r, check=True)
    (r / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=r, check=True)
    return r


def test_worktree_path_is_outside_the_repo_and_keyed_by_repo(repo, tmp_path):
    wt = _worktree_path(repo, "t")
    assert repo.resolve() not in wt.parents
    other = tmp_path / "other" / "repo"
    other.mkdir(parents=True)
    assert _worktree_path(other, "t") != wt  # two checkouts named "repo" never share (codex #440-5)


def test_lock_is_exclusive(repo):
    held = _acquire_repo_lock(repo)
    assert held is not None
    try:
        assert _acquire_repo_lock(repo) is None
    finally:
        _release_repo_lock(held)
    again = _acquire_repo_lock(repo)
    assert again is not None
    _release_repo_lock(again)


def test_repo_lock_waits_then_times_out(repo):
    held = _acquire_repo_lock(repo)
    try:
        t0 = time.monotonic()
        with pytest.raises(TimeoutError, match="repo lock"):
            with repo_lock(repo, wait_s=0.3):
                pass
        assert time.monotonic() - t0 >= 0.3
    finally:
        _release_repo_lock(held)
    with repo_lock(repo, wait_s=0.3):
        assert _acquire_repo_lock(repo) is None


def test_add_worktree_never_touches_the_primary_checkout(repo):
    (repo / "operator.txt").write_text("live dirt ok\n")
    branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
    wt = _worktree_path(repo, "iso")
    _add_worktree(repo, wt, "ratchetloop/iso", "master", False)
    (wt / "change.txt").write_text("x\n")
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == branch
    assert (repo / "operator.txt").read_text() == "live dirt ok\n"
    assert not (repo / "change.txt").exists()
    assert (wt / ".ratchetloop" / "identity.json").is_file()
    assert git("status", "--porcelain", cwd=wt) == "?? change.txt"  # metadata is excluded


def test_orphan_sweep_removes_a_finished_clean_worktree(repo):
    orphan = _worktree_path(repo, "old-task")
    _add_worktree(repo, orphan, "ratchetloop/old-task", "master", False)
    _sweep_orphans(repo, lambda key: key == "old-task")
    assert not orphan.exists()


def test_orphan_sweep_leaves_an_unfinished_task(repo):
    live = _worktree_path(repo, "still-running")
    _add_worktree(repo, live, "ratchetloop/still-running", "master", False)
    _sweep_orphans(repo, lambda key: False)
    assert live.exists()
    _remove_worktree(repo, live)


def test_add_worktree_without_revise_does_not_reset_existing_branch(repo):
    wt = _worktree_path(repo, "keep-sha")
    _add_worktree(repo, wt, "ratchetloop/keep-sha", "master", False)
    (wt / "extra.txt").write_text("kept\n")
    git("add", "-A", cwd=wt)
    git("commit", "-m", "keep me", cwd=wt)
    sha = git("rev-parse", "ratchetloop/keep-sha", cwd=repo)
    _remove_worktree(repo, wt)
    with pytest.raises(GitError):
        _add_worktree(repo, _worktree_path(repo, "keep-sha-2"),
                      "ratchetloop/keep-sha", "master", False)
    assert git("rev-parse", "ratchetloop/keep-sha", cwd=repo) == sha


def test_revise_from_reuses_the_existing_branch(repo):
    wt = _worktree_path(repo, "again")
    _add_worktree(repo, wt, "ratchetloop/again", "master", False)
    (wt / "a.txt").write_text("a\n")
    git("add", "a.txt", cwd=wt)
    git("commit", "-q", "-m", "round 1", cwd=wt)
    _remove_worktree(repo, wt)
    _add_worktree(repo, wt, "ratchetloop/again", "master", True)
    assert (wt / "a.txt").read_text() == "a\n"
