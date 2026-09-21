"""The git subprocess wrapper, worktree and lock locations, the common-dir check, and (re-exported
from porcelain.py) porcelain parsing and the allowed-paths guard."""
from __future__ import annotations

import hashlib
import subprocess
import sys

import os
from pathlib import Path, PurePosixPath

# Porcelain parsing + allowed-paths guard live in porcelain.py; re-exported
# here because worktree.py and the git-hardening tests import them from
# gitops, the module that produces the status text they consume.
from .porcelain import (  # noqa: F401
    _parse_porcelain, _stray_paths, _unquote_porcelain_path, _within_allowed)


class GitError(Exception):
    """Raised when a git() call exits non-zero and allow_fail was not set."""

    def __init__(self, cmd: tuple, returncode: int, stderr: str, cwd):
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr
        self.cwd = cwd
        super().__init__(str(self))

    def __str__(self) -> str:
        return (f"git {' '.join(self.cmd)} failed ({self.returncode}) "
                f"in {self.cwd}: {self.stderr}")


# The worktree's own metadata (identity, preserve marker) lives here, excluded from git.
META_DIR = ".ratchetloop"
_PROTECTED_ROOT = PurePosixPath(META_DIR)


def _is_protected(path: str) -> bool:
    pp = PurePosixPath(path)
    return pp == _PROTECTED_ROOT or _PROTECTED_ROOT in pp.parents


def _var_dir() -> Path:
    return Path.home() / "var" / "ratchetloop"


def _worktree_root() -> Path:
    raw = os.environ.get("RATCHETLOOP_WORKTREE_ROOT")
    return Path(raw).expanduser().resolve() if raw else _var_dir() / "worktrees"


def _repo_digest(repo: Path) -> str:
    """Collision-safe id for a repo path (same digest as the lock file)."""
    return hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:16]


def _lock_dir() -> Path:
    raw = os.environ.get("RATCHETLOOP_LOCK_DIR")
    return Path(raw).expanduser().resolve() if raw else _var_dir() / "locks"


def git_common_dir(tree: Path) -> Path:
    raw = git("rev-parse", "--git-common-dir", cwd=tree)
    p = Path(raw)
    return (Path(tree) / p).resolve() if not p.is_absolute() else p.resolve()


def common_dir_mismatch(tree: Path, repo: Path) -> str | None:
    """None if worktree --git-common-dir resolves to <repo>/.git."""
    got = git_common_dir(tree)
    want = (Path(repo) / ".git").resolve()
    if got != want:
        return f"git-common-dir mismatch: worktree {got} != repo {want}"
    return None


def unmerged_paths(tree: Path) -> str:
    return git("diff", "--name-only", "--diff-filter=U", cwd=tree, allow_fail=True) or ""


def git_try(*args: str, cwd: Path | None = None) -> str | None:
    """Output of a git command whose failure is an answer, not an error (None on a non-zero exit,
    without the warning `git(allow_fail=True)` prints)."""
    r = subprocess.run(["git", *args], capture_output=True, text=True, cwd=str(cwd), timeout=120)
    return r.stdout.rstrip("\n") if r.returncode == 0 else None


def git(*args: str, cwd: Path | None = None, allow_fail: bool = False) -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True,
                       cwd=str(cwd), timeout=120)
    if r.returncode != 0:
        stderr = r.stderr.strip()
        if allow_fail:
            print(f"WARNING: git {' '.join(args)} failed ({r.returncode}) "
                  f"in {cwd}: {stderr}", file=sys.stderr)
        else:
            raise GitError(args, r.returncode, stderr, cwd)
    # rstrip only: `status --porcelain` load-bearingly starts a line with
    # a space (` M path`). str.strip() ate that space and shifted the path
    # (` M README.md` → `M README.md` → pathspec `EADME.md`).
    return r.stdout.rstrip("\n")
