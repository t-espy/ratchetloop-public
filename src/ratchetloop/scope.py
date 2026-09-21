"""What a worker changed, and whether it stayed in bounds (CONTRACT.md §3.1 `scope`,
FAILURE_MODES.md §2).

A worker may change files in its worktree and nothing else: not HEAD or the branch it points to, not
the worktree's link to its repository, not the repository's git config, hooks or exclude file (a hook
planted there would run inside the pipeline's own commit; an exclude entry would hide files from
`git status`). `snapshot()` before a launch, `check()` after it; `violation()` names the breach that
fails the run; `git_state_changed()` is re-run under the repo lock just before the pipeline commits,
because config and hooks are shared with sibling runs. A path already dirty before the launch (a
setup output, check residue) is not the worker's unless the worker changed its content.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import gitops
from .gitops import _parse_porcelain, _stray_paths
from .worktree import _is_worker_artefact, status_porcelain


@dataclass(frozen=True)
class Snapshot:
    head: str
    branch: str
    git_state: str
    status: str
    dirty: frozenset[str]
    # Content of each dirty path. Membership alone let a worker rewrite check residue outside
    # allowed_paths unseen and unstaged (Phase 3 review, finding 3).
    digests: dict[str, str] = field(default_factory=dict, hash=False, compare=False)


def _digest(path: Path) -> str:
    try:
        if path.is_symlink():
            return "link:" + str(path.readlink())
        if path.is_file():
            return "file:" + hashlib.sha256(path.read_bytes()).hexdigest()
        return "dir" if path.is_dir() else "absent"
    except OSError as e:
        return f"unreadable:{e.errno}"


def _hash_file(path: Path, h: "hashlib._Hash") -> None:
    h.update(str(path.name).encode() + b"\0")
    h.update(path.read_bytes() if path.is_file() else b"<absent>")


def _hash_dir(path: Path, h: "hashlib._Hash") -> None:
    if not path.is_dir():
        return
    for p in sorted(path.rglob("*")):
        if p.is_file() or p.is_symlink():
            h.update(str(p.relative_to(path)).encode() + b"\0")
            h.update(p.read_bytes() if p.is_file() else str(p.readlink()).encode())


def _git_path(tree: Path, raw: str) -> Path:
    p = Path(raw)
    return p if p.is_absolute() else (Path(tree) / p).resolve()


def _git_state(tree: Path) -> str:
    """Hash of the worktree's .git link and private git dir, and the shared repository's config,
    hooks and exclude file."""
    tree = Path(tree)
    common = gitops.git_common_dir(tree)
    private = _git_path(tree, gitops.git("rev-parse", "--git-dir", cwd=tree))
    h = hashlib.sha256()
    link = tree / ".git"
    h.update(link.read_bytes() if link.is_file() else b"<dir>")
    for path in (common / "config", common / "info" / "exclude", private / "commondir",
                 private / "gitdir", private / "config.worktree"):
        _hash_file(path, h)
    _hash_dir(common / "hooks", h)
    return h.hexdigest()


def snapshot(tree: Path) -> Snapshot:
    status = status_porcelain(tree)
    dirty = frozenset(p for _, p in _parse_porcelain(status))
    return Snapshot(
        head=gitops.git("rev-parse", "HEAD", cwd=tree),
        branch=gitops.git("symbolic-ref", "-q", "HEAD", cwd=tree, allow_fail=True),
        git_state=_git_state(tree), status=status, dirty=dirty,
        digests={p: _digest(Path(tree) / p) for p in dirty})


def git_state_changed(tree: Path, before: Snapshot) -> bool:
    """Re-check the git state just before committing, under the repo lock."""
    return _git_state(tree) != before.git_state


def check(tree: Path, before: Snapshot,
          allowed_paths: Iterable[str] | None = None) -> dict[str, Any]:
    """The `scope` event's fields for what changed since `before`."""
    after = snapshot(tree)

    def mine(path: str) -> bool:
        if _is_worker_artefact(path):
            return False
        return path not in before.dirty or after.digests.get(path) != before.digests.get(path)

    outside: list[str] = []
    if allowed_paths is not None:
        outside = sorted({s for s in _stray_paths(after.status, list(allowed_paths)) if mine(s)})
    return {
        "changed": sorted(p for p in after.dirty if mine(p)),
        "outside_allowed": outside,
        "head_moved": (after.head, after.branch) != (before.head, before.branch),
        "git_dir_touched": after.git_state != before.git_state,
    }


def violation(scope: dict[str, Any]) -> str | None:
    """The reason for a `scope_violation` (CONTRACT.md §6), or None if the worker stayed in bounds."""
    if scope["head_moved"]:
        return "worker moved HEAD (committed, reset or switched branch)"
    if scope["git_dir_touched"]:
        return "worker changed the repository's git link, config, hooks or exclude file"
    if scope["outside_allowed"]:
        return "changes outside allowed_paths: " + ", ".join(scope["outside_allowed"])
    return None
