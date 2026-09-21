"""`run --continue` (D15): pick up a task's branch and worktree after a run that ended short of ACCEPT
or died. What the earlier runs left is read from their run directories: the base commit and outcome
from result.json, the last findings, every coder's model family and the dead run's last step from
events.jsonl, and the pipeline's own leftovers in the worktree, with their content, from
`.ratchetloop/residue.json`."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterator

from . import gitops
from .events import read_events
from .gitops import META_DIR
from .preserve import _read_identity, _worktree_registered
from .worktree import _worktree_path

RESIDUE = "residue.json"


def run_dirs(task_runs: Path) -> list[Path]:
    task_runs = Path(task_runs)
    return sorted(p for p in task_runs.iterdir() if p.is_dir()) if task_runs.is_dir() else []


def resumable_result(task_runs: Path) -> dict[str, Any] | None:
    """The newest result.json that records a base commit. A run abandoned before its admit event has
    none, and must not hide the run before it (Phase 4 review, finding 3)."""
    for run_dir in reversed(run_dirs(task_runs)):
        try:
            result = json.loads((run_dir / "result.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(result, dict) and result.get("base_sha"):
            return result
    return None


def _events(task_runs: Path) -> Iterator[dict[str, Any]]:
    for run_dir in run_dirs(task_runs):
        yield from read_events(run_dir / "events.jsonl")


def last_findings(task_runs: Path) -> str | None:
    """The most recent review of the task, when it asked for changes."""
    reviews = [e for e in _events(task_runs) if e.get("event") == "review" and e.get("verdict")]
    if reviews and reviews[-1]["verdict"] == "REQUEST_CHANGES":
        return reviews[-1].get("summary") or None
    return None


def coder_families(task_runs: Path, family_of: Callable[[str], str | None]) -> set[str]:
    """Every model family that coded on the task's branch: a reviewer may be none of them (D6)."""
    out: set[str] = set()
    for e in _events(task_runs):
        if e.get("event") == "admit":
            out.add(e.get("coder_family") or family_of(str(e.get("coder"))) or "")
        elif e.get("event") == "worker_end" and e.get("role") == "coder":
            out.add(e.get("family") or "")
    return out - {""}


def last_scope(run_dir: Path) -> list[str] | None:
    """The worker change the scope check named, when the run ended after that check and before a
    commit or another coder launch (it died during the checks, or stopped before them); None when it
    ended with a coder launch open, after a commit, or before any launch."""
    last = None
    for e in read_events(Path(run_dir) / "events.jsonl"):
        if e.get("event") in ("scope", "commit") or (
                e.get("event") == "worker_start" and e.get("role") == "coder"):
            last = e
    if last is None or last.get("event") != "scope":
        return None
    return [str(p) for p in last.get("changed") or []]


def check_tree(task: Any, branch: str, base_sha: str) -> str | None:
    """Why the task's existing worktree cannot be reused, or None (also when there is none)."""
    tree = _worktree_path(task.repo, task.task_key)
    if not tree.exists():
        return None
    ident = _read_identity(tree)
    found = (ident.get("task_key"), ident.get("branch"), ident.get("base_commit"))
    if found != (task.task_key, branch, base_sha):
        return f"{tree} was made for task {found[0]!r} on {found[1]!r} from {found[2]!r}"
    if not _worktree_registered(task.repo, tree):
        return f"{tree} is not a registered worktree of {task.repo}"
    head = gitops.git_try("symbolic-ref", "-q", "HEAD", cwd=tree)
    if head != f"refs/heads/{branch}":
        return f"{tree} has {head or 'a detached HEAD'} checked out, not {branch}"
    return None


def save_residue(tree: Path, digests: dict[str, str]) -> None:
    dest = Path(tree) / META_DIR / RESIDUE
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(dict(sorted(digests.items()))) + "\n")


def load_residue(tree: Path) -> dict[str, str | None]:
    """Path -> content digest of each pipeline leftover (None: recorded without its content)."""
    try:
        data = json.loads((Path(tree) / META_DIR / RESIDUE).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(data, list):
        return {str(p): None for p in data}
    return {str(p): (str(d) if d is not None else None) for p, d in data.items()} \
        if isinstance(data, dict) else {}


def previous_attempt(tree: Path, base_sha: str, adopted: list[str]) -> str | None:
    """The coder brief's account of what earlier runs left on the branch and in the tree."""
    parts = []
    log = gitops.git_try("log", "--oneline", f"{base_sha}..HEAD", cwd=tree) or ""
    if log.strip():
        parts.append("The branch already holds these commits from earlier runs of this task; "
                     "build on them:\n" + "\n".join(f"- {ln}" for ln in log.splitlines()))
    if adopted:
        parts.append("A run that died or was stopped left these uncommitted changes in the "
                     "worktree. They are part of your change now: finish, keep or revert them.\n"
                     + "\n".join(f"- {p}" for p in adopted))
    return "\n\n".join(parts) or None
