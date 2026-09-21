"""Idea files and the tasks an idea becomes (CONTRACT.md §9, D28, D29, D31).

An idea is a paragraph under YAML front matter. Its first stage is a `doc` task that writes
docs/DESIGN.md and docs/PLAN.md; PLAN.md's one fenced `yaml` block lists the phases, and each phase
becomes a `code` task. The builder and the plan stage's own check make those tasks with the same
functions here and validate them with the task-file validator, so a plan the pipeline cannot run
fails its checks, never its review (D29). `python -m ratchetloop.idea check-plan` is that check."""
from __future__ import annotations

import argparse
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .task import TASK_KEY, TaskError, validate_task

IDEA_KEYS = frozenset({"idea_key", "repo", "base_branch", "checks", "setup", "max_phases",
                       "cost_budget_usd", "wall_budget_s", "on_rounds_exhausted"})
ON_ROUNDS_EXHAUSTED = ("carry", "stop")  # §9.5: default carry
PHASE_KEYS = frozenset({"key", "objective", "acceptance_criteria", "checks", "allowed_paths"})
PHASE_KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,20}")  # `<idea_key>--<phase key>` fits a task_key
FENCE = re.compile(r"^```ya?ml[ \t]*\n(.*?)^```[ \t]*$", re.M | re.S)
DESIGN_PATH = "docs/DESIGN.md"


def plan_path(idea: "Idea") -> str:
    """Each idea owns its plan file.

    One shared docs/PLAN.md could not work: the plan stage must leave exactly
    one fenced yaml block behind, so a second idea appending its own phases
    would fail that check, and every planner therefore replaced the file and
    wiped the plans before it. A file per idea keeps the one-block rule true
    and makes the clobber impossible. It stays directly under docs/ so that a
    relative link to DESIGN.md, which a planner writes without being asked,
    still resolves.
    """
    return f"docs/PLAN-{idea.idea_key}.md"

PLAN_OBJECTIVE = """\
Design and plan this idea so that it can be built in phases, each by a coder that sees only its own
phase and the two documents you write:

{idea}

Write these two documents and nothing else:

1. `docs/DESIGN.md` — what is built and the rules it follows: the concept in a paragraph; the
   principles every phase must keep; the architecture (modules, data, interfaces) at the level a
   coder needs; explicit non-goals. Other ideas have their own sections in this file: append yours
   and leave theirs untouched.
2. `{plan_path}` — how it is built: a one-sentence goal; constraints; the phases in order, the
   riskiest unknown first and the first phase the smallest slice that runs; for each phase one exit
   criterion that a check can prove; non-goals. It must contain exactly one fenced `yaml` block, the
   plan the pipeline runs:

   ```yaml
   phases:
     - key: bootstrap                    # [a-z0-9-], unique, at most 21 characters, not "plan"
       objective: "..."                  # what this phase builds, for its coder
       acceptance_criteria: ["..."]
       checks: ["..."]                   # commands that prove this phase's exit criterion
       allowed_paths: ["src/", "tests/"] # optional
   ```

   1 to {max_phases} phases. Every phase adds or extends tests and its checks run them, from the
   repository root{setup_note}. After every phase the pipeline also runs: {idea_checks}.
"""

PLAN_CRITERIA = (
    "docs/DESIGN.md and {plan_path} exist and agree with each other and with the idea",
    "each phase can be built from its objective plus the two documents, in one coder run",
    "the phases put the riskiest unknown first, and the first phase is the smallest slice that runs",
    "each phase's checks would prove its exit criterion by running tests, not by reading source",
    "{plan_path} holds exactly one fenced yaml block: the phases",
    "sections other ideas already wrote in docs/DESIGN.md are still there, unchanged",
)

PHASE_OBJECTIVE = """\
Build phase `{key}` of the plan in {plan_path}, and only that phase. Read docs/DESIGN.md and
{plan_path} first: they are the contract every phase keeps. Earlier phases are already on this
branch.

This phase: {objective}"""


class IdeaError(ValueError):
    """The idea or its plan cannot be built as written; the message names the problem."""


@dataclass(frozen=True)
class Idea:
    idea_key: str
    repo: Path
    text: str
    base_branch: str = "master"
    checks: tuple[str, ...] = ()
    setup: tuple[str, ...] = ()
    max_phases: int = 6
    cost_budget_usd: float = 50.0
    wall_budget_s: float = 21600.0
    on_rounds_exhausted: str = "carry"


def _str_list(value: Any, where: str, *, required: bool = True) -> list[str]:
    """A YAML list of non-empty strings. `list("pytest")` would split a bare string into
    one-character commands that the task validator accepts (Phase 5 review, finding 2)."""
    if value is None and not required:
        return []
    if (not isinstance(value, list) or (required and not value)
            or not all(isinstance(s, str) and s.strip() for s in value)):
        raise IdeaError(f"{where} must be a list of non-empty strings")
    return value


def plan_key(idea: Idea) -> str:
    return f"{idea.idea_key}--plan"


def phase_key(idea: Idea, key: str) -> str:
    return f"{idea.idea_key}--{key}"


def stage_branch(task_key: str) -> str:
    return f"ratchetloop/{task_key}"


def _front_matter(raw: str) -> tuple[dict[str, Any], str]:
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        raise IdeaError("an idea file starts with YAML front matter between `---` lines")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise IdeaError("the front matter has no closing `---`")
    try:
        meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as e:
        raise IdeaError(f"invalid YAML front matter: {e}") from e
    if not isinstance(meta, dict):
        raise IdeaError("the front matter must be a mapping")
    return meta, "\n".join(lines[end + 1:]).strip()


def load_idea(path: Path) -> Idea:
    path = Path(path)
    try:
        meta, text = _front_matter(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise IdeaError(f"cannot read idea file {path}: {e}") from e
    unknown = set(meta) - IDEA_KEYS
    if unknown:
        raise IdeaError(f"unknown keys: {sorted(unknown)}")
    key = meta.get("idea_key")
    if not isinstance(key, str) or not TASK_KEY.fullmatch(key) or "--" in key or len(key) > 40:
        raise IdeaError("`idea_key` must match [a-z0-9][a-z0-9-]*, at most 40 characters, "
                        "without `--`")
    if not text:
        raise IdeaError("the idea needs a paragraph after its front matter: what to build")
    phases = meta.get("max_phases", 6)
    if isinstance(phases, bool) or not isinstance(phases, int) or not 1 <= phases <= 12:
        raise IdeaError("`max_phases` must be a whole number from 1 to 12")
    repo = meta.get("repo")
    if not isinstance(repo, str) or not repo.strip():
        raise IdeaError("`repo` is required")
    checks = _str_list(meta.get("checks"), "`checks`", required=False)
    setup = _str_list(meta.get("setup"), "`setup`", required=False)
    exhausted = meta.get("on_rounds_exhausted", "carry")
    if exhausted not in ON_ROUNDS_EXHAUSTED:
        raise IdeaError(f"`on_rounds_exhausted` must be one of {list(ON_ROUNDS_EXHAUSTED)}")
    idea = Idea(key, (path.resolve().parent / Path(repo).expanduser()).resolve(), text,
                meta.get("base_branch", "master"), tuple(checks), tuple(setup), phases,
                meta.get("cost_budget_usd", 50.0), meta.get("wall_budget_s", 21600.0), exhausted)
    try:  # the stages must be valid tasks: the same validator, on what the builder will write
        validate_task(plan_task(idea, "true", {}))
        validate_task(phase_task(idea, {"key": "x", "objective": "x", "acceptance_criteria": ["x"],
                                        "checks": ["true"]}, "master", {}))
    except TaskError as e:
        raise IdeaError(str(e)) from e
    if not (idea.repo / ".git").exists():
        raise IdeaError(f"`repo` is not a git repository: {idea.repo}")
    return idea


def write_idea(idea: Idea, dest: Path) -> None:
    """The idea as admitted, `repo` made absolute so the copy reads the same from anywhere."""
    meta: dict[str, Any] = {"idea_key": idea.idea_key, "repo": str(idea.repo),
                            "base_branch": idea.base_branch, "max_phases": idea.max_phases,
                            "cost_budget_usd": idea.cost_budget_usd,
                            "wall_budget_s": idea.wall_budget_s,
                            "on_rounds_exhausted": idea.on_rounds_exhausted}
    if idea.checks:
        meta["checks"] = list(idea.checks)
    if idea.setup:
        meta["setup"] = list(idea.setup)
    Path(dest).write_text("---\n" + yaml.safe_dump(meta, sort_keys=False, width=200) + "---\n"
                          + idea.text + "\n")


def check_plan_command(idea_copy: Path, path: str) -> str:
    # The check environment's PATH leaves out ratchetloop's venv: name its interpreter outright.
    return (f"{shlex.quote(sys.executable)} -m ratchetloop.idea check-plan {path} "
            f"--idea {shlex.quote(str(idea_copy))}")


def _package_root() -> str:
    """Where this very package is imported from, for the plan check's PYTHONPATH: the check must
    validate with the code that will build (a worktree or a review of ratchetloop itself otherwise
    ran the interpreter's installed copy, which refused the idea keys this copy writes)."""
    return str(Path(__file__).resolve().parents[1])


def plan_task(idea: Idea, check_cmd: str, budgets: dict[str, float]) -> dict[str, Any]:
    setup_note = f", after setup ({'; '.join(idea.setup)})" if idea.setup else ""
    idea_checks = ", ".join(f"`{c}`" for c in idea.checks) or "nothing more"
    path = plan_path(idea)
    return {"task_key": plan_key(idea), "kind": "doc", "repo": str(idea.repo),
            "objective": PLAN_OBJECTIVE.format(idea=idea.text, max_phases=idea.max_phases,
                                               setup_note=setup_note, idea_checks=idea_checks,
                                               plan_path=path),
            "acceptance_criteria": [c.format(plan_path=path) for c in PLAN_CRITERIA],
            "allowed_paths": ["docs/"],
            "base_branch": idea.base_branch, "checks": [check_cmd],
            "env": {"PYTHONPATH": _package_root()}, **budgets}


def phase_task(idea: Idea, phase: dict[str, Any], prev_branch: str,
               budgets: dict[str, float], carried: list[str] | None = None) -> dict[str, Any]:
    task: dict[str, Any] = {
        "task_key": phase_key(idea, phase["key"]), "kind": "code", "repo": str(idea.repo),
        "objective": PHASE_OBJECTIVE.format(key=phase["key"], objective=phase["objective"],
                                            plan_path=plan_path(idea)),
        "acceptance_criteria": phase["acceptance_criteria"],
        "checks": list(phase["checks"]) + list(idea.checks), "base_branch": prev_branch, **budgets}
    if idea.setup:
        task["setup"] = list(idea.setup)
    if phase.get("allowed_paths"):
        task["allowed_paths"] = phase["allowed_paths"]
    if carried:  # open residuals of earlier stages, for both briefs (§9.5)
        task["carried_findings"] = list(carried)
    return task


def plan_phases(plan_md: str, path: str = "the plan") -> list[Any]:
    blocks = FENCE.findall(plan_md)
    if len(blocks) != 1:
        raise IdeaError(f"{path} must hold exactly one fenced yaml block; it holds "
                        f"{len(blocks)}")
    try:
        data = yaml.safe_load(blocks[0])
    except yaml.YAMLError as e:
        raise IdeaError(f"the plan block is not valid YAML: {e}") from e
    if not isinstance(data, dict) or set(data) != {"phases"} or not isinstance(data["phases"], list):
        raise IdeaError("the plan block must be a mapping with one key, `phases`, holding a list")
    return data["phases"]


def validate_plan(plan_md: str, idea: Idea) -> list[dict[str, Any]]:
    """The plan's phases, each checked as the task it becomes; IdeaError names the first problem."""
    phases = plan_phases(plan_md, plan_path(idea))
    if not 1 <= len(phases) <= idea.max_phases:
        raise IdeaError(f"the plan has {len(phases)} phases; it may have 1 to {idea.max_phases}")
    seen: set[str] = set()
    for n, phase in enumerate(phases, 1):
        if not isinstance(phase, dict):
            raise IdeaError(f"phase {n} must be a mapping")
        unknown = set(phase) - PHASE_KEYS
        if unknown:
            raise IdeaError(f"phase {n}: unknown keys {sorted(unknown)}")
        key = phase.get("key")
        if not isinstance(key, str) or not PHASE_KEY.fullmatch(key) or key == "plan":
            raise IdeaError(f"phase {n}: `key` must match [a-z0-9][a-z0-9-]{{0,20}} and not be "
                            f"`plan`")
        if key in seen:
            raise IdeaError(f"phase {n}: the key {key!r} is used twice")
        seen.add(key)
        if not isinstance(phase.get("objective"), str) or not phase["objective"].strip():
            raise IdeaError(f"phase {key}: `objective` must be a non-empty string")
        if not phase.get("checks"):
            raise IdeaError(f"phase {key}: needs its own `checks`, the proof of its exit criterion")
        for name in ("acceptance_criteria", "checks", "allowed_paths"):
            if name in phase:
                _str_list(phase[name], f"phase {key}: `{name}`")
        try:
            validate_task(phase_task(idea, phase, "master", {}))
        except TaskError as e:
            raise IdeaError(f"phase {key}: {e}") from e
    return phases


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ratchetloop.idea")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check-plan", help="the plan stage's check (D29)")
    check.add_argument("plan")
    check.add_argument("--idea", required=True)
    args = parser.parse_args(argv)
    try:
        idea = load_idea(Path(args.idea))
        phases = validate_plan(Path(args.plan).read_text(encoding="utf-8"), idea)
        if not Path(DESIGN_PATH).is_file():
            raise IdeaError(f"{DESIGN_PATH} is missing")
    except (IdeaError, OSError) as e:
        print(f"plan check: {e}", file=sys.stderr)
        return 1
    print(f"plan check: {len(phases)} phases: {', '.join(p['key'] for p in phases)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
