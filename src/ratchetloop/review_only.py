"""`ratchetloop review TASK.yaml --branch B [--reviewer P] [--base SHA]` (CONTRACT.md §1): one review
round on a branch that already exists — predecessor's `kernel review --base`, the answer when a branch
must be judged again after its base moved, without a rebase. The task file supplies the objective,
criteria, setup and checks. Nothing is committed and no branch is locked: the checks run at the
branch head in a detached worktree, then a reviewer of another model family than any coder recorded
for the task reads base..HEAD. The record goes under `<runs_root>/<task_key>.review/`, apart from the
task's own runs, so `--continue` never takes it for one of them."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

from . import gitops
from .events import EventSink
from .lifecycle import hold_task, write_lock
from .model_choice import for_launch
from .preserve import _ensure_exclude, _remove_worktree, _write_preserved
from .result import build_result, write_result
from .resume import coder_families
from .review_step import review
from .reviewers import pick
from .run import PROVIDER_CLI, _new_run_dir, runs_root
from .signals import RunKilled, closing, kill_handler
from .state import DISPOSITIONS, REFUSED_EXIT, Finish, Refused, RunState
from .steps import checks_event, run_the_checks, setup
from .task import Task, TaskError, load_task
from .worktree import _worktree_path, repo_lock


def _family(provider: str, task: Task, policy: Path | None, root: Path) -> str | None:
    spec = {"tier": task.tier} if task.tier else {}
    return for_launch("reviewer", provider, spec=spec, task_runs=root / task.task_key,
                      policy_path=policy)["family"]


def _admit(task_path: Path, branch: str, base: str | None, reviewer: str | None,
           policy: Path | None, root: Path) -> tuple[Task, str, str, str, set[str]]:
    try:
        task = load_task(Path(task_path))
    except TaskError as e:
        raise Refused(str(e)) from e
    repo = task.repo
    head = gitops.git_try("rev-parse", "--verify", "-q", f"refs/heads/{branch}", cwd=repo)
    if not head:
        raise Refused(f"branch {branch!r} is not in {repo}")
    base_sha = gitops.git_try("rev-parse", "--verify", "-q", f"{base}^{{commit}}", cwd=repo) \
        if base else gitops.git_try("merge-base", head, task.base_branch, cwd=repo)
    if not base_sha or gitops.git_try("merge-base", "--is-ancestor", base_sha, head,
                                      cwd=repo) is None:
        raise Refused(f"base {base or task.base_branch!r} is not an ancestor of {branch}")
    avoid = coder_families(root / task.task_key,
                           lambda p: _family(p, task, policy, root) if p in PROVIDER_CLI else None)
    chosen = reviewer or pick(None, lambda p: _family(p, task, policy, root), runs_root=root,
                              avoid=avoid)
    if chosen is None or _family(chosen, task, policy, root) in (None, *avoid):
        raise Refused(f"no reviewer provably of another family than the task's coders {sorted(avoid)} "
                      f"(D6)")
    if shutil.which(PROVIDER_CLI[chosen]) is None:
        raise Refused(f"the {chosen} CLI is not on PATH")
    return task, head, base_sha, chosen, avoid


def _round(st: RunState, head: str) -> None:
    repo = st.task.repo
    with repo_lock(repo):
        gitops.git("worktree", "add", "--detach", str(st.tree), head, cwd=repo)
    _ensure_exclude(st.tree)
    st.sink.emit("worktree", path=str(st.tree), reused=False)
    st.head_sha = head
    setup(st)
    ok, results = run_the_checks(st, 1, {"changed": []})
    now = gitops.git("rev-parse", "HEAD", cwd=st.tree)
    if now != head:  # the checks must cover the head the reviewer is shown (Phase 6 review, 3)
        raise Finish("FAILED", "scope_violation",
                     f"setup or the checks moved HEAD from {head[:12]} to {now[:12]}")
    checks_event(st, 1, ok, results, head if ok else None)
    if not ok:
        raise Finish("FAILED", "checks_failed", "the checks are red at the branch head")
    st.checks_passed, st.checks_head = True, head
    if review(st, 1, results) == "APPROVE":
        raise Finish("ACCEPT", "approved")
    raise Finish("HUMAN_REVIEW", "review_rounds_exhausted", "the reviewer asked for changes")


def _dispose(st: RunState) -> None:
    """The tree holds nothing of value unless the reviewer wrote in it: then it is kept as evidence."""
    if st.tree is None or not st.tree.exists():
        return
    if st.reason == "reviewer_wrote":
        _write_preserved(st.tree, "reviewer wrote", task_key=st.task.task_key)
        st.worktree_kept = str(st.tree)
        return
    with repo_lock(st.task.repo):
        _remove_worktree(st.task.repo, st.tree, force=True)


def review_branch(task_path: Path, branch: str, *, base: str | None = None,
                  reviewer: str | None = None, policy_path: Path | None = None,
                  root: Path | None = None) -> dict[str, Any]:
    root = root or runs_root()
    task = load_task(Path(task_path)) if Path(task_path).is_file() else None
    key = f"{task.task_key if task else Path(task_path).stem}.review"
    hold = hold_task(root, key)
    try:
        task, head, base_sha, chosen, avoid = _admit(task_path, branch, base, reviewer,
                                                     policy_path, root)
        run_id, run_dir = _new_run_dir(root / key)
        shutil.copyfile(task_path, run_dir / "task.yaml")
        write_lock(run_dir)
        st = RunState(task=task, run_id=run_id, run_dir=run_dir, runs_root=root,
                      sink=EventSink(run_dir / "events.jsonl", run_id), coder="", reviewer=chosen,
                      coder_family="", policy_path=policy_path, branch=branch, avoid=avoid,
                      base_sha=base_sha, tree=_worktree_path(task.repo, key))
        crashed: BaseException | None = None
        with kill_handler():
            try:
                st.sink.emit("admit", task_key=task.task_key, kind="review", repo=str(task.repo),
                             base_sha=base_sha, branch=branch, head=head, reviewer=chosen,
                             avoid=sorted(avoid))
                _round(st, head)
            except Finish as f:
                st.disposition, st.reason, st.detail = f.disposition, f.reason, f.detail
            except RunKilled as k:
                st.disposition, st.reason, st.detail = "STOPPED", "killed", f"signal {k}"
            except BaseException as e:  # noqa: BLE001 — recorded, then re-raised
                st.disposition, st.reason, st.detail = "FAILED", "crash", f"{type(e).__name__}: {e}"
                crashed = e
            finally:
                closing()
                try:
                    _dispose(st)
                except Exception as e:  # noqa: BLE001 — the result must still be written
                    st.detail = f"{st.detail}; worktree: {e}".lstrip("; ")
                st.sink.emit("disposition", disposition=st.disposition, reason=st.reason,
                             detail=st.detail)
                result = build_result(st)
                result["kind"] = "review"  # not a task run: stats counts its spend only (review, 5)
                write_result(run_dir / "result.json", result)
                (run_dir / "lock").unlink(missing_ok=True)
        if crashed is not None:
            raise crashed
        return result
    finally:
        hold.release()


def cmd_review(args: Any) -> int:
    try:
        res = review_branch(Path(args.task), args.branch, base=args.base, reviewer=args.reviewer,
                            policy_path=args.policy)
    except (Refused, TaskError) as e:
        print(f"ratchetloop: refused: {e}", file=sys.stderr)
        return REFUSED_EXIT
    print(json.dumps({k: res.get(k) for k in ("task_key", "run_id", "disposition", "reason",
                                              "branch", "base_sha", "head_sha")}))
    return DISPOSITIONS[res["disposition"]]
