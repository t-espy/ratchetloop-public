"""Per-task worktree lifecycle: path, repo lock, create (via preserve), stage, commit, heal.

Patch contract (carried from predecessor, 2026-08-28): tests substitute ``gitops.git`` on its owner
module, so callers always reach it as ``gitops.git``, never as a bare imported name.
"""
from __future__ import annotations

import fcntl
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

from . import gitops
from .gitops import (
    META_DIR, _is_protected, _lock_dir, _parse_porcelain, _repo_digest, _stray_paths,
    _worktree_root,
)
from .preserve import (  # noqa: F401 — re-exported for callers and tests
    WorktreePreserved, _add_worktree, _ensure_exclude, _remove_or_preserve,
    _remove_worktree, _sweep_orphans, worktree_is_clean,
)

LOCK_WAIT_S = 60.0
_STAGE_CHUNK = 100


def _worktree_path(repo: Path, task_key: str) -> Path:
    # Include a digest of the resolved repo so two checkouts named "repo"
    # cannot share a worktree (codex #440 finding 5).
    return _worktree_root() / _repo_digest(repo) / task_key


def _repo_lock_path(repo: Path) -> Path:
    return _lock_dir() / f"{_repo_digest(repo)}.lock"


def _acquire_repo_lock(repo: Path):
    """Exclusive non-blocking flock. Returns an open file object, or None if another run already
    holds the lock for this repo."""
    path = _repo_lock_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


def _release_repo_lock(fh) -> None:
    if fh is None:
        return
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


@contextmanager
def repo_lock(repo: Path, wait_s: float = LOCK_WAIT_S) -> Iterator[None]:
    """Hold the repo's lock around one git mutation (worktree add or remove, commit). Separate
    worktrees of one repo may run in parallel; only their shared .git is serialized (D16)."""
    deadline = time.monotonic() + wait_s
    while True:
        fh = _acquire_repo_lock(repo)
        if fh is not None:
            break
        if time.monotonic() >= deadline:
            raise TimeoutError(f"repo lock held for more than {wait_s:g}s: {_repo_lock_path(repo)}")
        time.sleep(0.1)
    try:
        yield
    finally:
        _release_repo_lock(fh)


def _is_worker_artefact(path: str) -> bool:
    """The worktree's own metadata never goes into a commit. (predecessor's version used
    `lstrip("./")`, which also stripped the metadata directory's leading dot.)"""
    p = path.replace("\\", "/")
    p = p[2:] if p.startswith("./") else p
    p = p.rstrip("/")
    return p == META_DIR or p.startswith(META_DIR + "/")


def status_porcelain(repo: Path) -> str:
    """Every changed path, untracked files listed singly and a rename as its delete plus its add
    (`--no-renames`), so neither side of a rename can drop out of scope or staging."""
    return gitops.git("status", "--porcelain", "--untracked-files=all", "--no-renames", cwd=repo)


def dirty_paths(repo: Path) -> frozenset[str]:
    return frozenset(p for _, p in _parse_porcelain(status_porcelain(repo)))


def _stage_changes(repo: Path, allowed_paths: Iterable[str] | None,
                   exclude: Iterable[str] = ()) -> str | None:
    """Stage exactly the changed paths git reports, never `git add -A` over the tree
    (predecessor b116e90: -A committed a venv symlink). Paths in `exclude` (setup outputs) and the
    worktree metadata stay out. With allowed_paths, any other change fails the run instead of being
    staged. Returns a FAILED reason, or None once everything is staged."""
    skip = set(exclude)
    status = status_porcelain(repo)
    if allowed_paths is not None:
        strays = [s for s in _stray_paths(status, list(allowed_paths))
                  if s not in skip and not _is_worker_artefact(s)]
        if strays:
            return "stray changes outside allowed_paths: " + ", ".join(strays)
    stage_paths(repo, (p for _, p in _parse_porcelain(status) if p not in skip))
    return None


def stage_paths(repo: Path, paths: Iterable[str]) -> None:
    """Stage exactly these paths — in the loop, the scope check's `changed`, taken before the
    checks ran — so whatever a check leaves behind (a `__pycache__`, a build output) is not the
    worker's change and stays out of the commit (Phase 2 flow test, 2026-09-11). The index is
    first reset to HEAD: anything the worker staged itself is dropped, or the commit, which takes
    the whole index, would ship it (Phase 2 review, finding 1)."""
    gitops.git("reset", "-q", cwd=repo)
    todo = sorted({p for p in paths if not _is_worker_artefact(p)})
    for i in range(0, len(todo), _STAGE_CHUNK):
        # `git add <path>` records additions, edits and deletions of exactly those paths.
        gitops.git("add", "--", *todo[i:i + _STAGE_CHUNK], cwd=repo)


def _maybe_commit(repo: Path, message: str) -> bool:
    """Commit staged changes. Returns False if nothing was staged (a valid no-op); any other commit
    failure (a pre-commit hook rejection, an index lock) raises GitError rather than being
    swallowed, or the loop would check and review an edit that never reached HEAD. Hooks are
    honoured: a target's hooks are its own governance; a hook a worker planted is caught by the
    scope check before any commit."""
    staged = gitops.git("diff", "--cached", "--name-only", cwd=repo)
    if not staged.strip():
        return False
    gitops.git("commit", "-m", message, cwd=repo)
    return True


def _note_if_base_moved(repo: Path, base_branch: str, base: str, branch: str) -> str | None:
    """The new base sha if `base_branch` moved during the run, else None (read-only: the live
    checkout is never moved). A moved base makes the review diff stale until rebased."""
    moved_to = gitops.git("rev-parse", base_branch, cwd=repo, allow_fail=True)
    if moved_to and moved_to != base:
        print(f"WARNING: {base_branch} moved during this run ({base[:8]} -> {moved_to[:8]}); "
              f"rebase {branch} onto {base_branch} before merging", file=sys.stderr)
        return moved_to
    return None


def _finish_worktree(repo: Path, disposition: str, start_snapshot: str = "") -> list[str]:
    """Scoped self-healing at exit (operator ruling, 2026-08-26): anything dirty at exit that was
    not dirty at start and is not protected (the worktree metadata) is this run's own residue.
    Only single-path actions from porcelain output: tracked and modified -> restore from HEAD;
    untracked file -> remove it. Never `git reset --hard`, never `git clean -fd`, no recursion into
    untracked directories. Best-effort: never raises, never changes the caller's disposition."""
    healed: list[str] = []
    try:
        exit_status = gitops.git("status", "--porcelain", cwd=repo, allow_fail=True)
        if not exit_status:
            return healed
        start_paths = {p for _, p in _parse_porcelain(start_snapshot)}
        eligible = [(code, path) for code, path in _parse_porcelain(exit_status)
                    if path not in start_paths and not _is_protected(path)]
        if not eligible:
            print(f"WARNING: working tree left dirty on {disposition} exit "
                  f"(present at start or protected — left for a human):\n"
                  f"{exit_status}", file=sys.stderr)
            return healed
        for code, path in eligible:
            try:
                if code.startswith("??"):
                    target = repo / path
                    if target.is_dir() and not target.is_symlink():
                        print(f"SELF-HEAL SKIPPED (untracked directory, no "
                              f"recursion): {path}", file=sys.stderr)
                        continue
                    target.unlink()
                else:
                    gitops.git("checkout", "HEAD", "--", path, cwd=repo)
                healed.append(path)
                print(f"SELF-HEAL: {path}", file=sys.stderr)
            except Exception as e:  # noqa: BLE001 — one path failing must not stop the rest
                print(f"SELF-HEAL FAILED for {path}: {type(e).__name__}: {e}",
                      file=sys.stderr)
        remaining = gitops.git("status", "--porcelain", cwd=repo, allow_fail=True)
        if remaining:
            print(f"WARNING: working tree still dirty on {disposition} exit "
                  f"after self-heal:\n{remaining}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 — best-effort; never raises, never touches disposition
        print(f"SELF-HEAL aborted ({type(e).__name__}: {e}); tree left as-is "
              f"for the next run's start-of-run guard", file=sys.stderr)
    return healed
