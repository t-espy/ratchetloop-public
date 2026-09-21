"""Carrying a stage past exhausted review rounds (CONTRACT.md §9.5, D35), with fake workers: the
idea goes on with the residuals recorded, committed and briefed; the hard stops stay; `--continue`
skips a carried stage; a later review closes what it names."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from ratchetloop.cli import main
from runkit import (
    MUL, WRONG_MUL, done, install, make_repo, outcome, runs_root, verdict, write,
)
from test_build import PLAN, SUB, _idea, _planner

RC = "REQUEST_CHANGES"
BUG = "bug — calc.py:3: mul is right but the docstring promises ints only"
UNFIXED = "fixed: 1 — docstring reworded\nunfixed: 2 — calc.py:3 the promise is still there"


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path)


def _git(repo, *args) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          check=True).stdout.strip()


def _idea_result() -> dict:
    return json.loads((sorted((runs_root() / "calc").iterdir())[-1] / "result.json").read_text())


def _stage_result(key: str) -> dict:
    return json.loads((sorted((runs_root() / key).iterdir())[-1] / "result.json").read_text())


def _exhaust_mul(sub_reviewer=None, *, mul_summaries=(BUG, BUG), blocking=None):
    """Plan, then `mul` reviewed REQUEST_CHANGES twice with green checks, then `sub`."""
    coders = [(_planner(), done()), (write("calc.py", MUL), done()), (None, done()),
              (write("calc.py", SUB), done())]
    reviewers = [(None, verdict("APPROVE")), (None, verdict(RC, mul_summaries[0])),
                 (None, verdict(RC, mul_summaries[1], blocking=blocking))]
    if sub_reviewer is not None:
        reviewers.append((None, sub_reviewer))
    return coders, reviewers


def test_an_exhausted_stage_with_green_checks_is_carried_and_the_idea_goes_on(
        repo, tmp_path, monkeypatch, capsys):
    fake = install(monkeypatch, *_exhaust_mul(verdict("APPROVE")))
    assert main(["build", str(_idea(tmp_path, repo))]) == 0
    out = capsys.readouterr().out
    assert "ACCEPT with 1 residual findings, 1 open" in out
    res = _idea_result()
    assert (res["disposition"], res["reason"]) == ("ACCEPT", "approved")
    mul = res["stages"][1]
    assert (mul["task_key"], mul["disposition"], mul["reason"], mul["carried"]) == (
        "calc--mul", "HUMAN_REVIEW", "review_rounds_exhausted", True)
    assert [r["file"] for r in mul["residuals"]] == ["calc.py"]
    assert res["residuals"] == mul["residuals"] and res["velocity"]["phases_carried"] == 1
    assert res["velocity"]["phases_accepted"] == 1  # `sub`; the stage's own record is unchanged
    assert _stage_result("calc--mul")["disposition"] == "HUMAN_REVIEW"
    # The residuals travel as data in the run and as notes on the branch, then to the idea branch.
    run_dir = sorted((runs_root() / "calc").iterdir())[-1]
    assert json.loads((run_dir / "residuals.json").read_text()) == res["residuals"]
    notes = _git(repo, "show", "ratchetloop/calc:docs/REVIEW_NOTES.md")
    assert "## calc--mul" in notes and "calc.py:3" in notes
    assert _git(repo, "log", "-1", "--format=%s", "ratchetloop/calc--mul") == \
        "ratchetloop(calc--mul): residual review notes"
    assert (runs_root() / "calc--mul" / mul["run_id"] / "carried.json").is_file()
    # The next phase's coder and reviewer both saw the open residual.
    sub_coder, sub_reviewer = fake.calls[6]["brief"], fake.calls[7]["brief"]
    assert "## Residual findings carried from earlier phases" in sub_coder and "calc.py:3" in sub_coder
    assert "## Open residual findings from earlier phases" in sub_reviewer
    assert "not grounds for REQUEST_CHANGES" in sub_reviewer
    # The round-2 brief of `mul` asked for fixed:/unfixed: lines.
    assert "`fixed: <n> — <what>` or `unfixed: <n>" in fake.calls[5]["brief"]


def test_a_later_review_that_names_the_residual_closes_it(repo, tmp_path, monkeypatch, capsys):
    install(monkeypatch, *_exhaust_mul(verdict("APPROVE", "resolved: calc.py:3 docstring fixed")))
    assert main(["build", str(_idea(tmp_path, repo))]) == 0
    assert "ACCEPT with 1 residual findings, 0 open" in capsys.readouterr().out
    res = _idea_result()
    assert res["residuals"][0]["resolved_by"] == "calc--sub"
    run_dir = sorted((runs_root() / "calc").iterdir())[-1]  # the data file is current too
    assert json.loads((run_dir / "residuals.json").read_text())[0]["resolved_by"] == "calc--sub"
    assert main(["status", "calc"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["carried_stages"] == ["calc--mul"] and shown["open_residuals"] == []


def test_notes_of_an_accepted_fix_check_are_carried_as_nits(repo, tmp_path, monkeypatch, capsys):
    # Round 2 approves with a `notes:` line: the stage ACCEPTs and the note is a nit residual.
    install(monkeypatch, [(_planner(), done()), (write("calc.py", MUL), done()), (None, done()),
                          (write("calc.py", SUB), done())],
            [(None, verdict("APPROVE")), (None, verdict(RC, BUG)),
             (None, verdict("APPROVE", "fixed: 1 — docstring reworded\nnotes: calc.py:1 could "
                                       "carry a module docstring")),
             (None, verdict("APPROVE"))])
    assert main(["build", str(_idea(tmp_path, repo))]) == 0
    assert "ACCEPT with 1 residual findings, 1 open" in capsys.readouterr().out
    res = _idea_result()
    assert res["stages"][1]["disposition"] == "ACCEPT" and "carried" not in res["stages"][1]
    assert [(r["tag"], r["file"], r["stage"]) for r in res["residuals"]] == [
        ("nit", "calc.py", "calc--mul")]


def test_an_unfixed_line_in_round_two_is_carried_as_unfixed(repo, tmp_path, monkeypatch):
    install(monkeypatch, *_exhaust_mul(verdict("APPROVE"), mul_summaries=(BUG, UNFIXED)))
    assert main(["build", str(_idea(tmp_path, repo))]) == 0
    r = _idea_result()["residuals"]
    assert [(x["status"], x["file"], x["line"]) for x in r] == [("unfixed", "calc.py", "3")]


def test_stop_keeps_the_old_behaviour_and_other_values_are_refused(repo, tmp_path, monkeypatch,
                                                                    capsys):
    fake = install(monkeypatch, *_exhaust_mul())
    assert main(["build", str(_idea(tmp_path, repo, on_rounds_exhausted="stop"))]) == 3
    res = _idea_result()
    assert (res["disposition"], res["reason"], res["stage"]) == (
        "HUMAN_REVIEW", "review_rounds_exhausted", "calc--mul")
    assert res["residuals"] == [] and "carried" not in res["stages"][1]
    assert fake.roles()[-1] == "reviewer"  # `sub` never started
    assert main(["build", str(_idea(tmp_path, repo, on_rounds_exhausted="ask"))]) == 2
    assert "on_rounds_exhausted" in capsys.readouterr().err


def test_a_blocking_finding_stops_the_idea(repo, tmp_path, monkeypatch):
    install(monkeypatch, *_exhaust_mul(blocking=True))
    assert main(["build", str(_idea(tmp_path, repo))]) == 3
    res = _idea_result()
    assert (res["disposition"], res["stage"]) == ("HUMAN_REVIEW", "calc--mul")
    assert _stage_result("calc--mul")["review_blocking"] is True and res["residuals"] == []


def test_red_checks_and_a_blocked_coder_still_end_the_idea(repo, tmp_path, monkeypatch):
    # Round 2 breaks mul: the fix launch fails too → FAILED checks_failed, nothing to carry.
    install(monkeypatch, [(_planner(), done()), (write("calc.py", MUL), done()),
                          (write("calc.py", WRONG_MUL), done()),
                          (write("calc.py", WRONG_MUL), done())],
            [(None, verdict("APPROVE")), (None, verdict(RC, BUG))])
    assert main(["build", str(_idea(tmp_path, repo))]) == 4
    res = _idea_result()
    assert (res["reason"], res["stage"], res["residuals"]) == ("checks_failed", "calc--mul", [])
    (tmp_path / "second").mkdir()
    repo2 = make_repo(tmp_path / "second")
    install(monkeypatch, [(_planner(), done()), (None, outcome("blocked", "which mul?"))],
            [(None, verdict("APPROVE"))])
    idea2 = tmp_path / "idea2.md"
    idea2.write_text("---\n" + yaml.safe_dump({"idea_key": "calc", "repo": str(repo2)})
                     + "---\nA calc module.\n")
    assert main(["build", str(idea2)]) == 3
    assert _idea_result()["reason"] == "coder_blocked"


def test_continue_skips_a_carried_stage_and_keeps_its_residuals(repo, tmp_path, monkeypatch):
    idea = _idea(tmp_path, repo)
    coders, reviewers = _exhaust_mul()
    coders[-1] = (None, outcome("blocked", "sub of what?"))  # stop at `sub` after the carry
    install(monkeypatch, coders, reviewers)
    assert main(["build", str(idea)]) == 3
    first = _idea_result()
    assert first["stages"][1]["carried"] is True and first["stage"] == "calc--sub"
    assert first["head_sha"] == _git(repo, "rev-parse", "ratchetloop/calc--mul")
    fake = install(monkeypatch, [(write("calc.py", SUB), done())], [(None, verdict("APPROVE"))])
    assert main(["build", str(idea), "--continue"]) == 0
    res = _idea_result()
    assert [(s["skipped"], s.get("carried", False)) for s in res["stages"]] == [
        (True, False), (True, True), (False, False)]
    assert [r["file"] for r in res["residuals"]] == ["calc.py"]
    assert fake.roles() == ["coder", "reviewer"] and "calc.py:3" in fake.calls[0]["brief"]
    assert _git(repo, "show", "ratchetloop/calc:docs/REVIEW_NOTES.md").count("## calc--mul") == 1


def test_a_carried_branch_that_moved_on_is_rerun_not_skipped(repo, tmp_path, monkeypatch):
    # Commits added outside the pipeline after a carry are reviewed by `run --continue`, never
    # skipped onto the idea branch.
    idea = _idea(tmp_path, repo)
    coders, reviewers = _exhaust_mul()
    coders[-1] = (None, outcome("blocked", "sub of what?"))
    install(monkeypatch, coders, reviewers)
    assert main(["build", str(idea)]) == 3
    sneaked = _git(repo, "commit-tree", "ratchetloop/calc--mul^{tree}", "-p",
                   "ratchetloop/calc--mul", "-m", "not reviewed")
    _git(repo, "update-ref", "refs/heads/ratchetloop/calc--mul", sneaked)
    fake = install(monkeypatch, [(None, done()), (write("calc.py", SUB), done())],
                   [(None, verdict("APPROVE")), (None, verdict("APPROVE"))])
    main(["build", str(idea), "--continue"])  # the chain diverged by hand; how it ends is §9.4's
    res = _idea_result()
    assert (res["stages"][1]["skipped"], res["stages"][1].get("carried", False)) == (False, False)
    assert fake.roles()[:2] == ["coder", "reviewer"]
    assert "not reviewed" in fake.calls[0]["brief"]  # the coder saw the branch's commits


THREE = PLAN.replace("```\n", """  - key: div
    objective: "Add div(a, b) to calc.py."
    acceptance_criteria: ["calc.div(6, 3) == 2"]
    checks: ["python3 -c 'import calc; assert calc.div(6, 3) == 2'"]
    allowed_paths: ["calc.py"]
```
""", 1)
DIV = SUB + "\n\ndef div(a, b):\n    return a / b\n"


def test_a_closure_before_a_later_stop_holds_on_continue(repo, tmp_path, monkeypatch):
    # Review finding 1: replaying skipped ACCEPTed stages must replay their closures too.
    idea = _idea(tmp_path, repo)
    coders = [(_planner(THREE), done()), (write("calc.py", MUL), done()), (None, done()),
              (write("calc.py", SUB), done()), (None, outcome("blocked", "div by what?"))]
    reviewers = [(None, verdict("APPROVE")), (None, verdict(RC, BUG)), (None, verdict(RC, BUG)),
                 (None, verdict("APPROVE", "resolved: calc.py:3 docstring fixed"))]
    install(monkeypatch, coders, reviewers)
    assert main(["build", str(idea)]) == 3
    assert _idea_result()["residuals"][0]["resolved_by"] == "calc--sub"
    install(monkeypatch, [(write("calc.py", DIV), done())], [(None, verdict("APPROVE"))])
    assert main(["build", str(idea), "--continue"]) == 0
    res = _idea_result()
    assert [s["skipped"] for s in res["stages"]] == [True, True, True, False]
    assert res["residuals"][0]["resolved_by"] == "calc--sub"
    run_dir = sorted((runs_root() / "calc").iterdir())[-1]
    assert json.loads((run_dir / "residuals.json").read_text())[0]["resolved_by"] == "calc--sub"


def test_the_plan_stage_is_never_carried(repo, tmp_path, monkeypatch):
    # A doc stage asked for changes twice stays HUMAN_REVIEW: only code stages carry.
    plan_bad = PLAN.replace("Goal:", "Goal (vague):")
    install(monkeypatch, [(_planner(plan_bad), done()), (_planner(), done())],
            [(None, verdict(RC, BUG)), (None, verdict(RC, BUG))])
    assert main(["build", str(_idea(tmp_path, repo))]) == 3
    res = _idea_result()
    assert (res["stage"], res["residuals"]) == ("calc--plan", [])
    assert "carried" not in res["stages"][0]
