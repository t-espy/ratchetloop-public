"""The loop end to end with fake workers: accept, revise, rounds exhausted, the one check fix, reviewer
fallback, doc tasks, what result.json and events.jsonl carry, and admit refusals (exit 2)."""
from __future__ import annotations

import json
import subprocess

import pytest

from ratchetloop.cli import main
from runkit import (
    CHECK, MUL, WRONG_MUL, done, events_of, install, make_repo, outcome, primary_state, result_of,
    runs_root, verdict, write, write_task,
)


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path)


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          check=True).stdout.strip()


def test_accept_commits_only_the_workers_change_and_leaves_the_primary_alone(
        repo, tmp_path, monkeypatch, capsys):
    before = primary_state(repo)
    fake = install(monkeypatch, [(write("calc.py", MUL), done(cost_usd=0.02))],
                   [(None, verdict("APPROVE", cost_usd=0.01))])
    assert main(["run", str(write_task(tmp_path, repo))]) == 0
    res = result_of()
    assert (res["disposition"], res["reason"]) == ("ACCEPT", "approved")
    assert fake.roles() == ["coder", "reviewer"]
    assert (fake.calls[0]["provider"], fake.calls[1]["provider"]) == ("grok", "codex")
    assert _git(repo, "show", "--name-only", "--format=", res["head_sha"]) == "calc.py"
    assert res["checks"] == {"passed": True, "head_sha": res["head_sha"]}
    assert res["worktree"] is None  # removed; the branch keeps the work
    assert primary_state(repo) == before
    assert res["velocity"]["lines_added"] == 4 and res["velocity"]["review_rounds"] == 1
    assert res["usage"]["cost_usd"] == 0.03 and len(res["usage"]["launches"]) == 2
    kinds = [e["event"] for e in events_of()]
    assert kinds[0] == "admit" and kinds[-1] == "disposition"
    assert {"worktree", "setup", "scope", "checks", "commit", "review", "worktree_end"} <= set(kinds)
    assert json.loads(capsys.readouterr().out)["disposition"] == "ACCEPT"


def test_request_changes_then_approve_carries_findings_both_ways(repo, tmp_path, monkeypatch):
    fake = install(monkeypatch, [(write("calc.py", MUL), done()),
                                 (write("calc.py", MUL + "# handles ints\n"), done())],
                   [(None, verdict("REQUEST_CHANGES", "mention ints", {"bug": 0, "suggestion": 1, "nit": 0})),
                    (None, verdict("APPROVE", "fixed"))])
    assert main(["run", str(write_task(tmp_path, repo))]) == 0
    res = result_of()
    assert res["disposition"] == "ACCEPT" and res["velocity"]["review_rounds"] == 2
    coder2, reviewer2 = fake.calls[2], fake.calls[3]
    assert "## Review findings to address\nmention ints" in coder2["brief"]
    assert "Round-1 findings" in reviewer2["brief"] and "mention ints" in reviewer2["brief"]
    assert len(_git(repo, "rev-list", f"master..{res['branch']}").split()) == 2


def test_two_rounds_of_changes_end_in_human_review(repo, tmp_path, monkeypatch):
    install(monkeypatch, [(write("calc.py", MUL), done()), (None, done())],
            [(None, verdict("REQUEST_CHANGES")), (None, verdict("REQUEST_CHANGES"))])
    assert main(["run", str(write_task(tmp_path, repo))]) == 3
    assert (result_of()["disposition"], result_of()["reason"]) == ("HUMAN_REVIEW", "review_rounds_exhausted")


RESIDUE_CHECK = "echo ran >> check-output.txt"  # what a check leaves behind in the tree


def test_red_checks_get_one_fix_launch_without_a_review(repo, tmp_path, monkeypatch):
    # The checks leave a file between the two launches; it must not count as the fix's change.
    fake = install(monkeypatch, [(write("calc.py", WRONG_MUL), done()), (write("calc.py", MUL), done())],
                   [(None, verdict("APPROVE"))])
    assert main(["run", str(write_task(tmp_path, repo, checks=[CHECK, RESIDUE_CHECK]))]) == 0
    assert fake.roles() == ["coder", "coder", "reviewer"]
    assert "These checks failed on your change" in fake.calls[1]["brief"]
    assert [e["passed"] for e in events_of() if e["event"] == "checks"] == [False, True]


def test_a_fix_launch_that_rewrites_check_residue_is_out_of_scope(repo, tmp_path, monkeypatch):
    # Phase 3 review, finding 3: residue was excluded by name, so a rewrite of it went unseen.
    install(monkeypatch, [(write("calc.py", WRONG_MUL), done()),
                          (lambda t: (write("calc.py", MUL)(t),
                                      write("check-output.txt", "forged\n")(t)), done())], [])
    assert main(["run", str(write_task(tmp_path, repo, checks=[CHECK, RESIDUE_CHECK]))]) == 4
    res = result_of()
    assert res["reason"] == "scope_violation" and "check-output.txt" in res["detail"]


def test_an_accepted_tree_with_an_unaccounted_untracked_file_is_kept(repo, tmp_path, monkeypatch):
    # Phase 3 review, finding 4: only the pipeline's own leftovers may go with a forced remove.
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho made-by-hook > hook-output.txt\n")
    hook.chmod(0o755)
    install(monkeypatch, [(write("calc.py", MUL), done())], [(None, verdict("APPROVE"))])
    assert main(["run", str(write_task(tmp_path, repo))]) == 0
    res = result_of()
    assert res["disposition"] == "ACCEPT" and res["worktree"]
    assert (tmp_path / res["worktree"] / "hook-output.txt").read_text() == "made-by-hook\n"


def test_still_red_after_the_fix_fails_and_commits_nothing(repo, tmp_path, monkeypatch):
    install(monkeypatch, [(write("calc.py", WRONG_MUL), done()), (write("calc.py", WRONG_MUL), done())], [])
    assert main(["run", str(write_task(tmp_path, repo))]) == 4
    res = result_of()
    assert (res["disposition"], res["reason"]) == ("FAILED", "checks_failed")
    assert res["head_sha"] == res["base_sha"] and res["worktree"]  # the red work is preserved


def test_reviewer_quota_falls_back_to_another_family(repo, tmp_path, monkeypatch):
    from ratchetloop.reviewers import cooled
    fake = install(monkeypatch, [(write("calc.py", MUL), done())],
                   [(None, outcome("quota")), (None, verdict("APPROVE"))])
    assert main(["run", str(write_task(tmp_path, repo))]) == 0
    assert [c["provider"] for c in fake.calls if c["role"] == "reviewer"] == ["codex", "claude"]
    assert result_of()["reviewer"]["provider"] == "claude"
    assert "codex" in cooled(runs_root())


def test_doc_task_is_checked_for_presence_and_links_then_reviewed(repo, tmp_path, monkeypatch):
    plan = "# Plan\n\nSee [the design](DESIGN.md).\n"
    fake = install(monkeypatch, [(lambda t: (write("docs/DESIGN.md", "# Design\n")(t),
                                             write("docs/PLAN-calc.md", plan)(t)), done())],
                   [(None, verdict("APPROVE"))])
    task = write_task(tmp_path, repo, task_key="plan-div", kind="doc", checks=None,
                      allowed_paths=["docs/"], objective="Plan a div() change.")
    assert main(["run", str(task)]) == 0
    res = result_of("plan-div")
    assert res["kind"] == "doc" and res["velocity"]["tests_added"] is None
    assert fake.calls[1]["brief"].startswith("# Reviewer — one adversarial pass over a plan")
    assert fake.calls[0]["brief"].startswith("# Author")


def test_doc_task_with_a_broken_link_fails_its_checks(repo, tmp_path, monkeypatch):
    broken = write("docs/PLAN.md", "# Plan\n\n[gone](missing.md)\n")
    install(monkeypatch, [(broken, done()), (broken, done())], [])
    task = write_task(tmp_path, repo, task_key="plan-bad", kind="doc", checks=None,
                      allowed_paths=["docs/"])
    assert main(["run", str(task)]) == 4
    assert result_of("plan-bad")["reason"] == "checks_failed"


@pytest.mark.parametrize("args,setup,message", [
    ([], lambda repo, tmp: _git(repo, "branch", "ratchetloop/add-mul"), "already exists"),
    (["--reviewer", "grok"], None, "another family"),
    (["--coder", "copilot"], None, "family is unknown"),
])
def test_refusals_exit_2_without_a_run_directory(repo, tmp_path, monkeypatch, capsys, args, setup, message):
    install(monkeypatch, [], [])
    if setup:
        setup(repo, tmp_path)
    assert main(["run", str(write_task(tmp_path, repo)), *args]) == 2
    assert message in capsys.readouterr().err
    assert not (runs_root() / "add-mul").exists()


def test_a_repository_without_a_committer_name_is_refused(repo, tmp_path, monkeypatch, capsys):
    # Admit checked user.email only; a missing name surfaced as a crash at the first commit, after
    # a paid coder launch (Phase 3 review, finding 5). Global and system config would supply one.
    install(monkeypatch, [], [])
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    _git(repo, "config", "--unset", "user.name")
    assert main(["run", str(write_task(tmp_path, repo))]) == 2
    assert "user.name" in capsys.readouterr().err
    assert not (runs_root() / "add-mul").exists()


def test_missing_cli_and_bad_task_are_refused(repo, tmp_path, monkeypatch, capsys):
    install(monkeypatch, [], [], clis=False)
    monkeypatch.setattr("ratchetloop.run.shutil.which", lambda name: None)
    assert main(["run", str(write_task(tmp_path, repo))]) == 2
    assert "not on PATH" in capsys.readouterr().err
    bad = tmp_path / "bad.yaml"
    bad.write_text("task_key: x\nfoundational: true\n")
    assert main(["run", str(bad)]) == 2
    assert "unknown keys" in capsys.readouterr().err


def test_check_validates_without_running(repo, tmp_path, monkeypatch, capsys):
    fake = install(monkeypatch, [], [])
    assert main(["run", str(write_task(tmp_path, repo)), "--check"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "task_key": "add-mul",
                                                   "coder": "grok", "reviewer": "codex"}
    assert fake.calls == [] and not (runs_root() / "add-mul").exists()


def test_a_flag_is_never_taken_for_a_task_path(capsys):
    # `pin.sh --help` created a directory named `--help` (FAILURE_MODES.md §9).
    with pytest.raises(SystemExit) as exc:
        main(["run", "--", "-x.yaml"])
    assert exc.value.code == 2
    assert "looks like a flag" in capsys.readouterr().err
