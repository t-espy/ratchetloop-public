"""The pinned runtime's scripts (deploy/, the pin/promote design), run through `sh` against a
temporary opt root. pin.sh's refusals only: a real pin builds a venv and installs from the network,
which the Phase 7 smoke does; promote, rollback and the launcher run against pins made by hand."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"


def _sh(script: str, *args: str, opt: Path, cwd: Path | None = None,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "RATCHETLOOP_OPT": str(opt), **(env or {})}
    return subprocess.run(["sh", str(DEPLOY / script), *args], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=60)


def _head(repo: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
                          check=True).stdout.strip()


def _pin(opt: Path, name: str, *, verified: bool = True, runs: bool = True) -> Path:
    """A pin made by hand: a directory whose ratchetloop answers --help (or not)."""
    binary = opt / name / ".venv" / "bin" / "ratchetloop"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n" + (
        'echo "safepath=$PYTHONSAFEPATH path=${PYTHONPATH:-} args=$*"\n' if runs else "exit 1\n"))
    binary.chmod(0o755)
    if verified:
        (opt / name / ".verified").write_text(name + "\n")
    return opt / name


@pytest.fixture
def repo(tmp_path) -> Path:
    repo = tmp_path / "src-repo"
    repo.mkdir()
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "one"]):
        subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                       cwd=repo, check=True)
    return repo


@pytest.mark.parametrize("args,message", [
    ([], "usage"), (["--help"], "usage"), (["nope"], "not a commit"),
])
def test_pin_refuses_a_flag_or_a_non_commit(tmp_path, repo, args, message):
    r = _sh("pin.sh", *args, opt=tmp_path / "opt", cwd=repo)
    assert r.returncode == 2 and message in r.stderr


def test_pin_refuses_extra_arguments_and_never_deletes_an_existing_pin(tmp_path, repo):
    # Phase 7 review, findings 1 and 5: a re-pin deleted a good pin before rebuilding it.
    opt = tmp_path / "opt"
    assert _sh("pin.sh", "HEAD", "--foo", opt=opt, cwd=repo).returncode == 2
    _pin(opt, _head(repo))  # a good pin, not current
    r = _sh("pin.sh", "HEAD", opt=opt, cwd=repo)
    assert r.returncode == 2 and "never deletes a pin" in r.stderr
    assert (opt / _head(repo) / ".verified").exists()


def test_a_pin_that_cannot_build_its_venv_leaves_nothing_behind(tmp_path, repo):
    # Phase 7 review, finding 3: the failure path, with no network: no interpreter for the venv.
    opt = tmp_path / "opt"
    r = _sh("pin.sh", "HEAD", opt=opt, cwd=repo, env={"RATCHETLOOP_PYTHON": "/bin/false"})
    assert r.returncode != 0
    assert list(opt.iterdir()) == []  # no pin, no staging directory, so nothing to promote


def test_pin_refuses_to_rebuild_the_live_current(tmp_path, repo):
    opt = tmp_path / "opt"
    sha = _head(repo)
    _pin(opt, sha)
    (opt / "current").symlink_to(sha)
    r = _sh("pin.sh", "HEAD", opt=opt, cwd=repo)
    assert r.returncode == 2 and "re-pin the live current" in r.stderr
    assert (opt / sha / ".verified").exists()  # untouched


def test_promote_switches_current_and_logs_who_approved(tmp_path):
    opt = tmp_path / "opt"
    _pin(opt, "aaaa1111")
    _pin(opt, "bbbb2222")
    assert _sh("promote.sh", "aaaa", "--approved-by", "operator", opt=opt).returncode == 0
    r = _sh("promote.sh", "bbbb2222", "--approved-by", "operator", opt=opt)
    assert r.returncode == 0 and os.readlink(opt / "current") == "bbbb2222"
    log = (opt / "promotions.log").read_text().splitlines()
    assert "bbbb2222 promoted, approved by operator (was aaaa1111)" in log[-1]
    assert "already current" in _sh("promote.sh", "bbbb", "--approved-by", "x", opt=opt).stdout


@pytest.mark.parametrize("make,args,message", [
    (lambda o: _pin(o, "cccc3333", verified=False), ["cccc", "--approved-by", "op"], "not verified"),
    (lambda o: _pin(o, "dddd4444", runs=False), ["dddd", "--approved-by", "op"], "does not run"),
    (lambda o: (_pin(o, "eeee5555"), _pin(o, "eeee6666")), ["eeee", "--approved-by", "op"],
     "2 pins"),
    (lambda o: _pin(o, "ffff7777"), ["ffff"], "usage"),                   # no approver
    (lambda o: _pin(o, "ffff7777"), ["ffff", "--approved-by", ""], "usage"),
])
def test_promote_refuses_what_it_cannot_prove(tmp_path, make, args, message):
    opt = tmp_path / "opt"
    make(opt)
    r = _sh("promote.sh", *args, opt=opt)
    assert r.returncode == 2 and message in r.stderr
    assert not (opt / "current").exists()


def test_rollback_returns_to_an_earlier_verified_pin(tmp_path):
    opt = tmp_path / "opt"
    _pin(opt, "aaaa1111")
    _pin(opt, "bbbb2222")
    _sh("promote.sh", "aaaa", "--approved-by", "op", opt=opt)
    _sh("promote.sh", "bbbb", "--approved-by", "op", opt=opt)
    r = _sh("rollback.sh", "aaaa", opt=opt)
    assert r.returncode == 0 and os.readlink(opt / "current") == "aaaa1111"
    assert "rolled back (was bbbb2222)" in (opt / "promotions.log").read_text()
    assert _sh("rollback.sh", "zzzz", opt=opt).returncode == 2


def test_the_launcher_runs_the_current_pin_off_the_working_directory(tmp_path):
    opt = tmp_path / "opt"
    assert "no pinned runtime" in _sh("ratchetloop", "stats", opt=opt).stderr
    _pin(opt, "aaaa1111")
    _sh("promote.sh", "aaaa", "--approved-by", "op", opt=opt)
    r = _sh("ratchetloop", "status", "some-task", opt=opt, env={"PYTHONPATH": "/a/checkout/src"})
    assert r.returncode == 0  # the caller's PYTHONPATH is dropped (Phase 7 review, finding 2)
    assert r.stdout.strip() == "safepath=1 path= args=status some-task"
