"""`ratchetloop build` (CONTRACT.md §9) with fake workers: a plan, then phases on chained branches
with the idea's one branch following them; stopping at a stage and picking up there; the plan gate;
the idea's budget."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from ratchetloop.cli import main
from runkit import MUL, done, install, make_repo, outcome, primary_state, runs_root, verdict, write

SUB = MUL + "\n\ndef sub(a, b):\n    return a - b\n"
PLAN = """# Plan

Goal: a calc module that multiplies and subtracts.

```yaml
phases:
  - key: mul
    objective: "Add mul(a, b) to calc.py."
    acceptance_criteria: ["calc.mul(3, 4) == 12"]
    checks: ["python3 -c 'import calc; assert calc.mul(3, 4) == 12'"]
    allowed_paths: ["calc.py"]
  - key: sub
    objective: "Add sub(a, b) to calc.py."
    acceptance_criteria: ["calc.sub(5, 3) == 2"]
    checks: ["python3 -c 'import calc; assert calc.sub(5, 3) == 2'"]
    allowed_paths: ["calc.py"]
```
"""


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path)


def _idea(tmp_path: Path, repo: Path, **meta) -> Path:
    data = {"idea_key": "calc", "repo": str(repo), "checks": ["python3 -c 'import calc'"], **meta}
    path = tmp_path / "idea.md"
    path.write_text("---\n" + yaml.safe_dump(data) + "---\nA calc module with mul and sub.\n")
    return path


def _planner(plan: str = PLAN, key: str = "calc"):
    return lambda tree: (write("docs/DESIGN.md", "# Design\n\nA calc module.\n")(tree),
                         write(f"docs/PLAN-{key}.md", plan)(tree))


def _idea_result() -> dict:
    return json.loads((sorted((runs_root() / "calc").iterdir())[-1] / "result.json").read_text())


def _git(repo, *args) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          check=True).stdout.strip()


def test_an_idea_is_planned_then_built_phase_by_phase(repo, tmp_path, monkeypatch):
    before = primary_state(repo)
    fake = install(monkeypatch, [(_planner(), done(cost_usd=0.01)),
                                 (write("calc.py", MUL), done(cost_usd=0.01)),
                                 (write("calc.py", SUB), done(cost_usd=0.01))],
                   [(None, verdict("APPROVE")), (None, verdict("APPROVE")),
                    (None, verdict("APPROVE"))])
    assert main(["build", str(_idea(tmp_path, repo))]) == 0
    res = _idea_result()
    assert (res["disposition"], res["reason"]) == ("ACCEPT", "approved")
    assert [s["task_key"] for s in res["stages"]] == ["calc--plan", "calc--mul", "calc--sub"]
    assert res["velocity"]["phases_planned"] == 2 and res["velocity"]["phases_accepted"] == 2
    assert res["repo"] == str(repo.resolve())  # `stats --repo` finds ideas by it
    assert res["usage"]["cost_usd"] == pytest.approx(0.03)
    # One branch to merge, holding every stage's commits in order (D28).
    assert res["head_sha"] == _git(repo, "rev-parse", "ratchetloop/calc--sub")
    assert len(_git(repo, "rev-list", "master..ratchetloop/calc").split()) == 3
    assert "Build phase `mul`" in fake.calls[2]["brief"]
    sub_review = fake.calls[5]["brief"]
    assert "+def sub" in sub_review and "+def mul" not in sub_review  # its own commits only
    assert primary_state(repo) == before


def test_a_plan_the_pipeline_cannot_run_fails_its_checks_not_its_review(repo, tmp_path,
                                                                         monkeypatch):
    bad = PLAN.replace("key: sub", "key: mul")
    fake = install(monkeypatch, [(_planner(bad), done()), (_planner(bad), done())], [])
    assert main(["build", str(_idea(tmp_path, repo))]) == 4
    res = _idea_result()
    assert (res["disposition"], res["reason"], res["stage"]) == (
        "FAILED", "checks_failed", "calc--plan")
    assert "used twice" in res["detail"]
    assert fake.roles() == ["coder", "coder"]  # the one fix launch (D14); never a review


def test_a_stage_short_of_accept_stops_the_idea_and_continue_resumes_there(repo, tmp_path,
                                                                           monkeypatch):
    idea = _idea(tmp_path, repo)
    install(monkeypatch, [(_planner(), done()), (write("calc.py", MUL), done()),
                          (None, outcome("blocked", "sub of what?"))],
            [(None, verdict("APPROVE")), (None, verdict("APPROVE"))])
    assert main(["build", str(idea)]) == 3
    res = _idea_result()
    assert (res["disposition"], res["reason"], res["stage"]) == (
        "HUMAN_REVIEW", "coder_blocked", "calc--sub")
    assert res["head_sha"] == _git(repo, "rev-parse", "ratchetloop/calc--mul")
    assert main(["build", str(idea)]) == 2  # built before: only --continue picks it up
    fake = install(monkeypatch, [(write("calc.py", SUB), done())], [(None, verdict("APPROVE"))])
    assert main(["build", str(idea), "--continue"]) == 0
    res = _idea_result()
    assert [s["skipped"] for s in res["stages"]] == [True, True, False]
    assert res["disposition"] == "ACCEPT" and fake.roles() == ["coder", "reviewer"]
    assert res["head_sha"] == _git(repo, "rev-parse", "ratchetloop/calc--sub")


def test_the_plan_gate_pauses_the_idea_until_approved(repo, tmp_path, monkeypatch):
    idea = _idea(tmp_path, repo)
    install(monkeypatch, [(_planner(), done())], [(None, verdict("APPROVE"))])
    assert main(["build", str(idea), "--gate", "plan"]) == 6
    assert (_idea_result()["disposition"], _idea_result()["stage"]) == ("PAUSED", "calc--plan")
    # A gate once asked for holds until approved, with or without the flag (Phase 5 review, 3).
    assert main(["build", str(idea), "--continue"]) == 6
    assert main(["approve", "calc"]) == 0
    install(monkeypatch, [(write("calc.py", MUL), done()), (write("calc.py", SUB), done())],
            [(None, verdict("APPROVE")), (None, verdict("APPROVE"))])
    assert main(["build", str(idea), "--continue"]) == 0
    assert main(["approve", "calc"]) == 2  # nothing is paused now


def test_the_idea_budget_stops_it_at_a_stage_boundary(repo, tmp_path, monkeypatch):
    install(monkeypatch, [(_planner(), done(cost_usd=0.04))],
            [(None, verdict("APPROVE", cost_usd=0.02))])
    assert main(["build", str(_idea(tmp_path, repo, cost_budget_usd=0.05))]) == 4
    res = _idea_result()
    assert (res["disposition"], res["reason"], res["stage"]) == ("FAILED", "budget_cost", "calc--mul")
    assert res["usage"]["cost_usd"] == pytest.approx(0.06)


def test_build_detach_is_refused_in_the_foreground(repo, tmp_path, monkeypatch, capsys):
    # Phase 6 review, finding 4: only the idea file was checked before detaching, so "built
    # before" failed in a detached log while the caller exited 0.
    idea = _idea(tmp_path, repo)
    install(monkeypatch, [(_planner(), done()), (write("calc.py", MUL), done()),
                          (write("calc.py", SUB), done())], [(None, verdict("APPROVE"))] * 3)
    assert main(["build", str(idea)]) == 0
    capsys.readouterr()
    assert main(["build", str(idea), "--detach"]) == 2
    assert "was built before" in capsys.readouterr().err
    detached = runs_root() / ".detached"
    assert not detached.exists() or not any(detached.iterdir())  # no child was started


def test_a_zero_budget_starts_no_stage(repo, tmp_path, monkeypatch):
    # Phase 5 review, finding 4: nothing left was not a stop, so a stage could still spend.
    fake = install(monkeypatch, [], [])
    assert main(["build", str(_idea(tmp_path, repo, cost_budget_usd=0))]) == 4
    assert (_idea_result()["reason"], _idea_result()["stage"]) == ("budget_cost", "calc--plan")
    assert fake.calls == []


def test_a_signal_that_lands_as_a_stage_closes_stops_the_idea(repo, tmp_path, monkeypatch):
    # Phase 5 review, finding 1: recorded by the stage, not raised, then cleared by the next stage.
    import ratchetloop.build as build_mod
    import ratchetloop.signals as signals
    real = build_mod.run_task

    def run_then_signal(*args, **kw):
        result = real(*args, **kw)
        monkeypatch.setattr(signals, "_killed_why", "SIGTERM")  # restored after the test
        return result
    monkeypatch.setattr(build_mod, "run_task", run_then_signal)
    fake = install(monkeypatch, [(_planner(), done())], [(None, verdict("APPROVE"))])
    assert main(["build", str(_idea(tmp_path, repo))]) == 5
    res = _idea_result()
    assert (res["disposition"], res["reason"], res["stage"]) == ("STOPPED", "killed", "calc--plan")
    assert fake.roles() == ["coder", "reviewer"]  # no phase started
    assert res["head_sha"] == _git(repo, "rev-parse", "ratchetloop/calc--plan")  # still advanced


def test_unreviewed_commits_on_a_stage_branch_never_reach_the_idea_branch(repo, tmp_path,
                                                                          monkeypatch):
    # Phase 5 review, answer b: a skipped code stage carried later commits onto the idea branch.
    idea = _idea(tmp_path, repo)
    install(monkeypatch, [(_planner(), done()), (write("calc.py", MUL), done()),
                          (write("calc.py", SUB), done())],
            [(None, verdict("APPROVE")), (None, verdict("APPROVE")), (None, verdict("APPROVE"))])
    assert main(["build", str(idea)]) == 0
    merged = _git(repo, "rev-parse", "ratchetloop/calc")
    sneaked = _git(repo, "commit-tree", "ratchetloop/calc--mul^{tree}", "-p",
                   "ratchetloop/calc--mul", "-m", "not reviewed")
    _git(repo, "update-ref", "refs/heads/ratchetloop/calc--mul", sneaked)
    install(monkeypatch, [], [])
    assert main(["build", str(idea), "--continue"]) == 4
    res = _idea_result()
    assert (res["reason"], res["stage"]) == ("refused", "calc--mul")
    assert _git(repo, "rev-parse", "ratchetloop/calc") == merged


def test_two_ideas_keep_their_own_plans(repo, tmp_path, monkeypatch):
    """The bug this file layout exists to prevent.

    With one shared docs/PLAN.md the second idea's planner had to replace the
    file — its own check demanded exactly one fenced yaml block — and the first
    idea's plan went with it.
    """
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    install(monkeypatch, [(_planner(), done()), (write("calc.py", MUL), done()),
                          (write("calc.py", SUB), done())],
            [(None, verdict("APPROVE"))] * 3)
    assert main(["build", str(_idea(tmp_path / "a", repo))]) == 0
    # The operator merges each idea before starting the next; that is when a
    # shared plan file lost the previous idea's section.
    subprocess.run(["git", "merge", "--no-ff", "-m", "merge calc",
                    "ratchetloop/calc--sub"], cwd=repo, check=True,
                   capture_output=True)

    install(monkeypatch, [(_planner(key="calc2"), done()), (write("calc.py", MUL), done()),
                          (write("calc.py", SUB), done())],
            [(None, verdict("APPROVE"))] * 3)
    assert main(["build", str(_idea(tmp_path / "b", repo, idea_key="calc2"))]) == 0

    listed = subprocess.run(["git", "ls-tree", "--name-only",
                             "ratchetloop/calc2--sub:docs"], cwd=repo,
                            capture_output=True, text=True, check=True).stdout.split()
    assert "PLAN-calc.md" in listed, listed
    assert "PLAN-calc2.md" in listed, listed
