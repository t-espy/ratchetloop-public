"""Buildloop item 65: never delete a worktree that still holds unrecorded work; discard only a
preserved tree, only inside the worktree root, only with --yes. Kernel-driven cases (commit failure
preserving a run's tree, resume reusing it) return with ratchetloop's loop in Phases 3 and 4."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from ratchetloop import gitops
from ratchetloop.events import EventSink, read_events
from ratchetloop.gitops import GitError
from ratchetloop.preserve import PRESERVED_NAME, _remove_or_preserve
from ratchetloop.trees import discard_tree, list_preserved_trees
from ratchetloop.worktree import (
    WorktreePreserved, _add_worktree, _remove_worktree, _sweep_orphans, _worktree_path,
    worktree_is_clean,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=r, check=True)
    (r / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=r, check=True)
    return r


def _force_remove(repo: Path, tree: Path) -> None:
    try:
        _remove_worktree(repo, tree, force=True)
    except Exception:
        pass


def _tree(repo: Path, key: str) -> Path:
    wt = _worktree_path(repo, key)
    _add_worktree(repo, wt, f"ratchetloop/{key}", "master", False)
    return wt


def test_worktree_is_clean_false_when_porcelain_dirty(repo):
    wt = _tree(repo, "dirty-check")
    (wt / "leftover.py").write_text("keep\n")
    clean, reason = worktree_is_clean(repo, wt)
    assert clean is False and "leftover.py" in reason
    with pytest.raises(WorktreePreserved) as exc:
        _remove_worktree(repo, wt)
    assert exc.value.path == wt
    assert (wt / "leftover.py").read_text() == "keep\n"


def test_remove_force_deletes_dirty_tree(repo):
    wt = _tree(repo, "force-me")
    (wt / "x.py").write_text("x\n")
    _remove_worktree(repo, wt, force=True)
    assert not wt.exists()


def test_remove_or_preserve_records_a_worktree_end_event(repo, tmp_path):
    sink = EventSink(tmp_path / "events.jsonl", "r1")
    clean = _tree(repo, "clean-one")
    assert _remove_or_preserve(repo, clean, sink=sink, task_key="clean-one") == "removed"
    dirty = _tree(repo, "dirty-one")
    (dirty / "wip.py").write_text("wip\n")
    disp = _remove_or_preserve(repo, dirty, sink=sink, task_key="dirty-one")
    assert disp.startswith("preserved") and dirty.exists()
    assert (dirty / ".ratchetloop" / PRESERVED_NAME).is_file()
    removed, preserved = read_events(tmp_path / "events.jsonl")
    assert (removed["event"], removed["disposition"]) == ("worktree_end", "removed")
    assert preserved["disposition"] == "preserved" and "wip.py" in preserved["reason"]


def test_orphan_sweep_skips_dirty(repo, capsys):
    orphan = _tree(repo, "dirty-orphan")
    (orphan / "leftover.py").write_text("keep me\n")
    _sweep_orphans(repo, lambda key: True)
    assert (orphan / "leftover.py").read_text() == "keep me\n"
    assert "ORPHAN_PRESERVED" in capsys.readouterr().err


def test_trees_lists_and_discard_requires_a_preserved_tree_and_yes(repo, capsys):
    wt = _tree(repo, "listed-dirty")
    (wt / "leftover.py").write_text("keep\n")
    rows = list_preserved_trees()
    assert any(r["path"] == str(wt) and "leftover.py" in r["reason"] for r in rows)
    assert discard_tree(wt, yes=True) == 2
    assert "preserved" in capsys.readouterr().err
    _remove_or_preserve(repo, wt, task_key="listed-dirty")
    assert discard_tree(wt, yes=False) == 2
    assert "without --yes" in capsys.readouterr().err
    assert (wt / "leftover.py").is_file()
    assert discard_tree(wt, yes=True) == 0
    assert not wt.exists()


def test_discard_refuses_worktree_root(capsys):
    root = Path(os.environ["RATCHETLOOP_WORKTREE_ROOT"])
    root.mkdir(parents=True, exist_ok=True)
    assert discard_tree(root, yes=True) == 2
    assert "worktree root" in capsys.readouterr().err
    assert root.exists()


def test_discard_refuses_a_tree_ratchetloop_did_not_preserve(capsys):
    stranger = Path(os.environ["RATCHETLOOP_WORKTREE_ROOT"]) / "someone-elses-tree"
    stranger.mkdir(parents=True)
    (stranger / "notes.txt").write_text("do not delete\n")
    assert discard_tree(stranger, yes=True) == 2
    assert "preserved" in capsys.readouterr().err
    assert (stranger / "notes.txt").read_text() == "do not delete\n"


def test_discard_refuses_status_failure_even_with_yes(repo, monkeypatch, capsys):
    wt = _tree(repo, "status-fail")
    (wt / ".ratchetloop" / PRESERVED_NAME).write_text(json.dumps({
        "task_key": "status-fail", "branch": "ratchetloop/status-fail",
        "base_commit": "x", "reason": "t", "at": "t",
    }) + "\n")
    real = gitops.git

    def boom(*args, **kwargs):
        if args and args[0] == "status":
            raise GitError(("status",), 128, "boom", kwargs.get("cwd"))
        return real(*args, **kwargs)

    monkeypatch.setattr(gitops, "git", boom)
    assert discard_tree(wt, yes=True) == 2
    assert "git status failed" in capsys.readouterr().err
    assert wt.exists()


def test_worktree_is_clean_false_when_detached(repo):
    wt = _tree(repo, "detach-me")
    subprocess.run(["git", "checkout", "--detach", "-q"], cwd=wt, check=True)
    clean, reason = worktree_is_clean(repo, wt)
    assert clean is False and "detached" in reason
    with pytest.raises(WorktreePreserved):
        _remove_worktree(repo, wt)


def test_worktree_is_clean_false_when_wrong_branch(repo):
    wt = _tree(repo, "wrong-branch")
    ident_path = wt / ".ratchetloop" / "identity.json"
    ident = json.loads(ident_path.read_text())
    ident["branch"] = "ratchetloop/other"
    ident_path.write_text(json.dumps(ident) + "\n")
    clean, reason = worktree_is_clean(repo, wt)
    assert clean is False and "expected ratchetloop/other" in reason


def test_worktree_is_clean_false_when_base_changed(repo):
    wt = _tree(repo, "base-moved")
    (wt / ".ratchetloop" / "base_commit").write_text("0" * 40 + "\n")
    clean, reason = worktree_is_clean(repo, wt)
    assert clean is False and "base_commit" in reason


@pytest.mark.parametrize("field,value", [
    ("task_key", "other-key"), ("branch", "ratchetloop/other"), ("base_commit", "0" * 40),
])
def test_add_refuses_preserved_mismatch(repo, field, value):
    key = f"mis-{field}"
    wt = _tree(repo, key)
    data = {"task_key": key, "branch": f"ratchetloop/{key}",
            "base_commit": gitops.git("rev-parse", "master", cwd=repo), "reason": "t", "at": "t"}
    data[field] = value
    (wt / ".ratchetloop" / PRESERVED_NAME).write_text(json.dumps(data) + "\n")
    with pytest.raises(SystemExit) as exc:
        _add_worktree(repo, wt, f"ratchetloop/{key}", "master", False)
    assert "mismatch" in str(exc.value) and field in str(exc.value)
    assert wt.exists()


def test_dirty_between_check_and_remove_survives(repo, monkeypatch):
    wt = _tree(repo, "race-dirty")
    real = gitops.git

    def wrap(*args, **kwargs):
        if args[:2] == ("worktree", "remove") and "--force" not in args:
            (wt / "raced.py").write_text("sneak\n")
        return real(*args, **kwargs)

    monkeypatch.setattr(gitops, "git", wrap)
    with pytest.raises(WorktreePreserved):
        _remove_worktree(repo, wt)
    assert (wt / "raced.py").read_text() == "sneak\n"


def test_dirty_at_checkout_leaves_tree(repo, monkeypatch):
    wt = _worktree_path(repo, "dirty-create")
    real = gitops.git

    def wrap(*args, **kwargs):
        out = real(*args, **kwargs)
        if args[:2] == ("worktree", "add"):
            (wt / "leftover.py").write_text("keep\n")
        return out

    monkeypatch.setattr(gitops, "git", wrap)
    with pytest.raises(SystemExit) as exc:
        _add_worktree(repo, wt, "ratchetloop/dirty-create", "master", False)
    assert (wt / "leftover.py").read_text() == "keep\n"
    assert str(wt) in str(exc.value) and "leftover.py" in str(exc.value)
    monkeypatch.setattr(gitops, "git", real)
    _force_remove(repo, wt)
