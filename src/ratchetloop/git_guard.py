"""A worktree must belong to the repository it claims: its git common dir must be `<repo>/.git`.

A mismatch marks the tree preserved, so it is never deleted as a stranger's, and fails the run.
Carried from predecessor's kernel guard; its `handoff:` step (merge, cherry-pick or patch another
branch into the worktree) is not part of ratchetloop's task contract and was left behind.
"""
from __future__ import annotations

from pathlib import Path

from .gitops import common_dir_mismatch
from .preserve import _write_preserved


def common_dir_guard(tree: Path, repo: Path, task_key: str | None = None) -> str | None:
    """None when the worktree's common dir is the repo's .git; otherwise the FAILED note."""
    note = common_dir_mismatch(Path(tree), Path(repo))
    if not note:
        return None
    try:
        _write_preserved(Path(tree), note, task_key=task_key)
    except Exception:  # noqa: BLE001 — the run fails either way; the note is what matters
        pass
    return note
