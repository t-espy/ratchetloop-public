"""Setup commands and deterministic checks (CONTRACT.md §2, D20).

Both run in the worktree with an explicit environment; exit codes decide; the full output goes to
a log. An empty check list never passes (predecessor item 109: empty checks passed vacuously). A
command that runs past its timeout has its whole process group killed.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable

from .signals import set_active_pgid
from .worker_proc import _reap_pgid, worker_env

_KILL_SIGNALS = {signal.SIGTERM, signal.SIGINT}

DEFAULT_TIMEOUT_S = 600.0
# Only these pass through from the caller; anything else a check needs comes from the task's `env`.
# The caller's variables leaked into a target's suite in predecessor (dev item 3, 2026-09-11).
PASS_THROUGH = ("HOME", "USER", "LOGNAME", "LANG", "TERM", "TMPDIR", "SHELL", "TZ")


def check_env(task_env: dict[str, str] | None = None, *, base: dict[str, str] | None = None,
              gh_dir: str | None = None) -> dict[str, str]:
    """The environment setup and checks run in: a short allow-list from the caller, a PATH without
    ratchetloop's own virtualenv, the task's `env`, and the same git containment workers get (no
    push transports, no GitHub credentials)."""
    src = os.environ if base is None else base
    env = {k: v for k, v in src.items() if k in PASS_THROUGH or k.startswith("LC_")}
    own_bin = str(Path(sys.prefix) / "bin")
    env["PATH"] = os.pathsep.join(
        p for p in src.get("PATH", "").split(os.pathsep) if p and p != own_bin)
    env = worker_env(env, gh_dir=gh_dir)
    for key in ("PYTHONSAFEPATH", "LANGSMITH_TRACING"):
        env.pop(key, None)
    env.update(task_env or {})
    return env


def checks_cwd(tree: Path, relative: str) -> Path:
    """The task's checks_cwd inside the worktree; a symlink out of it is refused."""
    root = Path(tree).resolve()
    cwd = (root / relative).resolve()
    if cwd != root and root not in cwd.parents:
        raise ValueError(f"checks_cwd {relative!r} leaves the worktree")
    return cwd


_DRAIN_S = 10.0


def _child_unblock() -> None:
    signal.pthread_sigmask(signal.SIG_UNBLOCK, _KILL_SIGNALS)


def _run_one(cmd: str, cwd: Path, env: dict[str, str], timeout: float,
             pidfile: Path | None = None) -> tuple[int, str]:
    # The command's process group is recorded — in `pidfile` for a later run to reap after a
    # SIGKILL, as the active group for a SIGTERM — before a kill signal can land (as in
    # worker_run). Unrecorded, a pipeline killed during checks orphaned them (Phase 4).
    prev = signal.pthread_sigmask(signal.SIG_BLOCK, _KILL_SIGNALS)
    try:
        # pipefail: `pytest | tee log` must be red when pytest is (Phase 2 review, finding 4).
        proc = subprocess.Popen(
            ["bash", "-o", "pipefail", "-c", cmd], cwd=str(cwd), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True, preexec_fn=_child_unblock)
        if pidfile is not None:
            Path(pidfile).write_text(str(proc.pid))
        set_active_pgid(proc.pid)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, prev)
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out or ""
    except subprocess.TimeoutExpired as expired:
        _reap_pgid(proc.pid)
        try:
            out, _ = proc.communicate(timeout=_DRAIN_S)
        except subprocess.TimeoutExpired:
            # A member that escaped the group can hold the pipe open; never wait on it forever.
            proc.stdout.close()
            out = expired.output if isinstance(expired.output, str) else ""
        return 124, (out or "") + f"\n[timed out after {timeout:g}s; process group killed]\n"
    finally:
        set_active_pgid(None)
        if pidfile is not None:
            Path(pidfile).unlink(missing_ok=True)


def run_commands(commands: Iterable[str], cwd: Path, env: dict[str, str], *,
                 timeout: float = DEFAULT_TIMEOUT_S, log: Path | None = None,
                 stop_on_failure: bool = False, pidfile: Path | None = None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for cmd in commands:
        t0 = time.monotonic()
        code, out = _run_one(cmd, Path(cwd), env, timeout, pidfile)
        results.append({"cmd": cmd, "exit": code, "wall_s": round(time.monotonic() - t0, 3),
                        "tail": out[-800:]})
        if log is not None:
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(f"$ {cmd}\n{out}\n[exit {code}]\n")
        if stop_on_failure and code != 0:
            break
    return results


def run_checks(checks: Iterable[str], cwd: Path, env: dict[str, str],
               **kw: Any) -> tuple[bool, list[dict[str, Any]]]:
    """True only if there is at least one check and every one exited 0."""
    checks = list(checks)
    if not checks:
        return False, []
    results = run_commands(checks, cwd, env, **kw)
    return all(r["exit"] == 0 for r in results), results


def run_setup(commands: Iterable[str], cwd: Path, env: dict[str, str],
              **kw: Any) -> tuple[bool, list[dict[str, Any]]]:
    """Setup commands (e.g. link a venv) run before the coder; the first failure stops them."""
    results = run_commands(commands, cwd, env, stop_on_failure=True, **kw)
    return all(r["exit"] == 0 for r in results), results
