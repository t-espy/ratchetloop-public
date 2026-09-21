"""git() error hardening, scoped staging, commit, and scoped self-heal (temp repos, no network).
Carried from predecessor; staging now adds exactly the paths git reports, never `git add -A`."""
import subprocess
from pathlib import Path

import pytest

from ratchetloop import gitops
from ratchetloop.gitops import GitError, _parse_porcelain, git
from ratchetloop.worktree import _finish_worktree, _maybe_commit, _stage_changes, stage_paths


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=r, check=True)
    (r / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=r, check=True)
    return r


def _staged(repo: Path) -> list[str]:
    return git("diff", "--cached", "--name-only", cwd=repo).splitlines()


def test_nonzero_git_raises_giterror_with_details(repo: Path):
    with pytest.raises(GitError) as exc_info:
        git("checkout", "no-such-branch", cwd=repo)
    err = exc_info.value
    assert err.returncode != 0 and err.cwd == repo
    assert "checkout" in " ".join(err.cmd)
    assert err.stderr and str(repo) in str(err) and err.stderr in str(err)


def test_allow_fail_suppresses_raise_and_warns(repo: Path, capsys):
    assert git("checkout", "no-such-branch", cwd=repo, allow_fail=True) == ""
    err = capsys.readouterr().err
    assert "WARNING" in err and "no-such-branch" in err


def test_stage_changes_refuses_out_of_scope_and_stages_nothing(repo: Path):
    (repo / "in_scope.txt").write_text("a\n")
    (repo / "out_of_scope.txt").write_text("b\n")
    reason = _stage_changes(repo, ["in_scope.txt"])
    assert reason is not None and "out_of_scope.txt" in reason
    assert _staged(repo) == []


def test_stage_changes_allowed_paths_clean_stages_expected(repo: Path):
    (repo / "in_scope.txt").write_text("a\n")
    assert _stage_changes(repo, ["in_scope.txt", "never_touched.txt"]) is None
    assert _staged(repo) == ["in_scope.txt"]


def test_stage_changes_allowed_directory(repo: Path):
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("x = 1\n")
    (repo / "src" / "b.py").write_text("y = 2\n")
    assert _stage_changes(repo, ["src"]) is None
    assert _staged(repo) == ["src/a.py", "src/b.py"]


def test_stage_changes_without_allowed_paths_stages_every_reported_path(repo: Path):
    (repo / "new_file.txt").write_text("new\n")
    (repo / "README.md").write_text("edited\n")
    assert _stage_changes(repo, None) is None
    assert _staged(repo) == ["README.md", "new_file.txt"]


def test_stage_changes_records_deletions(repo: Path):
    (repo / "README.md").unlink()
    assert _stage_changes(repo, None) is None
    assert git("diff", "--cached", "--name-status", cwd=repo) == "D\tREADME.md"


def test_stage_changes_skips_setup_outputs_and_metadata(repo: Path):
    (repo / "venv").symlink_to("/usr")  # what a setup command leaves behind
    (repo / ".ratchetloop").mkdir()
    (repo / ".ratchetloop" / "identity.json").write_text("{}\n")
    (repo / "real.txt").write_text("x\n")
    assert _stage_changes(repo, None, exclude={"venv"}) is None
    assert _staged(repo) == ["real.txt"]
    assert _stage_changes(repo, ["real.txt"], exclude={"venv"}) is None  # neither is a stray


def test_stage_paths_stages_only_what_it_is_given(repo: Path):
    (repo / "worker.py").write_text("x = 1\n")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "worker.cpython-312.pyc").write_bytes(b"\0")  # left by a check
    stage_paths(repo, ["worker.py", ".ratchetloop/identity.json"])
    assert _staged(repo) == ["worker.py"]


def test_stage_paths_drops_anything_the_worker_staged(repo: Path):
    # Phase 2 review finding 1: the commit takes the whole index.
    (repo / "worker.py").write_text("x = 1\n")
    (repo / "py3").symlink_to("/usr/bin/python3")  # a setup output the worker staged itself
    git("add", "py3", cwd=repo)
    stage_paths(repo, ["worker.py"])
    assert _staged(repo) == ["worker.py"]


def test_stage_changes_real_add_failure_propagates(monkeypatch, repo: Path):
    (repo / "in_scope.txt").write_text("a\n")
    real_git = gitops.git

    def fake_git(*args, **kwargs):
        if args[:2] == ("add", "--") and "in_scope.txt" in args:
            raise GitError(args, 128, "fatal: unable to write new index file", repo)
        return real_git(*args, **kwargs)

    monkeypatch.setattr(gitops, "git", fake_git)
    with pytest.raises(GitError):
        _stage_changes(repo, ["in_scope.txt"])


def test_maybe_commit_nothing_staged_is_noop(repo: Path):
    head = git("rev-parse", "HEAD", cwd=repo)
    assert _maybe_commit(repo, "should not happen") is False
    assert git("rev-parse", "HEAD", cwd=repo) == head


def test_maybe_commit_advances_head(repo: Path):
    (repo / "in_scope.txt").write_text("a\n")
    git("add", "-A", cwd=repo)
    head = git("rev-parse", "HEAD", cwd=repo)
    assert _maybe_commit(repo, "add in_scope.txt") is True
    assert git("rev-parse", "HEAD", cwd=repo) != head


def test_maybe_commit_hook_rejection_raises_not_swallowed(repo: Path):
    pre_commit = repo / ".git" / "hooks" / "pre-commit"
    pre_commit.write_text("#!/bin/sh\necho 'blocked by policy' >&2\nexit 1\n")
    pre_commit.chmod(0o755)
    (repo / "in_scope.txt").write_text("a\n")
    git("add", "-A", cwd=repo)
    head = git("rev-parse", "HEAD", cwd=repo)
    with pytest.raises(GitError) as exc_info:
        _maybe_commit(repo, "should be rejected")
    assert "blocked by policy" in exc_info.value.stderr
    assert git("rev-parse", "HEAD", cwd=repo) == head


def test_parse_porcelain_keeps_leading_space_status():
    """` M path` (unstaged modify) must not shift the path by one char."""
    assert _parse_porcelain(" M README.md\n?? scratch.log\nM  staged.txt\n") == [
        (" M", "README.md"), ("??", "scratch.log"), ("M ", "staged.txt"),
    ]


def test_git_status_porcelain_preserves_leading_space(repo: Path):
    (repo / "README.md").write_text("uncommitted edit\n")
    status = git("status", "--porcelain", cwd=repo)
    assert [p for _, p in _parse_porcelain(status)] == ["README.md"]


def test_finish_worktree_heals_run_residue_and_restores_tracked(repo: Path, capsys):
    (repo / "scratch.log").write_text("scratch output\n")
    (repo / "README.md").write_text("uncommitted edit\n")
    healed = _finish_worktree(repo, "FAILED")
    assert not (repo / "scratch.log").exists()
    assert (repo / "README.md").read_text() == "hello\n"
    assert {"scratch.log", "README.md"} <= set(healed)
    err = capsys.readouterr().err
    assert "SELF-HEAL: scratch.log" in err and "SELF-HEAL: README.md" in err


def test_finish_worktree_never_touches_start_snapshot_or_metadata(repo: Path):
    (repo / "preexisting.log").write_text("was here at start\n")
    start = git("status", "--porcelain", cwd=repo)
    meta = repo / ".ratchetloop"
    meta.mkdir()
    (meta / "identity.json").write_text("{}\n")
    (repo / "builder_out.txt").write_text("run residue\n")
    healed = _finish_worktree(repo, "FAILED", start_snapshot=start)
    assert (repo / "preexisting.log").read_text() == "was here at start\n"
    assert (meta / "identity.json").read_text() == "{}\n"
    assert not (repo / "builder_out.txt").exists()
    assert healed == ["builder_out.txt"]


def test_finish_worktree_failure_does_not_raise(monkeypatch, repo: Path, capsys):
    (repo / "builder_out.txt").write_text("run residue\n")

    def boom(*args, **kwargs):
        raise RuntimeError("status exploded")

    monkeypatch.setattr("ratchetloop.gitops.git", boom)
    assert _finish_worktree(repo, "FAILED") == []
    assert (repo / "builder_out.txt").exists()
    assert "SELF-HEAL aborted" in capsys.readouterr().err
