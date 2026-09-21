"""Keep every test away from the operator's real policy file and environment."""
import pytest


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    monkeypatch.delenv("RATCHETLOOP_POLICY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("WORKER_SILENCE_S", raising=False)
    monkeypatch.setenv("RATCHETLOOP_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    monkeypatch.setenv("RATCHETLOOP_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("RATCHETLOOP_RUNS_ROOT", str(tmp_path / "runs"))
