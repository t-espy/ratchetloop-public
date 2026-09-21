"""One task run (CONTRACT.md §1–§6): admit, worktree, setup, then up to two rounds of coder → scope →
checks → commit → review, ending in a disposition. `result.json` is written on every exit path, a
crash or a SIGTERM included. Hand-rolled (D4): each step is a function over the RunState."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import gitops
from .events import EventSink
from .gitops import _parse_porcelain
from .lifecycle import TaskHold, hold_task, settle, write_lock
from .model_choice import for_launch
from .model_policy import ModelPolicyError
from .policy import PolicyError
from .result import build_result, write_result
from .resume import check_tree, coder_families, last_findings, resumable_result
from .review_step import review
from .reviewers import pick
from .signals import RunKilled, closing, kill_handler
from .state import Finish, Refused, RunState
from .steps import checked_change, commit, open_worktree, round_start, setup
from .task import Task, TaskError, load_task
from .worktree import _remove_or_preserve, _remove_worktree, repo_lock, status_porcelain

PROVIDER_CLI = {"grok": "grok", "codex": "codex", "claude": "claude", "copilot": "copilot"}
DEFAULT_CODER = "grok"  # D17: grok has never hit quota on the operator's plan


def runs_root() -> Path:
    raw = os.environ.get("RATCHETLOOP_RUNS_ROOT")
    return Path(raw).expanduser().resolve() if raw else Path.home() / "var" / "ratchetloop" / "runs"


def branch_name(task: Task) -> str:
    return f"ratchetloop/{task.task_key}"


def _family(role: str, provider: str, task: Task, policy: Path | None, root: Path) -> str | None:
    spec = {"tier": task.tier} if task.tier else {}
    return for_launch(role, provider, spec=spec, task_runs=root / task.task_key,
                      policy_path=policy)["family"]


@dataclass
class Admission:
    """What admit settled: the run's cast and, for `--continue`, where it picks up."""
    task: Task
    coder: str
    reviewer: str
    coder_family: str
    root: Path
    avoid: set[str] = field(default_factory=set)
    base_sha: str | None = None
    continued_from: str | None = None
    hold: TaskHold | None = None  # the task's flock, until the run ends

    def release(self) -> None:
        if self.hold is not None:
            self.hold.release()
            self.hold = None


def _cast(task: Task, coder: str | None, reviewer: str | None, policy: Path | None, root: Path,
          continue_: bool) -> tuple[str, str, str, set[str]]:
    def family(role: str, provider: str) -> str | None:
        return _family(role, provider, task, policy, root)

    coder = coder or task.coder or DEFAULT_CODER
    chosen = reviewer or task.reviewer
    try:
        coder_family = family("coder", coder)
        if coder_family is None:
            raise Refused(f"{coder}'s model family is unknown: give it a ladder rung naming its "
                          f"family (D6)")
        avoid = {coder_family}
        if continue_:  # every family that coded on the branch, not only this run's coder (D6)
            avoid |= coder_families(root / task.task_key,
                                    lambda p: family("coder", p) if p in PROVIDER_CLI else None)
        if chosen is None:
            chosen = pick(coder_family, lambda p: family("reviewer", p), runs_root=root,
                          avoid=avoid)
            if chosen is None:
                raise Refused("no reviewer of another model family is available")
        elif family("reviewer", chosen) in (None, *avoid):
            raise Refused(f"reviewer {chosen} is not provably of another family than every coder "
                          f"of this task (D6)")
    except (PolicyError, ModelPolicyError) as e:
        raise Refused(str(e)) from e
    return coder, chosen, coder_family, avoid


def _resume_point(task: Task, root: Path, branch: str) -> tuple[str, str]:
    """For `--continue`: (the base commit, the run picked up). Refuses what cannot be continued."""
    prior = resumable_result(root / task.task_key)
    if prior is None:
        raise Refused(f"no earlier run of {task.task_key} to continue")
    if prior.get("disposition") == "ACCEPT":
        raise Refused(f"{task.task_key} was accepted at {str(prior.get('head_sha'))[:12]}; a "
                      f"further change is a new task_key")
    base_sha = prior.get("base_sha")
    if not base_sha:
        raise Refused(f"run {prior.get('run_id')} recorded no base commit to continue from")
    problem = check_tree(task, branch, base_sha)
    if problem:
        raise Refused(f"cannot reuse the worktree: {problem}; `ratchetloop trees --discard` it "
                      f"if nothing in it is worth keeping")
    return base_sha, str(prior.get("run_id"))


def admit(task_path: Path, *, coder: str | None = None, reviewer: str | None = None,
          policy_path: Path | None = None, root: Path | None = None, continue_: bool = False,
          dry: bool = False) -> Admission:
    """Everything checkable before a run exists; raises Refused (exit 2) otherwise. The task is held
    from here to the end of the run (under `--check`, `dry`, only for the checking), and a dead
    earlier run of it is marked abandoned first — except under `--check`."""
    root = root or runs_root()
    try:
        task = load_task(Path(task_path))
    except TaskError as e:
        raise Refused(str(e)) from e
    hold = hold_task(root, task.task_key)
    try:
        adm = _admit(task, root, coder, reviewer, policy_path, continue_, dry)
    except BaseException:
        hold.release()
        raise
    if dry:
        hold.release()
    else:
        adm.hold = hold
    return adm


def _admit(task: Task, root: Path, coder: str | None, reviewer: str | None,
           policy_path: Path | None, continue_: bool, dry: bool) -> Admission:
    settle(root / task.task_key, root, dry=dry)
    coder, chosen, coder_family, avoid = _cast(task, coder, reviewer, policy_path, root, continue_)
    for provider in (coder, chosen):
        if shutil.which(PROVIDER_CLI[provider]) is None:
            raise Refused(f"the {provider} CLI is not on PATH")
    repo = task.repo
    if gitops.git_try("rev-parse", "--verify", "-q", f"{task.base_branch}^{{commit}}", cwd=repo) is None:
        raise Refused(f"base_branch {task.base_branch!r} is not in {repo}")
    branch = branch_name(task)
    exists = gitops.git_try("rev-parse", "--verify", "-q", f"refs/heads/{branch}", cwd=repo)
    if exists and not continue_:
        raise Refused(f"branch {branch} already exists: `--continue` picks it up; a fresh attempt "
                      f"is a new task_key (D15)")
    if continue_ and not exists:
        raise Refused(f"nothing to continue: branch {branch} does not exist")
    base_sha, continued_from = _resume_point(task, root, branch) if continue_ else (None, None)
    if not all(gitops.git_try("config", key, cwd=repo) for key in ("user.name", "user.email")):
        raise Refused("set git user.name and user.email in the target repository; the pipeline "
                      "commits there")
    return Admission(task, coder, chosen, coder_family, root, avoid, base_sha, continued_from)


def _new_run_dir(task_runs: Path) -> tuple[str, Path]:
    base = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for n in range(1, 100):
        run_id = base if n == 1 else f"{base}-{n}"
        try:
            (task_runs / run_id).mkdir(parents=True, exist_ok=False)
            return run_id, task_runs / run_id
        except FileExistsError:
            continue
    raise RuntimeError(f"no free run directory under {task_runs}")


def _dispose_worktree(st: RunState) -> None:
    """ACCEPT whose only leftovers are untracked paths the pipeline itself made (setup outputs,
    check residue): everything the worker changed is committed and the tree goes. Anything else — a
    tracked change, an untracked file nobody accounted for (a hook's output, say) — keeps the tree
    (item 65; Phase 3 review, finding 4)."""
    if st.tree is None or not st.tree.exists():
        return
    repo = st.task.repo
    with repo_lock(repo):
        lines = [ln for ln in status_porcelain(st.tree).splitlines() if ln]
        tracked_dirt = [ln for ln in lines if not ln.startswith("??")]
        untracked = {p for _, p in _parse_porcelain("\n".join(ln for ln in lines if ln.startswith("??")))}
        if st.disposition == "ACCEPT" and not tracked_dirt and untracked <= st.pipeline_dirt:
            _remove_worktree(repo, st.tree, force=True)
            st.sink.emit("worktree_end", path=str(st.tree), disposition="removed",
                         reason="accepted; untracked setup or check residue discarded")
            return
        disp = _remove_or_preserve(repo, st.tree, sink=st.sink, task_key=st.task.task_key)
    if disp.startswith("preserved"):
        st.worktree_kept = str(st.tree)


def _note(st: RunState, where: str, e: Exception) -> None:
    st.detail = f"{st.detail}; {where}: {type(e).__name__}: {e}".lstrip("; ")


def _close(st: RunState) -> dict:
    """Nothing before the write may stop result.json from being written (Phase 3 review,
    finding 1): each step's failure is noted in `detail` instead."""
    try:
        _dispose_worktree(st)
    except Exception as e:  # noqa: BLE001
        _note(st, "worktree", e)
    try:
        st.sink.emit("disposition", disposition=st.disposition, reason=st.reason, detail=st.detail)
    except Exception as e:  # noqa: BLE001
        _note(st, "events", e)
    try:
        result = build_result(st)
    except Exception as e:  # noqa: BLE001 — the record shrinks to what the state holds
        _note(st, "result", e)
        result = {"task_key": st.task.task_key, "run_id": st.run_id, "kind": st.task.kind,
                  "disposition": st.disposition, "reason": st.reason, "detail": st.detail,
                  "repo": str(st.task.repo), "branch": st.branch, "base_sha": st.base_sha or None,
                  "head_sha": st.head_sha or None, "worktree": st.worktree_kept}
    write_result(st.run_dir / "result.json", result)
    (st.run_dir / "lock").unlink(missing_ok=True)
    return result


def _rounds(st: RunState) -> None:
    open_worktree(st)
    setup(st)
    for round_ in range(1, st.task.max_review_rounds + 1):
        before = round_start(st, round_)
        scope, results = checked_change(st, round_, before)
        commit(st, round_, before, scope, results)
        if review(st, round_, results) == "APPROVE":
            if st.checks_passed and st.checks_head == st.head_sha:
                raise Finish("ACCEPT", "approved")
            raise Finish("FAILED", "checks_failed", "the checks did not cover the reviewed HEAD")
        st.findings = st.review_summary
    raise Finish("HUMAN_REVIEW", "review_rounds_exhausted")


def run_task(task_path: Path, adm: Admission, *, policy_path: Path | None = None) -> dict:
    """Run an admitted task to a disposition; returns the result.json contents. The task's hold is
    released only once the result is written."""
    try:
        return _run(task_path, adm, policy_path)
    finally:
        adm.release()


def _run(task_path: Path, adm: Admission, policy_path: Path | None) -> dict:
    task = adm.task
    run_id, run_dir = _new_run_dir(adm.root / task.task_key)
    shutil.copyfile(task_path, run_dir / "task.yaml")
    write_lock(run_dir)
    st = RunState(task=task, run_id=run_id, run_dir=run_dir, runs_root=adm.root,
                  sink=EventSink(run_dir / "events.jsonl", run_id), coder=adm.coder,
                  reviewer=adm.reviewer, coder_family=adm.coder_family, policy_path=policy_path,
                  branch=branch_name(task), avoid=set(adm.avoid), continued_from=adm.continued_from)
    crashed: BaseException | None = None
    with kill_handler():
        try:
            st.base_sha = adm.base_sha or gitops.git(
                "rev-parse", f"{task.base_branch}^{{commit}}", cwd=task.repo)
            st.sink.emit("admit", task_key=task.task_key, kind=task.kind, repo=str(task.repo),
                         base_sha=st.base_sha, branch=st.branch, coder=adm.coder,
                         coder_family=adm.coder_family, reviewer=adm.reviewer,
                         continued_from=adm.continued_from)
            if adm.continued_from:
                st.findings = last_findings(st.task_runs)
            _rounds(st)
        except Finish as f:
            st.disposition, st.reason, st.detail = f.disposition, f.reason, f.detail
        except RunKilled as k:  # SIGTERM/SIGINT; the active launch's group is already reaped
            st.disposition, st.reason, st.detail = "STOPPED", "killed", f"signal {k}"
        except BaseException as e:  # noqa: BLE001 — recorded as a crash, then re-raised
            st.disposition, st.reason, st.detail = "FAILED", "crash", f"{type(e).__name__}: {e}"
            crashed = e
        finally:
            closing()
            result = _close(st)
    if crashed is not None:
        raise crashed
    return result
