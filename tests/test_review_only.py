"""`ratchetloop review --branch B [--base SHA]` (CONTRACT.md §1) with fake workers: one round on an
existing branch after its base moved, the checks first, independence from the branch's coders, and a
record kept apart from the task's own runs."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ratchetloop.cli import main
from ratchetloop.resume import resumable_result
from runkit import (
    CALC, MUL, WRONG_MUL, done, install, make_repo, result_of, runs_root, verdict, write, write_task,
)


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path)


def _git(repo, *args) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          check=True).stdout.strip()


def _human_review(repo, tmp_path, monkeypatch, code=MUL) -> Path:
    """A task whose one round asked for changes, then a master that moved on."""
    task = write_task(tmp_path, repo, max_review_rounds=1)
    install(monkeypatch, [(write("calc.py", code), done())],
            [(None, verdict("REQUEST_CHANGES", "explain mul"))])
    assert main(["run", str(task)]) == 3
    (repo / "README").write_text("moved on\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-q", "-m", "master moves on")
    return task


def test_a_branch_is_reviewed_again_against_its_base_after_master_moved(repo, tmp_path,
                                                                        monkeypatch, capsys):
    task = _human_review(repo, tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "ratchetloop/add-mul")
    capsys.readouterr()
    fake = install(monkeypatch, [], [(None, verdict("APPROVE", "mul is fine"))])
    assert main(["review", str(task), "--branch", "ratchetloop/add-mul"]) == 0
    res = result_of("add-mul.review")
    assert (res["disposition"], res["head_sha"], res["branch"]) == ("ACCEPT", head,
                                                                    "ratchetloop/add-mul")
    assert res["kind"] == "review"  # spend, not a task, in stats
    brief = fake.calls[0]["brief"]
    assert "+def mul" in brief and "moved on" not in brief  # base..HEAD, not master's new commit
    assert fake.calls[0]["provider"] == "codex"  # grok coded the branch
    assert resumable_result(runs_root() / "add-mul")["disposition"] == "HUMAN_REVIEW"
    assert _git(repo, "rev-parse", "ratchetloop/add-mul") == head and res["worktree"] is None
    assert json.loads(capsys.readouterr().out)["disposition"] == "ACCEPT"


def test_red_checks_at_the_branch_head_mean_no_review(repo, tmp_path, monkeypatch):
    # A run never commits red work, so the branch is made by hand, as a person might push one.
    task = write_task(tmp_path, repo)
    _git(repo, "checkout", "-q", "-b", "ratchetloop/add-mul")
    (repo / "calc.py").write_text(WRONG_MUL)
    _git(repo, "commit", "-q", "-am", "a wrong mul")
    _git(repo, "checkout", "-q", "master")
    fake = install(monkeypatch, [], [])
    assert main(["review", str(task), "--branch", "ratchetloop/add-mul"]) == 4
    assert result_of("add-mul.review")["reason"] == "checks_failed" and fake.calls == []


@pytest.mark.parametrize("args,message", [
    (["--reviewer", "grok"], "another family"),          # grok coded the branch (D6)
    (["--base", "ratchetloop/add-mul"], None),           # a base equal to the head is allowed
])
def test_review_refuses_a_reviewer_of_a_coders_family(repo, tmp_path, monkeypatch, capsys, args,
                                                      message):
    task = _human_review(repo, tmp_path, monkeypatch)
    install(monkeypatch, [], [(None, verdict("APPROVE"))])
    code = main(["review", str(task), "--branch", "ratchetloop/add-mul", *args])
    if message:
        assert code == 2 and message in capsys.readouterr().err
    else:
        assert code == 0


def test_review_refuses_a_missing_branch_or_a_base_off_the_branch(repo, tmp_path, monkeypatch,
                                                                  capsys):
    task = _human_review(repo, tmp_path, monkeypatch)
    install(monkeypatch, [], [])
    assert main(["review", str(task), "--branch", "ratchetloop/nope"]) == 2
    assert "not in" in capsys.readouterr().err
    assert main(["review", str(task), "--branch", "ratchetloop/add-mul", "--base", "master"]) == 2
    assert "not an ancestor" in capsys.readouterr().err


def test_a_setup_that_moves_the_head_fails_before_any_review(repo, tmp_path, monkeypatch):
    # Phase 6 review, finding 3: the checks must cover the head the reviewer is shown.
    _human_review(repo, tmp_path, monkeypatch)
    moved = write_task(tmp_path, repo, max_review_rounds=1,
                       setup=["git commit -q --allow-empty -m moved"])
    fake = install(monkeypatch, [], [])
    assert main(["review", str(moved), "--branch", "ratchetloop/add-mul"]) == 4
    res = result_of("add-mul.review")
    assert res["reason"] == "scope_violation" and "moved HEAD" in res["detail"]
    assert fake.calls == []


def test_a_reviewer_that_writes_leaves_the_tree_as_evidence(repo, tmp_path, monkeypatch):
    task = _human_review(repo, tmp_path, monkeypatch)
    install(monkeypatch, [], [(write("notes.txt", "looks fine\n"), verdict("APPROVE"))])
    assert main(["review", str(task), "--branch", "ratchetloop/add-mul"]) == 4
    res = result_of("add-mul.review")
    assert res["reason"] == "reviewer_wrote" and (Path(res["worktree"]) / "notes.txt").exists()
