"""Carrying a stage whose review rounds ran out (CONTRACT.md §9.5, D35): with green checks and no
blocking finding, an idea build records the residual findings, commits them to the stage branch as
docs/REVIEW_NOTES.md, and goes on to the next phase instead of stopping for a person. The stage's
own result.json is not rewritten; `carried.json` beside it says the idea carried it and at which
head, so `build --continue` skips it like an ACCEPTed stage."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from . import gitops
from .residuals import NOTES_HEADER, NOTES_PATH, RESIDUALS, notes_section, parse_findings, save
from .worktree import _maybe_commit, _worktree_path, repo_lock

CARRIED = "carried.json"


def carriable(idea: Any, task: dict[str, Any], res: dict[str, Any]) -> bool:
    """Only a code stage that ended `review_rounds_exhausted` with its checks green at the reviewed
    head and no blocking finding, and only when the idea says `carry` (the default)."""
    return (getattr(idea, "on_rounds_exhausted", "carry") == "carry"
            and task.get("kind") == "code"
            and res.get("disposition") == "HUMAN_REVIEW"
            and res.get("reason") == "review_rounds_exhausted"
            and bool((res.get("checks") or {}).get("passed"))
            and not res.get("review_blocking"))


def _git_env(*args: str, cwd: Path, env: dict[str, str]) -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True, cwd=str(cwd),
                       env={**os.environ, **env}, timeout=120, check=True)
    return r.stdout.rstrip("\n")


def _commit_without_tree(repo: Path, branch: str, content: str, message: str) -> str:
    """Add or replace docs/REVIEW_NOTES.md on `branch` with plumbing only: no worktree of the branch
    exists, so the commit is built from a temporary index."""
    ref = f"refs/heads/{branch}"
    head = gitops.git("rev-parse", ref, cwd=repo)
    with tempfile.TemporaryDirectory() as tmp:
        blob_src = Path(tmp) / "notes.md"
        blob_src.write_text(content)
        blob = gitops.git("hash-object", "-w", str(blob_src), cwd=repo)
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        _git_env("read-tree", head, cwd=repo, env=env)
        _git_env("update-index", "--add", "--cacheinfo", f"100644,{blob},{NOTES_PATH}",
                 cwd=repo, env=env)
        tree = _git_env("write-tree", cwd=repo, env=env)
    commit = _git_env("commit-tree", tree, "-p", head, "-m", message, cwd=repo, env={})
    gitops.git("update-ref", ref, commit, head, cwd=repo)
    return commit


def commit_notes(repo: Path, task_key: str, branch: str, section: str) -> str:
    """Append `section` to docs/REVIEW_NOTES.md on the stage branch and return the new head. In
    the stage's preserved worktree when it still has the branch checked out (so the tree and the
    branch agree), else through the index-only path."""
    message = f"ratchetloop({task_key}): residual review notes"
    ref = f"refs/heads/{branch}"
    with repo_lock(repo):
        tree = _worktree_path(repo, task_key)
        if tree.exists() and gitops.git_try("symbolic-ref", "-q", "HEAD", cwd=tree) == ref:
            dest = tree / NOTES_PATH
            dest.parent.mkdir(parents=True, exist_ok=True)
            existing = dest.read_text() if dest.exists() else NOTES_HEADER
            dest.write_text(existing.rstrip("\n") + "\n" + section)
            gitops.git("reset", "-q", cwd=tree)
            gitops.git("add", "--", NOTES_PATH, cwd=tree)
            _maybe_commit(tree, message)
            return gitops.git("rev-parse", "HEAD", cwd=tree)
        existing = gitops.git_try("show", f"{ref}:{NOTES_PATH}", cwd=repo)
        content = (existing if existing is not None else NOTES_HEADER).rstrip("\n") + "\n" + section
        return _commit_without_tree(repo, branch, content, message)


def carry_stage(idea: Any, key: str, branch: str, res: dict[str, Any],
                stage_runs: Path) -> tuple[list[dict[str, Any]], str]:
    """Record the stage's residuals, commit the notes, mark the stage run carried; returns the
    residuals and the branch's new head."""
    rounds = int((res.get("velocity") or {}).get("review_rounds") or 0)
    found = parse_findings(res.get("review_summary") or "", key, rounds)
    head = commit_notes(idea.repo, key, branch, notes_section(key, str(res.get("run_id")), found))
    run_dir = stage_runs / str(res.get("run_id"))
    if run_dir.is_dir():
        (run_dir / CARRIED).write_text(json.dumps(
            {"head_sha": head, "residuals": found}, indent=1) + "\n")
    return found, head


def carried_before(stage_runs: Path, res: dict[str, Any] | None) -> dict[str, Any] | None:
    """The carried.json of the stage's latest resumable run, if the idea carried it."""
    if not res or res.get("disposition") != "HUMAN_REVIEW":
        return None
    try:
        data = json.loads((stage_runs / str(res.get("run_id")) / CARRIED).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and data.get("head_sha") else None


def record_carried(ir: Any, key: str, res: dict[str, Any], found: list[dict[str, Any]],
                   *, skipped: bool) -> None:
    """A stage the idea goes past with its residuals recorded (§9.5); its own record stays."""
    known = {(s.get("stage"), s.get("text")) for s in ir.residuals}
    ir.residuals += [dict(r) for r in found if (r.get("stage"), r.get("text")) not in known]
    save(ir.run_dir / RESIDUALS, ir.residuals)
    ir.stages.append({"task_key": key, "run_id": res.get("run_id"),
                      "disposition": res.get("disposition"), "reason": res.get("reason"),
                      "skipped": skipped, "carried": True, "residuals": found, "result": res})
    ir.sink.emit("stage_end", task_key=key, stage_run=res.get("run_id"),
                 disposition=res.get("disposition"), reason=res.get("reason"), carried=True,
                 residuals=len(found), skipped=skipped)
