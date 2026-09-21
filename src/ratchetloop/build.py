"""`ratchetloop build IDEA.md` (CONTRACT.md §9, D28): an idea taken through a design-and-plan stage and
one code stage per phase, each an ordinary task run. Stages chain branches — each starts from the
previous stage's branch, so its review sees only its own commits — and after each ACCEPT the idea's
own branch, `ratchetloop/<idea_key>`, is fast-forwarded to it: the one branch a person merges. The
chain stops at the first stage short of ACCEPT; `build --continue` skips the stages already ACCEPTed
and picks up there (the stage itself with `run --continue` when its branch exists)."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from . import gitops
from .carry import carriable, carried_before, carry_stage, record_carried
from .events import EventSink
from .idea import (
    Idea, IdeaError, check_plan_command, load_idea, phase_key, phase_task, plan_key, plan_path,
    plan_task, stage_branch, validate_plan, write_idea,
)
from .lifecycle import LOCK, hold_task, lock_alive, read_lock, write_lock
from .result import write_result
from .residuals import RESIDUALS, as_lines, mark_resolved, parse_notes, save
from .resume import resumable_result, run_dirs
from .run import _new_run_dir, admit, run_task, runs_root
from .signals import RunKilled, closing, kill_handler, killed_why
from .state import DISPOSITIONS, Refused
from .worktree import repo_lock

IDEA_EXIT = {**DISPOSITIONS, "PAUSED": 6}
APPROVAL = "approved.json"
USAGE_KEYS = ("tokens_in", "tokens_out", "cost_usd", "ai_credits", "launches_with_unknown_usage")


class Stop(Exception):
    """Ends the idea with a disposition, a reason, and the stage it stopped at."""

    def __init__(self, disposition: str, reason: str, stage: str | None = None, detail: str = ""):
        super().__init__(f"{disposition} {reason} {stage or ''} {detail}".strip())
        self.disposition, self.reason, self.stage, self.detail = disposition, reason, stage, detail


@dataclass
class IdeaRun:
    idea: Idea
    run_id: str
    run_dir: Path
    root: Path
    sink: EventSink
    gate: str | None = None
    policy_path: Path | None = None
    coder: str | None = None
    reviewer: str | None = None
    started: float = field(default_factory=time.time)
    stages: list[dict[str, Any]] = field(default_factory=list)
    residuals: list[dict[str, Any]] = field(default_factory=list)  # carried, cumulative (§9.5)
    spent_usd: float = 0.0
    base_sha: str = ""
    phases_planned: int | None = None
    disposition: str = ""
    reason: str = ""
    stage: str | None = None
    detail: str = ""

    @property
    def branch(self) -> str:
        return stage_branch(self.idea.idea_key)


def _head(repo: Path, branch: str) -> str | None:
    return gitops.git_try("rev-parse", "--verify", "-q", f"refs/heads/{branch}", cwd=repo) or None


def _ancestor(repo: Path, old: str, new: str) -> bool:
    return gitops.git_try("merge-base", "--is-ancestor", old, new, cwd=repo) is not None


def _advance(ir: IdeaRun, new: str, key: str) -> None:
    """Fast-forward the idea's branch to an ACCEPTed stage's head: never a rewind, never a branch
    someone has checked out. A head it already holds (a stage skipped on `--continue`) is a no-op."""
    repo, ref = ir.idea.repo, f"refs/heads/{ir.branch}"
    with repo_lock(repo):
        old = _head(repo, ir.branch)
        if old == new or (old and _ancestor(repo, new, old)):
            return
        if old and not _ancestor(repo, old, new):
            raise Stop("FAILED", "idea_branch", key, f"{ir.branch} at {old[:12]} is not an "
                       f"ancestor of {new[:12]}: it was moved outside the pipeline")
        listing = gitops.git_try("worktree", "list", "--porcelain", cwd=repo) or ""
        if f"branch {ref}" in listing.splitlines():
            raise Stop("FAILED", "idea_branch", key, f"{ir.branch} is checked out; the pipeline "
                       f"does not move a checked-out branch")
        gitops.git("update-ref", ref, new, old or "0" * 40, cwd=repo)


def _boundary(ir: IdeaRun, key: str) -> dict[str, float]:
    """Between stages: the STOP sentinel and the idea's budgets (§8, §9.4); the stage gets what is
    left of them."""
    if (ir.root / "STOP").exists():
        raise Stop("STOPPED", "stop_sentinel", key, str(ir.root / "STOP"))
    wall = ir.idea.wall_budget_s - (time.time() - ir.started)
    cost = ir.idea.cost_budget_usd - ir.spent_usd
    if wall <= 0:
        raise Stop("FAILED", "budget_wall", key)
    if cost <= 0:  # nothing left, not merely overspent (Phase 5 review, finding 4)
        raise Stop("FAILED", "budget_cost", key, f"${ir.spent_usd:.2f} measured")
    return {"wall_budget_s": round(wall, 1), "cost_budget_usd": round(cost, 6)}


def _stage(ir: IdeaRun, task: dict[str, Any]) -> None:
    """Run one stage, or skip it if its latest run ACCEPTed and its branch still holds exactly that
    head — for the plan stage, that head or a person's edit on top of it (the gate). A code stage's
    branch that moved on holds unreviewed commits: it is not skipped, so admit refuses it, and they
    never reach the idea's branch (Phase 5 review). Stop the idea unless the stage ends ACCEPT;
    then fast-forward the idea's branch."""
    key, repo = task["task_key"], ir.idea.repo
    head = _head(repo, stage_branch(key))
    prior = resumable_result(ir.root / key)
    accepted = prior.get("head_sha") if prior and prior.get("disposition") == "ACCEPT" else None
    carried = carried_before(ir.root / key, prior)
    if accepted and head and (head == accepted or (
            task["kind"] == "doc" and _ancestor(repo, accepted, head))):
        _resolve(ir, key, prior)  # replayed in order, so earlier closures hold (review, 1)
        ir.stages.append({"task_key": key, "run_id": prior.get("run_id"), "disposition": "ACCEPT",
                          "reason": prior.get("reason"), "skipped": True, "result": prior})
        ir.sink.emit("stage_end", task_key=key, stage_run=prior.get("run_id"),
                     disposition="ACCEPT", skipped=True)
    elif carried and head == carried["head_sha"]:  # carried by an earlier build (§9.5)
        record_carried(ir, key, prior, carried["residuals"], skipped=True)
    else:
        path = ir.run_dir / "stages" / f"{key}.yaml"
        path.parent.mkdir(exist_ok=True)
        path.write_text(yaml.safe_dump(task, sort_keys=False, width=200))
        ir.sink.emit("stage_start", task_key=key, continued=head is not None)
        try:
            adm = admit(path, coder=ir.coder, reviewer=ir.reviewer, policy_path=ir.policy_path,
                        root=ir.root, continue_=head is not None)
        except Refused as e:
            ir.sink.emit("stage_end", task_key=key, disposition="FAILED", reason="refused")
            raise Stop("FAILED", "refused", key, str(e)) from e
        res = run_task(path, adm, policy_path=ir.policy_path)
        ir.spent_usd += float((res.get("usage") or {}).get("cost_usd") or 0.0)
        _resolve(ir, key, res)
        if carriable(ir.idea, task, res):
            found, _ = carry_stage(ir.idea, key, stage_branch(key), res, ir.root / key)
            record_carried(ir, key, res, found, skipped=False)
        else:
            ir.stages.append({"task_key": key, "run_id": res.get("run_id"),
                              "disposition": res.get("disposition"), "reason": res.get("reason"),
                              "skipped": False, "result": res})
            ir.sink.emit("stage_end", task_key=key, stage_run=res.get("run_id"),
                         disposition=res.get("disposition"), reason=res.get("reason"))
            if res.get("disposition") != "ACCEPT":
                raise Stop(res.get("disposition") or "FAILED", res.get("reason") or "crash", key,
                           res.get("detail") or "")
    _advance(ir, _head(repo, stage_branch(key)) or "", key)
    why = killed_why()
    if why:  # landed while the stage wrote its result (Phase 5 review, finding 1)
        # Recorded there, not raised; the next stage's handler would have cleared it and started
        # another paid stage.
        raise Stop("STOPPED", "killed", key, f"signal {why} as the stage closed")


def _resolve(ir: IdeaRun, key: str, res: dict[str, Any]) -> None:
    """Close what a stage's review names; keep an ACCEPTed review's `notes:` as nit residuals."""
    summary = res.get("review_summary") or ""
    closed = mark_resolved(ir.residuals, key, summary)
    notes = parse_notes(summary, key, int((res.get("velocity") or {}).get("review_rounds") or 0)) \
        if res.get("disposition") == "ACCEPT" else []
    known = {(r.get("stage"), r.get("text")) for r in ir.residuals}
    ir.residuals += [n for n in notes if (n["stage"], n["text"]) not in known]
    if closed or notes:
        save(ir.run_dir / RESIDUALS, ir.residuals)


def _approved(idea_runs: Path) -> bool:
    return any((d / APPROVAL).is_file() for d in run_dirs(idea_runs))


def _paused_before(idea_runs: Path, current: str) -> bool:
    """An earlier build of the idea stopped at the gate: it holds until `approve`, with or without
    `--gate` on this build (Phase 5 review, finding 3)."""
    for d in run_dirs(idea_runs):
        try:
            if d.name != current and json.loads((d / "result.json").read_text()).get(
                    "disposition") == "PAUSED":
                return True
        except (OSError, json.JSONDecodeError):
            continue
    return False


def _chain(ir: IdeaRun) -> None:
    idea, repo = ir.idea, ir.idea.repo
    ir.base_sha = gitops.git("rev-parse", f"{idea.base_branch}^{{commit}}", cwd=repo)
    pkey, path = plan_key(idea), plan_path(idea)
    _stage(ir, plan_task(idea, check_plan_command(ir.run_dir / "idea.md", path),
                         _boundary(ir, pkey)))
    gate = ir.gate == "plan" or _paused_before(ir.root / idea.idea_key, ir.run_id)
    if gate and not _approved(ir.root / idea.idea_key):
        ir.sink.emit("gate", stage=pkey)
        raise Stop("PAUSED", "gate", pkey, f"`ratchetloop approve {idea.idea_key}`, then "
                   f"`build --continue`")
    prev = stage_branch(pkey)
    try:  # re-read at the branch head: a person may have edited the plan at the gate
        phases = validate_plan(gitops.git_try("show", f"{prev}:{path}", cwd=repo) or "", idea)
    except IdeaError as e:
        raise Stop("FAILED", "checks_failed", pkey, f"{path} at {prev}: {e}") from e
    ir.phases_planned = len(phases)
    for phase in phases:
        key = phase_key(idea, phase["key"])
        _stage(ir, phase_task(idea, phase, prev, _boundary(ir, key), carried=as_lines(ir.residuals)))
        prev = stage_branch(key)
    raise Stop("ACCEPT", "approved")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def idea_result(ir: IdeaRun) -> dict[str, Any]:
    """CONTRACT.md §9.3: usage and velocity summed over every stage's own result.json."""
    results = [s["result"] for s in ir.stages]
    usage = {k: sum((r.get("usage") or {}).get(k) or 0 for r in results) for k in USAGE_KEYS}
    usage["cost_usd"] = round(usage["cost_usd"], 6)
    vel = [r.get("velocity") or {} for r in results]
    code = [r for r in results if r.get("kind") == "code"]
    now = time.time()
    return {
        "idea_key": ir.idea.idea_key, "run_id": ir.run_id, "kind": "idea",
        "disposition": ir.disposition, "reason": ir.reason, "stage": ir.stage,
        "detail": ir.detail, "repo": str(ir.idea.repo), "branch": ir.branch,
        "base_sha": ir.base_sha or None, "head_sha": _head(ir.idea.repo, ir.branch),
        "stages": [{k: s[k] for k in ("task_key", "run_id", "disposition", "reason", "skipped")}
                   | ({"carried": True, "residuals": s["residuals"]} if s.get("carried") else {})
                   for s in ir.stages],
        "residuals": ir.residuals,
        "usage": usage,
        "velocity": {
            "phases_planned": ir.phases_planned,
            "phases_accepted": sum(r.get("disposition") == "ACCEPT" for r in code),
            "phases_carried": sum(bool(s.get("carried")) for s in ir.stages),
            "lines_added": sum(v.get("lines_added") or 0 for v in vel),
            "lines_removed": sum(v.get("lines_removed") or 0 for v in vel),
            "tests_added": sum(v.get("tests_added") or 0 for v in vel),
            "review_rounds": sum(v.get("review_rounds") or 0 for v in vel),
            "wall_s": round(now - ir.started, 3)},
        "started": _iso(ir.started), "ended": _iso(now),
    }


def _settle(idea_runs: Path) -> None:
    """A build that died leaves its lock: record it (its stages keep their own records)."""
    for d in run_dirs(idea_runs):
        if not (d / LOCK).exists():
            continue
        if lock_alive(d):
            raise Refused(f"idea {idea_runs.name} is being built (pid {read_lock(d)[0]}, {d})")
        if not (d / "result.json").exists():
            write_result(d / "result.json", {
                "idea_key": idea_runs.name, "run_id": d.name, "kind": "idea",
                "disposition": "FAILED", "reason": "abandoned",
                "detail": "the build died; its stages keep their own records"})
        (d / LOCK).unlink(missing_ok=True)


def _run(ir: IdeaRun) -> dict[str, Any]:
    crashed: BaseException | None = None
    with kill_handler():
        try:
            ir.sink.emit("idea_start", idea_key=ir.idea.idea_key, gate=ir.gate)
            _chain(ir)
        except Stop as s:
            ir.disposition, ir.reason, ir.stage, ir.detail = s.disposition, s.reason, s.stage, s.detail
        except RunKilled as k:
            ir.disposition, ir.reason, ir.detail = "STOPPED", "killed", f"signal {k}"
        except BaseException as e:  # noqa: BLE001 — recorded, then re-raised
            ir.disposition, ir.reason, ir.detail = "FAILED", "crash", f"{type(e).__name__}: {e}"
            crashed = e
        finally:
            closing()
            result = idea_result(ir)
            write_result(ir.run_dir / "result.json", result)
            ir.sink.emit("idea_end", disposition=ir.disposition, reason=ir.reason, stage=ir.stage)
            (ir.run_dir / LOCK).unlink(missing_ok=True)
    if crashed is not None:
        raise crashed
    return result


def _refusals(idea: Idea, root: Path, continue_: bool) -> None:
    """What `build` refuses before an idea run exists (exit 2). The caller holds the idea."""
    idea_runs = root / idea.idea_key
    _settle(idea_runs)
    begun = _head(idea.repo, stage_branch(idea.idea_key)) or _head(
        idea.repo, stage_branch(plan_key(idea)))
    if begun and not continue_:
        raise Refused(f"idea {idea.idea_key} was built before: `build --continue` picks it "
                      f"up; a fresh attempt is a new idea_key")
    if continue_ and not run_dirs(idea_runs):
        raise Refused(f"nothing to continue: no earlier build of {idea.idea_key}")
    if gitops.git_try("rev-parse", "--verify", "-q", f"{idea.base_branch}^{{commit}}",
                      cwd=idea.repo) is None:
        raise Refused(f"base_branch {idea.base_branch!r} is not in {idea.repo}")


def build(idea_path: Path, *, gate: str | None = None, continue_: bool = False,
          policy_path: Path | None = None, coder: str | None = None, reviewer: str | None = None,
          root: Path | None = None) -> dict[str, Any]:
    """Build an idea; returns its result.json. Refused (exit 2) before any run directory."""
    root = root or runs_root()
    try:
        idea = load_idea(Path(idea_path))
    except IdeaError as e:
        raise Refused(str(e)) from e
    hold = hold_task(root, idea.idea_key)  # one build of an idea at a time
    try:
        _refusals(idea, root, continue_)
        idea_runs = root / idea.idea_key
        run_id, run_dir = _new_run_dir(idea_runs)
        write_idea(idea, run_dir / "idea.md")
        write_lock(run_dir)
        return _run(IdeaRun(idea, run_id, run_dir, root, EventSink(run_dir / "events.jsonl", run_id),
                            gate, policy_path, coder, reviewer))
    finally:
        hold.release()
