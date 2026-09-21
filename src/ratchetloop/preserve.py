"""Worktree identity, preservation and removal. A dirty or unexpected tree is preserved, never
force-removed; forced delete happens only through trees --discard of a preserved tree.

Carried from predecessor (item 65 fixes: provenance-gated discard, identity checks, no --force on the
ordinary path). Its database recording became a run event; its controller/STOP handling is gone
(sentinels live outside the worktree, CONTRACT.md §8).
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import gitops
from .events import EventSink
from .gitops import META_DIR, GitError, _repo_digest, _worktree_root

PRESERVED_NAME = "preserved.json"
IDENTITY_NAME = "identity.json"


class WorktreePreserved(Exception):
    def __init__(self, path: Path, reason: str):
        self.path = Path(path)
        self.reason = reason
        super().__init__(f"{self.path} {reason}")


def _meta_dir(tree: Path) -> Path:
    return Path(tree) / META_DIR


def _stored_base_commit(tree: Path) -> str:
    marker = _meta_dir(tree) / "base_commit"
    try:
        if marker.is_file():
            return marker.read_text().strip()
    except OSError:
        pass
    return ""


def _read_json_marker(tree: Path, name: str) -> dict:
    path = _meta_dir(tree) / name
    try:
        if path.is_file():
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _read_preserved(tree: Path) -> dict:
    return _read_json_marker(tree, PRESERVED_NAME)


def _read_identity(tree: Path) -> dict:
    data = _read_preserved(tree) or _read_json_marker(tree, IDENTITY_NAME)
    if not data.get("base_commit"):
        base = _stored_base_commit(tree)
        if base:
            data = {**data, "base_commit": base}
    return data


def _ensure_exclude(tree: Path, extra: str | None = None) -> None:
    exclude = Path(gitops.git("rev-parse", "--git-path", "info/exclude", cwd=tree))
    if not exclude.is_absolute():
        exclude = Path(tree) / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    ex = exclude.read_text() if exclude.exists() else ""
    needed = [f"{META_DIR}/"]
    if extra:
        needed.append(extra)
    out = ex
    for line in needed:
        if line not in out.splitlines() and line not in out:
            out = out + ("" if out.endswith("\n") or not out else "\n") + line + "\n"
    if out != ex:
        exclude.write_text(out)


def _write_identity(tree: Path, task_key: str, branch: str, base: str) -> None:
    d = _meta_dir(tree)
    d.mkdir(parents=True, exist_ok=True)
    (d / "base_commit").write_text(base + "\n")
    (d / IDENTITY_NAME).write_text(json.dumps({
        "task_key": task_key, "branch": branch, "base_commit": base,
    }) + "\n")


def _write_preserved(tree: Path, reason: str, *, task_key: str | None = None) -> bool:
    dest = _meta_dir(tree) / PRESERVED_NAME
    if dest.is_file():
        return False
    ident = _read_identity(tree)
    branch = ident.get("branch") or gitops.git(
        "rev-parse", "--abbrev-ref", "HEAD", cwd=tree, allow_fail=True) or ""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps({
        "task_key": task_key or ident.get("task_key") or Path(tree).name,
        "branch": branch,
        "base_commit": ident.get("base_commit") or _stored_base_commit(tree),
        "reason": reason,
        "at": datetime.now(timezone.utc).isoformat(),
    }) + "\n")
    return True


def _identity_mismatch(marker: dict, task_key: str, branch: str, base: str) -> str:
    parts = []
    if marker.get("task_key") != task_key:
        parts.append(f"task_key {marker.get('task_key')!r} != {task_key!r}")
    if marker.get("branch") != branch:
        parts.append(f"branch {marker.get('branch')!r} != {branch!r}")
    if marker.get("base_commit") != base:
        parts.append(f"base_commit {marker.get('base_commit')!r} != {base!r}")
    return "; ".join(parts)


def worktree_is_clean(repo: Path, tree: Path) -> tuple[bool, str]:
    """False with a reason when the tree still holds unrecorded work."""
    if tree is None or not Path(tree).exists():
        return True, ""
    tree = Path(tree)
    forced = (_read_preserved(tree).get("reason") or "")
    if "git-common-dir mismatch" in forced or forced.startswith("signal "):
        return False, forced
    try:
        porcelain = gitops.git(
            "status", "--porcelain", "--untracked-files=normal", cwd=tree)
    except GitError as e:
        return False, f"git status failed: {e}"
    if porcelain.strip():
        return False, f"uncommitted changes: {porcelain.splitlines()[0]}"
    try:
        head = gitops.git("rev-parse", "--abbrev-ref", "HEAD", cwd=tree)
    except GitError as e:
        return False, f"git inspect failed: {e}"
    if not head or head == "HEAD":
        return False, "detached HEAD"
    ident = _read_identity(tree)
    expected_branch = ident.get("branch")
    if not expected_branch:
        return False, "no expected task branch"
    if head != expected_branch:
        return False, f"HEAD is {head}, expected {expected_branch}"
    expected_base = ident.get("base_commit") or ""
    stored = _stored_base_commit(tree) or expected_base
    if not stored:
        return False, "missing stored base_commit"
    if expected_base and stored != expected_base:
        return False, f"base_commit {stored} != expected {expected_base}"
    try:
        ahead = gitops.git("rev-list", "--count", f"{stored}..HEAD", cwd=tree)
        tracked = gitops.git("diff", "--name-only", "HEAD", cwd=tree)
        staged = gitops.git("diff", "--cached", "--name-only", cwd=tree)
    except GitError as e:
        return False, f"git inspect failed: {e}"
    if ((ahead or "").strip() == "0"
            and ((tracked or "").strip() or (staged or "").strip())):
        return False, "no commits beyond base; tracked files differ from HEAD"
    return True, ""


def _worktree_registered(repo: Path, path: Path) -> bool:
    listing = gitops.git("worktree", "list", "--porcelain", cwd=repo, allow_fail=True)
    abs_path = str(path.resolve())
    for line in listing.splitlines():
        if line.startswith("worktree ") and line.split(" ", 1)[1] == abs_path:
            return True
    return False


def _remove_worktree(repo: Path, path: Path, force: bool = False) -> None:
    if path is None:
        return
    registered = _worktree_registered(repo, path)
    if not path.exists() and not registered:
        return
    if not force:
        clean, reason = worktree_is_clean(repo, path)
        if not clean:
            raise WorktreePreserved(path, reason)
        try:
            gitops.git("worktree", "remove", str(path), cwd=repo)
        except GitError as e:
            raise WorktreePreserved(path, str(e)) from e
        return
    try:
        gitops.git("worktree", "remove", "--force", str(path), cwd=repo)
    except GitError as e:
        print(f"WARNING: git worktree remove failed ({e}); rmtree + prune",
              file=sys.stderr)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        gitops.git("worktree", "prune", cwd=repo, allow_fail=True)


def _remove_or_preserve(repo: Path, path: Path, *, sink: EventSink | None = None,
                        task_key: str | None = None) -> str:
    """Remove a clean worktree, or preserve a dirty one; emits a worktree_end event."""
    try:
        _remove_worktree(repo, path)
        if sink is not None:
            sink.emit("worktree_end", path=str(path), disposition="removed")
        return "removed"
    except WorktreePreserved as e:
        _write_preserved(e.path, e.reason, task_key=task_key)
        print(f"WORKTREE_PRESERVED {e.path} {e.reason}", file=sys.stderr)
        if sink is not None:
            sink.emit("worktree_end", path=str(e.path), disposition="preserved",
                      reason=e.reason)
        return f"preserved {e.reason}"


def _add_worktree(repo: Path, path: Path, branch: str, base_branch: str,
                  revise_from: bool) -> None:
    """Create a worktree OUTSIDE the live tree. Never checks out `repo`.
    Never `add -B`: that resets an existing task branch to base_branch."""
    path.parent.mkdir(parents=True, exist_ok=True)
    task_key = path.name
    base_commit = gitops.git("rev-parse", base_branch, cwd=repo)
    if path.exists() or _worktree_registered(repo, path):
        marker = _read_preserved(path) if path.exists() else {}
        if marker:
            mismatch = _identity_mismatch(marker, task_key, branch, base_commit)
            if mismatch:
                raise SystemExit(
                    f"refusing to start: preserved tree mismatch {path}: "
                    f"{mismatch}")
        if path.exists():
            clean, reason = worktree_is_clean(repo, path)
            if not clean:
                if not marker:
                    raise SystemExit(
                        f"refusing to start: existing dirty tree without "
                        f"preserve marker {path}: {reason}")
                return
        try:
            _remove_worktree(repo, path)
        except WorktreePreserved as e:
            if marker:
                return
            raise SystemExit(f"refusing to start: {e}") from e
    if revise_from:
        gitops.git("worktree", "add", str(path), branch, cwd=repo)
    else:
        gitops.git("worktree", "add", "-b", branch, str(path), base_branch, cwd=repo)
    _ensure_exclude(path)
    _write_identity(path, task_key, branch, base_commit)
    dirty = gitops.git("status", "--porcelain", cwd=path)
    if dirty:
        raise SystemExit(
            f"refusing to start: worktree dirty at creation\n{path}\n{dirty}")


def _sweep_orphans(repo: Path, is_finished: Callable[[str], bool]) -> None:
    """Remove clean worktrees of this repo whose task has finished; preserve dirty ones.
    `is_finished(task_key)` comes from the run record (Phase 4), not from a database."""
    root = _worktree_root() / _repo_digest(repo)
    if not root.is_dir():
        return
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not is_finished(child.name):
            continue
        clean, reason = worktree_is_clean(repo, child)
        if not clean:
            print(f"ORPHAN_PRESERVED {child} {reason}", file=sys.stderr)
            continue
        print(f"orphan sweep: removing {child} (task {child.name} finished)",
              file=sys.stderr)
        try:
            _remove_worktree(repo, child)
        except WorktreePreserved as e:
            print(f"ORPHAN_PRESERVED {e.path} {e.reason}", file=sys.stderr)
