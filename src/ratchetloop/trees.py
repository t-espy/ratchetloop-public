"""List and discard preserved worktrees. Discard is the only forced removal."""
from __future__ import annotations

import sys
import time
from pathlib import Path

from ratchetloop import gitops
from ratchetloop.gitops import META_DIR, GitError, _worktree_root
from ratchetloop.preserve import PRESERVED_NAME, _remove_worktree, worktree_is_clean


def _age_label(mtime: float) -> str:
    s = max(0, int(time.time() - mtime))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _repo_from_tree(tree: Path) -> Path:
    raw = gitops.git("rev-parse", "--git-common-dir", cwd=tree)
    p = Path(raw)
    if not p.is_absolute():
        p = (tree / p).resolve()
    else:
        p = p.resolve()
    return p.parent if p.name == ".git" else p


def list_preserved_trees(root: Path | None = None) -> list[dict]:
    root = Path(root) if root is not None else _worktree_root()
    rows: list[dict] = []
    if not root.is_dir():
        return rows
    for digest_dir in sorted(root.iterdir()):
        if not digest_dir.is_dir():
            continue
        for child in sorted(digest_dir.iterdir()):
            if not child.is_dir():
                continue
            try:
                repo = _repo_from_tree(child)
            except GitError:
                repo = child
            clean, reason = worktree_is_clean(repo, child)
            if clean:
                continue
            branch = gitops.git(
                "rev-parse", "--abbrev-ref", "HEAD", cwd=child, allow_fail=True,
            ) or ""
            try:
                mtime = child.stat().st_mtime
            except OSError:
                mtime = time.time()
            rows.append({
                "path": str(child),
                "task_key": child.name,
                "branch": branch,
                "age": _age_label(mtime),
                "reason": reason,
            })
    return rows


def discard_tree(path: Path, *, yes: bool) -> int:
    path = Path(path).resolve()
    root = _worktree_root().resolve()
    if path == root:
        print(f"refusing discard of worktree root {root}", file=sys.stderr)
        return 2
    if root not in path.parents:
        print(f"refusing discard outside worktree root {root}", file=sys.stderr)
        return 2
    marker = path / META_DIR / PRESERVED_NAME
    if not marker.is_file():
        print(f"refusing discard: not a ratchetloop-preserved tree "
              f"(missing {marker})", file=sys.stderr)
        return 2
    try:
        porcelain = gitops.git(
            "status", "--porcelain", "--untracked-files=normal", cwd=path)
    except GitError as e:
        print(f"git status failed: {e}", file=sys.stderr)
        print("refusing discard: git status failed", file=sys.stderr)
        return 2
    print(porcelain)
    if not yes:
        print("refusing --discard without --yes", file=sys.stderr)
        return 2
    try:
        repo = _repo_from_tree(path)
    except GitError:
        repo = path
    _remove_worktree(repo, path, force=True)
    return 0


def cmd_trees(args) -> int:
    if args.discard:
        return discard_tree(Path(args.discard), yes=bool(args.yes))
    for row in list_preserved_trees():
        print(f"{row['path']}\t{row['task_key']}\t{row['branch']}\t"
              f"{row['age']}\t{row['reason']}")
    return 0
