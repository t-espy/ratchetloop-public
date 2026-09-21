"""Phase 2 exit (the end-to-end flow), end to end with no agent: the "worker" is this test
writing files. A change in a worktree is scoped, checked and committed; a worker commit or an
out-of-scope write fails; the primary checkout is unchanged throughout. A worker push is refused by
the containment environment (test_workers.test_worker_env_blocks_git_push_allows_commit)."""
import hashlib
import subprocess
from pathlib import Path

import pytest

from ratchetloop.checks import check_env, checks_cwd, run_checks, run_setup
from ratchetloop.gitops import git
from ratchetloop.scope import check, snapshot, violation
from ratchetloop.worktree import (
    _add_worktree, _maybe_commit, _stage_changes, _worktree_path, repo_lock, stage_paths,
)

CALC = "def add(a, b):\n    return a + b\n"
CHECK = "./py3 -c 'import calc; assert calc.add(2, 3) == 5; assert calc.mul(3, 4) == 12'"


def _primary_state(repo: Path) -> str:
    parts = [git("rev-parse", "HEAD", cwd=repo), git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo),
             git("status", "--porcelain=v1", "-uall", cwd=repo), git("diff", cwd=repo)]
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=r, check=True)
    (r / "calc.py").write_text(CALC)
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=r, check=True)
    (r / "operator-notes.txt").write_text("the operator's own dirt\n")
    return r


def _prepare(repo: Path, key: str):
    wt = _worktree_path(repo, key)
    with repo_lock(repo):
        _add_worktree(repo, wt, f"ratchetloop/{key}", "master", False)
    env = check_env()
    ok, results = run_setup(["ln -s /usr/bin/python3 py3"], wt, env)  # like linking a venv
    assert ok, results
    return wt, env, snapshot(wt)


def test_in_scope_change_is_scoped_checked_and_committed(repo):
    primary = _primary_state(repo)
    wt, env, before = _prepare(repo, "add-mul")
    (wt / "calc.py").write_text(CALC + "\n\ndef mul(a, b):\n    return a * b\n")
    git("add", "py3", cwd=wt)  # the "worker" also stages the setup link; it must not ship
    scope = check(wt, before, ["calc.py"])
    assert violation(scope) is None and scope["changed"] == ["calc.py"]
    ok, results = run_checks([CHECK], checks_cwd(wt, "."), env)
    assert ok, results
    stage_paths(wt, scope["changed"])  # the worker's change, not what the check left behind
    with repo_lock(repo):
        assert _maybe_commit(wt, "ratchetloop(add-mul): implement #1") is True
    assert git("show", "--name-only", "--format=", "HEAD", cwd=wt).splitlines() == ["calc.py"]
    # The setup link was not committed, and the check wrote no bytecode (check_env turns it off).
    assert set(git("status", "--porcelain", cwd=wt).splitlines()) == {"?? py3"}
    assert _primary_state(repo) == primary


def test_workers_and_checks_write_no_python_bytecode(repo):
    # A Phase 5 build committed __pycache__/*.pyc from the coder's own test runs: the fresh
    # repository ignored nothing and bytecode under allowed_paths counted as the worker's change.
    from ratchetloop.worker_proc import worker_env
    assert worker_env(gh_dir=str(repo / ".gh"))["PYTHONDONTWRITEBYTECODE"] == "1"
    assert check_env()["PYTHONDONTWRITEBYTECODE"] == "1"
    assert check_env({"PYTHONDONTWRITEBYTECODE": ""})["PYTHONDONTWRITEBYTECODE"] == ""  # task wins
    wt, env, _ = _prepare(repo, "no-bytecode")
    ok, results = run_checks(["python3 -c 'import calc'"], wt, env)
    assert ok, results
    assert not (wt / "__pycache__").exists()


def test_red_checks_are_red(repo):
    wt, env, _ = _prepare(repo, "no-mul")
    (wt / "calc.py").write_text(CALC + "\n# forgot mul\n")
    ok, results = run_checks([CHECK], wt, env)
    assert not ok and results[0]["exit"] != 0


def test_a_worker_commit_fails_the_run(repo):
    primary = _primary_state(repo)
    wt, _, before = _prepare(repo, "sneaky")
    (wt / "calc.py").write_text(CALC + "# sneaky\n")
    git("commit", "-q", "-am", "worker commit", cwd=wt)
    assert "HEAD" in violation(check(wt, before, ["calc.py"]))
    assert _primary_state(repo) == primary


def test_an_out_of_scope_write_fails_the_run_and_is_never_staged(repo):
    primary = _primary_state(repo)
    wt, _, before = _prepare(repo, "stray")
    (wt / "calc.py").write_text(CALC + "# ok\n")
    (wt / "other.py").write_text("x = 1\n")
    assert "other.py" in violation(check(wt, before, ["calc.py"]))
    assert "other.py" in _stage_changes(wt, ["calc.py"], exclude=before.dirty)
    assert git("diff", "--cached", "--name-only", cwd=wt) == ""
    assert _primary_state(repo) == primary
