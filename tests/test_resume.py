"""`run --continue` (D15), the run lock and reaping (Phase 4), with fake workers in-process. The
kill drills against a real pipeline process are in test_kill.py."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ratchetloop.cli import main
from ratchetloop.lifecycle import hold_task, lock_alive, reap_groups, start_ticks, write_lock
from runkit import (
    CHECK, MUL, done, events_of, install, make_repo, result_of, runs_root, verdict, write,
    write_task,
)


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path)


def _run(task, *args) -> int:
    return main(["run", str(task), *args])


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          check=True).stdout.strip()


def _runs(key: str = "add-mul"):
    return sorted((runs_root() / key).iterdir())


def test_continue_needs_the_branch_and_an_unaccepted_run(repo, tmp_path, monkeypatch, capsys):
    task = write_task(tmp_path, repo)
    install(monkeypatch, [(write("calc.py", MUL), done())], [(None, verdict("APPROVE"))])
    assert _run(task, "--continue") == 2
    assert "nothing to continue" in capsys.readouterr().err
    assert _run(task) == 0
    assert _run(task, "--continue") == 2
    assert "was accepted" in capsys.readouterr().err


def test_continue_after_the_rounds_run_out_feeds_the_findings_to_another_coder(
        repo, tmp_path, monkeypatch, capsys):
    # No bytecode, so no check residue: HUMAN_REVIEW leaves a clean tree, which is removed.
    task = write_task(tmp_path, repo, max_review_rounds=1, env={"PYTHONDONTWRITEBYTECODE": "1"})
    install(monkeypatch, [(write("calc.py", MUL), done())],
            [(None, verdict("REQUEST_CHANGES", "say it handles ints"))])
    assert _run(task) == 3
    # grok coded the branch, so grok may not review it, whoever codes now (D6).
    assert _run(task, "--continue", "--coder", "claude", "--reviewer", "grok") == 2
    assert "every coder" in capsys.readouterr().err
    fake = install(monkeypatch, [(write("calc.py", MUL + "# ints\n"), done())],
                   [(None, verdict("APPROVE"))])
    assert _run(task, "--continue", "--coder", "claude") == 0
    res = result_of()
    assert res["disposition"] == "ACCEPT" and res["coder"]["provider"] == "claude"
    assert fake.calls[1]["provider"] == "codex"
    brief = fake.calls[0]["brief"]
    assert "## Review findings to address\nsay it handles ints" in brief
    assert "ratchetloop(add-mul): round 1" in brief.split("## Previous attempt")[1]
    assert len(_git(repo, "rev-list", f"master..{res['branch']}").split()) == 2
    events = events_of()
    assert events[0]["continued_from"] == _runs()[0].name
    # The removed tree is made again on the branch, and setup runs in it.
    assert [e["reused"] for e in events if e["event"] == "worktree"] == [False]
    assert "skipped" not in next(e for e in events if e["event"] == "setup")


def test_a_stopped_run_is_continued_and_its_uncommitted_change_adopted(repo, tmp_path, monkeypatch):
    def change_then_stop(tree):
        write("calc.py", MUL)(tree)
        (runs_root() / "STOP").touch()
    task = write_task(tmp_path, repo, setup=["echo ran >> setup-count.txt"])
    install(monkeypatch, [(change_then_stop, done())], [])
    assert _run(task) == 5
    first = result_of()
    assert (first["disposition"], first["reason"]) == ("STOPPED", "stop_sentinel")
    assert first["worktree"] and first["head_sha"] == first["base_sha"]  # stopped before checks
    (runs_root() / "STOP").unlink()
    fake = install(monkeypatch, [(None, done())], [(None, verdict("APPROVE"))])
    assert _run(task, "--continue") == 0
    res = result_of()
    assert res["disposition"] == "ACCEPT" and res["worktree"] is None
    assert _git(repo, "show", "--name-only", "--format=", res["head_sha"]) == "calc.py"
    assert "- calc.py" in fake.calls[0]["brief"].split("## Previous attempt")[1]
    events = events_of()
    assert [e["reused"] for e in events if e["event"] == "worktree"] == [True]
    assert next(e for e in events if e["event"] == "setup")["skipped"] == "reused worktree"


def _dead_lock(run_dir: Path) -> None:
    gone = subprocess.Popen(["true"])
    gone.wait()
    (run_dir / "lock").write_text(f"{gone.pid} 1 0\n")


def test_output_of_interrupted_checks_is_not_adopted_as_the_workers(repo, tmp_path, monkeypatch):
    # Phase 4 review, finding 4: a run killed during its checks left output the residue record
    # never saw. A run that ended after its scope check left only the paths that check named.
    def change_then_stop(tree):
        write("calc.py", MUL)(tree)
        (runs_root() / "STOP").touch()
    task = write_task(tmp_path, repo)
    install(monkeypatch, [(change_then_stop, done())], [])
    assert _run(task) == 5
    tree = Path(result_of()["worktree"])
    write("build/out.txt", "left by a check that was killed\n")(tree)
    (runs_root() / "STOP").unlink()
    install(monkeypatch, [(None, done())], [(None, verdict("APPROVE"))])
    assert _run(task, "--continue") == 0
    res = result_of()
    assert _git(repo, "show", "--name-only", "--format=", res["head_sha"]) == "calc.py"
    assert res["worktree"] is None  # the check output went with the tree as the pipeline's


def test_a_finished_run_that_left_its_lock_keeps_its_record(repo, tmp_path, monkeypatch, capsys):
    # Phase 4 review, finding 1: an ACCEPT rewritten as abandoned could be continued.
    task = write_task(tmp_path, repo)
    install(monkeypatch, [(write("calc.py", MUL), done())], [(None, verdict("APPROVE"))])
    assert _run(task) == 0
    _dead_lock(_runs()[0])  # died after writing result.json, before removing its lock
    assert _run(task, "--continue") == 2
    assert "was accepted" in capsys.readouterr().err
    assert result_of()["disposition"] == "ACCEPT" and not (_runs()[0] / "lock").exists()


def test_continue_skips_a_record_that_died_before_its_admit(repo, tmp_path, monkeypatch):
    # Phase 4 review, finding 3: such a record has no base commit and hid the run before it.
    task = write_task(tmp_path, repo, max_review_rounds=1)
    install(monkeypatch, [(write("calc.py", MUL), done())],
            [(None, verdict("REQUEST_CHANGES", "note the ints"))])
    assert _run(task) == 3
    stub = runs_root() / "add-mul" / "29990101T000000Z"
    stub.mkdir()
    shutil.copyfile(task, stub / "task.yaml")
    _dead_lock(stub)
    install(monkeypatch, [(write("calc.py", MUL + "# ints\n"), done())],
            [(None, verdict("APPROVE"))])
    assert _run(task, "--continue") == 0
    assert json.loads((stub / "result.json").read_text())["reason"] == "abandoned"


def test_a_second_start_of_a_held_task_is_refused(repo, tmp_path, monkeypatch, capsys):
    # Phase 4 review, finding 2: the run lock came after admit, so two starts could both pass it.
    hold = hold_task(runs_root(), "add-mul")
    try:
        install(monkeypatch, [], [])
        assert _run(write_task(tmp_path, repo)) == 2
        assert "is running" in capsys.readouterr().err
    finally:
        hold.release()
    assert _run(write_task(tmp_path, repo), "--check") == 0


def test_checks_record_their_process_group_while_they_run(repo, tmp_path, monkeypatch):
    pidfile_seen = 'test -s "$(ls "$RUNS"/add-mul/*/checks.pid)"'
    install(monkeypatch, [(write("calc.py", MUL), done())], [(None, verdict("APPROVE"))])
    task = write_task(tmp_path, repo, checks=[CHECK, pidfile_seen], env={"RUNS": str(runs_root())})
    assert _run(task) == 0
    assert not list(runs_root().glob("add-mul/*/checks.pid"))


def test_a_live_run_of_the_task_is_refused(repo, tmp_path, monkeypatch, capsys):
    run_dir = runs_root() / "add-mul" / "20260101T000000Z"
    run_dir.mkdir(parents=True)
    write_lock(run_dir)  # this very process: alive
    install(monkeypatch, [], [])
    assert _run(write_task(tmp_path, repo)) == 2
    assert "is running" in capsys.readouterr().err
    assert (run_dir / "lock").exists()


def test_a_recycled_pid_is_not_a_live_run(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "lock").write_text(f"{os.getpid()} 1 0\n")  # our pid, another start time
    assert not lock_alive(run_dir)
    write_lock(run_dir)
    assert lock_alive(run_dir)
    gone = subprocess.Popen(["true"])
    gone.wait()
    (run_dir / "lock").write_text(f"{gone.pid} {start_ticks(os.getpid())} 0\n")
    assert not lock_alive(run_dir)


def test_reaping_spares_a_group_that_works_elsewhere(tmp_path):
    tree, elsewhere, run_dir = tmp_path / "tree", tmp_path / "elsewhere", tmp_path / "run"
    for d in (tree, elsewhere, run_dir):
        d.mkdir()
    ours = subprocess.Popen(["sleep", "60"], cwd=tree, start_new_session=True)
    theirs = subprocess.Popen(["sleep", "60"], cwd=elsewhere, start_new_session=True)
    recycled = subprocess.Popen(["sleep", "60"], cwd=tree, start_new_session=True)
    try:
        (run_dir / "coder.1.log.pid").write_text(str(ours.pid))
        (run_dir / "checks.pid").write_text(str(theirs.pid))
        # A launch that ended normally: whoever has its pgid now is not the dead run's.
        (run_dir / "reviewer.1.log.pid").write_text(str(recycled.pid))
        (run_dir / "reviewer.1.log.exit").write_text("{}\n")
        assert reap_groups(run_dir, [tree]) == [ours.pid]
        ours.wait(timeout=5)
        assert theirs.poll() is None and recycled.poll() is None
    finally:
        for proc in (ours, theirs, recycled):
            proc.kill()
            proc.wait()
