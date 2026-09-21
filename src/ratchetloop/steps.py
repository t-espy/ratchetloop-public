"""The steps of one task run (CONTRACT.md §3.1): the worktree and setup, a coder launch and its scope
check, the checks, and the commit. Each reads and updates the RunState and raises Finish to end the
run. Workers are reached as `workers.run_worker` (patch contract: tests substitute the owner)."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from . import gitops, workers
from .checks import check_env, checks_cwd, run_checks, run_commands, run_setup
from .doc_checks import doc_check_results
from .git_guard import common_dir_guard
from .gitops import META_DIR
from .model_choice import for_launch
from .preserve import PRESERVED_NAME
from .result import numstat
from .resume import last_scope, load_residue, previous_attempt, save_residue
from .reviewers import record_quota
from .scope import Snapshot, _digest, check, git_state_changed, snapshot, violation
from .state import Finish, RunState
from .worker_brief import assemble_brief, coder_task_brief
from .worktree import (
    _add_worktree, _maybe_commit, _worktree_path, dirty_paths, repo_lock, stage_paths,
)

# A coder outcome other than `done` ends the run with this reason (CONTRACT.md §4, §6).
CODER_FAILED = {"quota": "quota_exhausted", "invalid_result": "invalid_result",
                "protocol_error": "invalid_result", "timeout": "budget_wall"}


def guard(st: RunState) -> None:
    """At every step boundary — each launch, the checks, the commit: the STOP sentinels, then the
    budgets (CONTRACT.md §6, §8)."""
    for stop in (st.runs_root / "STOP", st.run_dir / "STOP"):
        if stop.exists():
            raise Finish("STOPPED", "stop_sentinel", str(stop))
    if st.remaining_s() <= 0:
        raise Finish("FAILED", "budget_wall")
    if st.cost_usd > st.task.cost_budget_usd:
        raise Finish("FAILED", "budget_cost", f"${st.cost_usd:.2f} measured")


def account(st: RunState, outcome: Any) -> None:
    cost = (outcome.usage or {}).get("cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        st.cost_usd += float(cost)


def launch_budget(st: RunState) -> float:
    return max(st.remaining_s(), 1.0)


def open_worktree(st: RunState) -> None:
    """A fresh tree from base_branch; or, for `--continue`, the task's tree as the last run left it
    (admit checked its identity), or a new tree on the existing branch when that one is gone."""
    task = st.task
    st.tree = _worktree_path(task.repo, task.task_key)
    st.reused = bool(st.continued_from) and st.tree.exists()
    if st.reused:
        (st.tree / META_DIR / PRESERVED_NAME).unlink(missing_ok=True)  # in use again
        st.pipeline_dirt |= set(load_residue(st.tree))
    else:
        with repo_lock(task.repo):
            if st.continued_from:
                gitops.git("worktree", "prune", cwd=task.repo)
            _add_worktree(task.repo, st.tree, st.branch,
                          st.base_sha if st.continued_from else task.base_branch,
                          bool(st.continued_from))
    st.sink.emit("worktree", path=str(st.tree), reused=st.reused)
    note = common_dir_guard(st.tree, task.repo, task.task_key)
    if note:
        raise Finish("FAILED", "scope_violation", note)
    st.head_sha = gitops.git("rev-parse", "HEAD", cwd=st.tree)


def _pipeline_made(st: RunState, paths: set[str]) -> None:
    """Record untracked paths the pipeline made, in the state and — with their content, so a later
    rewrite is seen — in the tree for `--continue`."""
    st.pipeline_dirt |= paths
    save_residue(st.tree, {p: _digest(st.tree / p) for p in st.pipeline_dirt})


def setup(st: RunState) -> None:
    """Setup runs where the tree was made. A reused tree already has its outputs, and re-running
    `ln -s …/venv venv` on one would plant a link inside the first link's target."""
    st.env = check_env(st.task.env)
    if st.reused:
        st.sink.emit("setup", passed=True, wall_s=0.0, commands=[], skipped="reused worktree")
        return
    ok, results = run_setup(st.task.setup, st.tree, st.env, log=st.run_dir / "setup.log",
                            pidfile=st.run_dir / "setup.pid")
    st.sink.emit("setup", passed=ok, wall_s=round(sum(r["wall_s"] for r in results), 3),
                 commands=[{"cmd": r["cmd"], "exit": r["exit"]} for r in results])
    _pipeline_made(st, set(dirty_paths(st.tree)))  # a fresh tree: all of it is setup's
    if not ok:
        raise Finish("FAILED", "setup_failed", results[-1]["cmd"])


def round_start(st: RunState, round_: int) -> Snapshot:
    """The round's `before`. On a reused tree, round 1 splits what the last run left. The
    pipeline's own leftovers, unchanged since recorded, stay as already there; the rest is that
    run's uncommitted change and becomes this run's, scoped and committed with it — otherwise a
    coder could build on a file that never reaches the commit. If that run ended after its scope
    check (killed during the checks), only the paths the check named are its change; anything else
    is output of the interrupted checks (Phase 4 review, finding 4)."""
    snap = snapshot(st.tree)
    if round_ != 1 or not st.reused:
        return snap
    recorded = load_residue(st.tree)
    rest = {p for p in snap.dirty
            if p not in recorded or recorded[p] not in (None, snap.digests.get(p))}
    scoped = last_scope(st.task_runs / st.continued_from) if st.continued_from else None
    adopted = rest if scoped is None else rest & set(scoped)
    keep = snap.dirty - adopted
    st.pipeline_dirt |= keep
    st.adopted = sorted(adopted)
    return replace(snap, dirty=frozenset(keep), digests={p: snap.digests[p] for p in keep})


def coder(st: RunState, round_: int, before: Snapshot, *, check_failures: str | None = None,
          label: str | None = None) -> dict[str, Any]:
    """One coder launch, then the scope check against `before` (the round's start)."""
    guard(st)
    name = label or f"coder.{round_}"
    brief = st.run_dir / f"brief.{name}.md"
    previous = (previous_attempt(st.tree, st.base_sha, st.adopted)
                if st.continued_from and round_ == 1 else None)
    assemble_brief("coder", coder_task_brief(st.task, st.tree, round_=round_, findings=st.findings,
                                             check_failures=check_failures, previous=previous),
                   brief, kind=st.task.kind)
    st.coder_meta = for_launch("coder", st.coder, spec=st.spec, task_runs=st.task_runs,
                               policy_path=st.policy_path)
    out = workers.run_worker(
        "coder", st.coder, str(brief), str(st.tree), str(st.run_dir / f"{name}.log"),
        timeout=launch_budget(st), attempt=round_, wall_budget_s=launch_budget(st),
        model_meta=st.coder_meta, sink=st.sink)
    account(st, out)
    if out.kind == "blocked":
        raise Finish("HUMAN_REVIEW", "coder_blocked", out.reason)
    if out.kind != "done":
        if out.kind == "quota":
            record_quota(st.runs_root, st.coder)
        raise Finish("FAILED", CODER_FAILED.get(out.kind, "worker_error"), out.reason)
    scope = check(st.tree, before, st.task.allowed_paths)
    st.sink.emit("scope", round=round_, **scope)
    breach = violation(scope)
    if breach:
        raise Finish("FAILED", "scope_violation", breach)
    return scope


def _failures(results: list[dict[str, Any]]) -> str:
    return "\n\n".join(f"$ {r['cmd']}  (exit {r['exit']})\n{r['tail']}"
                       for r in results if r["exit"] != 0)


def run_the_checks(st: RunState, round_: int, scope: dict[str, Any]) -> tuple[bool, list]:
    guard(st)
    log, pidfile = st.run_dir / f"checks.{round_}.log", st.run_dir / "checks.pid"
    cwd = checks_cwd(st.tree, st.task.checks_cwd)
    if st.task.kind == "doc":
        committed = gitops.git("diff", "--name-only", f"{st.base_sha}..HEAD", cwd=st.tree)
        changed = sorted(set(committed.splitlines()) | set(scope["changed"]))
        results = doc_check_results(st.tree, changed)
        if st.task.checks:
            results += run_commands(st.task.checks, cwd, st.env, log=log, pidfile=pidfile)
        ok = all(r["exit"] == 0 for r in results)
    else:
        ok, results = run_checks(st.task.checks, cwd, st.env, log=log, pidfile=pidfile)
    st.checks_wall_s += sum(r["wall_s"] for r in results)
    _pipeline_made(st, set(dirty_paths(st.tree)) - set(scope["changed"]))
    return ok, results


def checks_event(st: RunState, round_: int, ok: bool, results: list, head_sha: str | None) -> None:
    st.sink.emit("checks", round=round_, head_sha=head_sha, passed=ok,
                 wall_s=round(sum(r["wall_s"] for r in results), 3),
                 commands=[{"cmd": r["cmd"], "exit": r["exit"]} for r in results])


def checked_change(st: RunState, round_: int, before: Snapshot) -> tuple[dict[str, Any], list]:
    """The round's change, checked. Red checks earn the coder one fix launch per run, without
    spending a review (D14); still red ends the run before anything is committed."""
    scope = coder(st, round_, before)
    if not scope["changed"] and st.head_sha == st.base_sha:
        raise Finish("FAILED", "no_change", "the coder changed nothing and the branch holds nothing")
    ok, results = run_the_checks(st, round_, scope)
    if not ok and not st.check_fix_used:
        checks_event(st, round_, ok, results, None)
        st.check_fix_used = True
        # What the checks left behind (a __pycache__) is not the fix launch's change; scoped
        # against the round's start alone, the fix was failed as out of scope (Phase 3 loop
        # tests, 2026-09-11). Its content is recorded, so rewriting it is the worker's change
        # (Phase 3 review, finding 3). The first launch's own changes stay the worker's.
        now = snapshot(st.tree)
        residue = now.dirty - before.dirty - set(scope["changed"])
        fix_before = replace(before, dirty=before.dirty | residue,
                             digests={**before.digests, **{p: now.digests[p] for p in residue}})
        scope = coder(st, round_, fix_before, check_failures=_failures(results),
                      label=f"coder.{round_}.fix")
        ok, results = run_the_checks(st, round_, scope)
    if not ok:
        checks_event(st, round_, ok, results, None)
        raise Finish("FAILED", "checks_failed", _failures(results)[:500])
    return scope, results


def commit(st: RunState, round_: int, before: Snapshot, scope: dict[str, Any],
           results: list) -> None:
    """Under the repo lock: re-check the shared git state (Phase 2 review, finding 3), stage
    exactly the worker's paths, commit. The checks event then names the commit they covered."""
    guard(st)
    with repo_lock(st.task.repo):
        if git_state_changed(st.tree, before):
            raise Finish("FAILED", "scope_violation", "git config, hooks or exclude changed before commit")
        stage_paths(st.tree, scope["changed"])
        committed = _maybe_commit(
            st.tree, f"ratchetloop({st.task.task_key}): round {round_}\n\nrun {st.run_id}")
    st.head_sha = gitops.git("rev-parse", "HEAD", cwd=st.tree)
    if committed:
        added, removed, _ = numstat(st.tree, f"{st.head_sha}~1", st.head_sha)
        st.sink.emit("commit", round=round_, sha=st.head_sha, paths=sorted(scope["changed"]),
                     lines_added=added, lines_removed=removed)
    st.checks_passed, st.checks_head = True, st.head_sha
    checks_event(st, round_, True, results, st.head_sha)
