"""Idea files and plan validation (CONTRACT.md §9.1–§9.2, D29): what the builder and the plan stage's
check refuse, with the same code."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ratchetloop import idea as idea_mod
from ratchetloop.idea import IdeaError, load_idea, validate_plan
from runkit import make_repo

PHASE = ("  - key: {key}\n    objective: \"Add {key}.\"\n    acceptance_criteria: [\"{key} works\"]\n"
         "    checks: [\"{check}\"]\n")


def _plan(*keys: str, check: str = "python3 -c 'import calc'", blocks: int = 1) -> str:
    body = "phases:\n" + "".join(PHASE.format(key=k, check=check) for k in keys)
    return "# Plan\n\n" + "\n".join(f"```yaml\n{body}```\n" for _ in range(blocks))


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path)


def _idea_file(tmp_path: Path, repo: Path, text: str = "A calc module.", **meta) -> Path:
    data = {"idea_key": "calc", "repo": str(repo), **meta}
    path = tmp_path / "idea.md"
    path.write_text("---\n" + yaml.safe_dump(data) + "---\n" + text + "\n")
    return path


def test_an_idea_file_loads_with_its_defaults(tmp_path, repo):
    idea = load_idea(_idea_file(tmp_path, repo, checks=["python3 -m pytest -q"]))
    assert (idea.idea_key, idea.repo, idea.max_phases) == ("calc", repo.resolve(), 6)
    assert idea.checks == ("python3 -m pytest -q",) and idea.text == "A calc module."


@pytest.mark.parametrize("meta,text,message", [
    ({"colour": "red"}, "x", "unknown keys"),
    ({"idea_key": "calc--cli"}, "x", "without `--`"),
    ({}, "", "needs a paragraph"),
    ({"max_phases": 0}, "x", "max_phases"),
    ({"checks": ["grep -q mul src/calc.py"]}, "x", "not grep source"),
    ({"checks": "pytest -q"}, "x", "`checks` must be a list"),  # Phase 5 review, finding 2
    ({"setup": True}, "x", "`setup` must be a list"),
])
def test_an_idea_that_cannot_be_built_is_refused(tmp_path, repo, meta, text, message):
    with pytest.raises(IdeaError, match=message):
        load_idea(_idea_file(tmp_path, repo, text=text, **meta))


def test_front_matter_is_required(tmp_path, repo):
    path = tmp_path / "idea.md"
    path.write_text("Just a paragraph.\n")
    with pytest.raises(IdeaError, match="front matter"):
        load_idea(path)


@pytest.mark.parametrize("plan,message", [
    (_plan("mul", blocks=2), "exactly one fenced yaml block; it holds 2"),
    ("# Plan\n\nno block\n", "it holds 0"),
    (_plan("mul", "mul"), "used twice"),
    (_plan("plan"), "not be `plan`"),
    (_plan("a", "b", "c"), "may have 1 to 2"),
    (_plan("mul", check="grep -q mul calc.py"), "phase mul: checks must test behaviour"),
    ("```yaml\nsteps: []\n```\n", "one key, `phases`"),
    # Phase 5 review, finding 2: a bare string became one-character commands.
    ("```yaml\nphases:\n  - key: mul\n    objective: x\n    acceptance_criteria: [y]\n"
     "    checks: pytest\n```\n", "phase mul: `checks` must be a list"),
])
def test_a_plan_the_pipeline_cannot_run_is_refused(tmp_path, repo, plan, message):
    idea = load_idea(_idea_file(tmp_path, repo, max_phases=2))
    with pytest.raises(IdeaError, match=message):
        validate_plan(plan, idea)


def test_a_valid_plan_becomes_phase_tasks_on_chained_branches(tmp_path, repo):
    idea = load_idea(_idea_file(tmp_path, repo, checks=["python3 -c 'import calc'"]))
    phases = validate_plan(_plan("mul", "sub"), idea)
    task = idea_mod.phase_task(idea, phases[1], "ratchetloop/calc--mul", {"wall_budget_s": 60})
    assert task["task_key"] == "calc--sub" and task["base_branch"] == "ratchetloop/calc--mul"
    assert task["checks"][-1] == "python3 -c 'import calc'"  # the idea's checks after the phase's
    assert "Build phase `sub`" in task["objective"] and "docs/DESIGN.md" in task["objective"]


def test_the_plan_check_command_passes_a_runnable_plan_and_fails_the_rest(tmp_path, repo,
                                                                          monkeypatch, capsys):
    idea_path = _idea_file(tmp_path, repo)
    work = tmp_path / "tree"
    (work / "docs").mkdir(parents=True)
    (work / "docs" / "PLAN-calc.md").write_text(_plan("mul", "sub"))
    monkeypatch.chdir(work)
    args = ["check-plan", "docs/PLAN-calc.md", "--idea", str(idea_path)]
    assert idea_mod.main(args) == 1 and "DESIGN.md is missing" in capsys.readouterr().err
    (work / "docs" / "DESIGN.md").write_text("# Design\n")
    assert idea_mod.main(args) == 0 and "2 phases: mul, sub" in capsys.readouterr().out
    (work / "docs" / "PLAN-calc.md").write_text(_plan("mul", "mul"))
    assert idea_mod.main(args) == 1 and "used twice" in capsys.readouterr().err
