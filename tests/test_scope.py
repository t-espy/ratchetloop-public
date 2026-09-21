"""The scope check catches a worker that moved HEAD, touched the repository's git config or hooks,
or wrote outside allowed_paths; setup outputs are not the worker's; a stranger's worktree fails."""
import subprocess
from pathlib import Path

import pytest

from ratchetloop.git_guard import common_dir_guard
from ratchetloop.gitops import git
from ratchetloop.preserve import PRESERVED_NAME
from ratchetloop.scope import check, git_state_changed, snapshot, violation
from ratchetloop.worktree import _add_worktree, _maybe_commit, _worktree_path, stage_paths


@pytest.fixture
def tree(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    wt = _worktree_path(repo, "scoped")
    _add_worktree(repo, wt, "ratchetloop/scoped", "master", False)
    return repo, wt


def test_in_scope_change_is_clean(tree):
    _, wt = tree
    before = snapshot(wt)
    (wt / "src").mkdir()
    (wt / "src" / "a.py").write_text("x = 1\n")
    scope = check(wt, before, ["src/"])
    assert scope == {"changed": ["src/a.py"], "outside_allowed": [], "head_moved": False,
                     "git_dir_touched": False}
    assert violation(scope) is None


def test_write_outside_allowed_paths_is_a_violation(tree):
    _, wt = tree
    before = snapshot(wt)
    (wt / "stray.txt").write_text("x\n")
    scope = check(wt, before, ["src/"])
    assert scope["outside_allowed"] == ["stray.txt"]
    assert "stray.txt" in violation(scope)
    assert check(wt, before)["outside_allowed"] == []  # no allowed_paths, no strays


def test_a_worker_commit_moves_head(tree):
    _, wt = tree
    before = snapshot(wt)
    (wt / "a.txt").write_text("a\n")
    git("add", "a.txt", cwd=wt)
    git("commit", "-q", "-m", "sneaky", cwd=wt)
    scope = check(wt, before)
    assert scope["head_moved"] and "HEAD" in violation(scope)


@pytest.mark.parametrize("tamper", ["hook", "config"])
def test_git_hooks_or_config_changes_are_caught(tree, tamper):
    repo, wt = tree
    before = snapshot(wt)
    if tamper == "hook":
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\necho planted\n")
        hook.chmod(0o755)
    else:
        git("config", "core.hooksPath", "/tmp/elsewhere", cwd=wt)
    scope = check(wt, before)
    assert scope["git_dir_touched"] and "config, hooks" in violation(scope)


def test_a_branch_switch_at_the_same_commit_is_caught(tree):
    # Phase 2 review finding 2(i): the pipeline's commit would land on the wrong branch.
    _, wt = tree
    before = snapshot(wt)
    git("switch", "-q", "-c", "elsewhere", cwd=wt)
    scope = check(wt, before)
    assert scope["head_moved"] and "HEAD" in violation(scope)


def test_an_exclude_entry_hiding_files_is_caught(tree):
    # Phase 2 review finding 2(ii): an excluded path vanishes from `git status`.
    repo, wt = tree
    before = snapshot(wt)
    with open(repo / ".git" / "info" / "exclude", "a") as fh:
        fh.write("secret-change.txt\n")
    (wt / "secret-change.txt").write_text("hidden\n")
    scope = check(wt, before, ["src/"])
    assert scope["git_dir_touched"] and "exclude" in violation(scope)


def test_a_hook_planted_after_the_check_is_caught_before_commit(tree):
    # Phase 2 review finding 3: config and hooks are shared with sibling runs.
    repo, wt = tree
    before = snapshot(wt)
    (wt / "a.txt").write_text("a\n")
    assert violation(check(wt, before, ["a.txt"])) is None
    assert not git_state_changed(wt, before)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho late\n")
    hook.chmod(0o755)
    assert git_state_changed(wt, before)


def test_a_staged_rename_counts_both_sides_and_commits_the_deletion(tree):
    _, wt = tree
    before = snapshot(wt)
    git("mv", "README.md", "moved.md", cwd=wt)
    scope = check(wt, before)
    assert scope["changed"] == ["README.md", "moved.md"]
    stage_paths(wt, scope["changed"])
    assert _maybe_commit(wt, "rename")
    assert git("show", "--name-status", "--format=", "--no-renames", "HEAD", cwd=wt).splitlines() == [
        "D\tREADME.md", "A\tmoved.md"]


def test_setup_outputs_are_not_the_workers(tree):
    _, wt = tree
    (wt / "venv").symlink_to("/usr")  # a setup command's leftover
    before = snapshot(wt)
    (wt / "a.txt").write_text("a\n")
    scope = check(wt, before, ["a.txt"])
    assert scope["changed"] == ["a.txt"] and scope["outside_allowed"] == []


def test_common_dir_guard_fails_a_strangers_tree_and_preserves_it(tree, tmp_path):
    repo, wt = tree
    assert common_dir_guard(wt, repo, "scoped") is None
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=other, check=True)
    note = common_dir_guard(wt, other, "scoped")
    assert note and "git-common-dir mismatch" in note
    assert (wt / ".ratchetloop" / PRESERVED_NAME).is_file()
