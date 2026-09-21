"""Process group kill and non-blocking stdout/stderr line reads."""
from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from ratchetloop.runtime import safepath_env

MAX_LINE_BYTES = 1024 * 1024
_READ_CHUNK = 8192
_POLL_S = 0.05
_TERM_S = 2.0
_KILL_S = 2.0


class ProtocolLineError(Exception):
    """A stdout/stderr line exceeded MAX_LINE_BYTES."""


def worker_env(base: dict[str, str] | None = None, *, gh_dir: str | None = None) -> dict[str, str]:
    """Containment env (Amendment B1). protocol.allow=never blocks push transports."""
    env = safepath_env(base if base is not None else os.environ)
    env["GIT_CONFIG_COUNT"] = "3"
    env["GIT_CONFIG_KEY_0"] = "protocol.allow"
    env["GIT_CONFIG_VALUE_0"] = "never"
    env["GIT_CONFIG_KEY_1"] = "credential.helper"
    env["GIT_CONFIG_VALUE_1"] = ""
    env["GIT_CONFIG_KEY_2"] = "remote.origin.pushurl"
    env["GIT_CONFIG_VALUE_2"] = "DISABLED"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    if gh_dir is None:
        gh_dir = tempfile.mkdtemp(prefix="ratchetloop-gh-")
    Path(gh_dir).mkdir(parents=True, exist_ok=True)
    env["GH_CONFIG_DIR"] = gh_dir
    env["LANGSMITH_TRACING"] = "false"
    # No Python bytecode in a worktree: a Phase 5 build committed __pycache__/*.pyc from the coder's
    # own test runs (the fresh repository ignored nothing). check_env builds on this; a task's `env`
    # can still set it otherwise.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # No shared .NET build servers: a reviewer's `dotnet test` inside its read-only sandbox left a
    # compiler/MSBuild server that the next builds — the coder's and the pipeline's checks — reused,
    # and every write through it failed "access denied" (Donchian build, 2026-09-12).
    env["DOTNET_CLI_USE_MSBUILD_SERVER"] = "0"
    env["MSBUILDDISABLENODEREUSE"] = "1"
    env["UseSharedCompilation"] = "false"
    return env


def _alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except OSError:
        return False


def _try_wait(pgid: int) -> None:
    try:
        while True:
            pid, _st = os.waitpid(-pgid, os.WNOHANG)
            if pid <= 0:
                break
    except (ChildProcessError, OSError):
        pass


def _wait_dead(pgid: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        _try_wait(pgid)
        if not _alive(pgid):
            return True
        time.sleep(_POLL_S)
    _try_wait(pgid)
    return not _alive(pgid)


def _reap_pgid(pgid: int, term_s: float = _TERM_S, kill_s: float = _KILL_S) -> bool:
    """TERM, wait, KILL, wait. True iff the group is dead."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        _try_wait(pgid)
        return not _alive(pgid)
    if _wait_dead(pgid, term_s):
        return True
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        _try_wait(pgid)
        return not _alive(pgid)
    return _wait_dead(pgid, kill_s)


def _kill_stale(pidfile: Path) -> bool:
    """Reap a leftover process group. True if it is safe to proceed."""
    if not pidfile.is_file():
        return True
    try:
        pgid = int(pidfile.read_text().strip())
    except ValueError:
        return True
    if not _alive(pgid):
        return True
    return _reap_pgid(pgid)


def _kill_pg(proc: subprocess.Popen) -> bool:
    """TERM then KILL the worker's process group; True only once the whole group is gone.

    Polls after each signal (via _reap_pgid) instead of checking once: a single check right after
    SIGKILL counted group members that were still dying, so a real grok killed on silence was
    recorded cleanup_failed although its group was dead a moment later (Phase 1 spike, 2026-09-11)."""
    # Workers start with start_new_session=True, so the group id is the leader's pid; it stays
    # valid while any member lives, even after the leader is reaped (review finding 8).
    dead = _reap_pgid(proc.pid)
    try:
        proc.wait(timeout=_POLL_S)
    except Exception:
        pass
    return dead


def _nonblock(fh) -> None:
    if fh is None:
        return
    fd = fh.fileno()
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


class StreamBuf:
    """Incremental newline splitter. prefix is prepended to each decoded line."""

    def __init__(self, fh, prefix: str = "") -> None:
        self.fh = fh
        self.fd = fh.fileno() if fh is not None else -1
        self.buf = bytearray()
        self.prefix = prefix
        self.eof = fh is None

    def feed(self) -> list[str]:
        if self.eof or self.fd < 0:
            return []
        try:
            chunk = os.read(self.fd, _READ_CHUNK)
        except BlockingIOError:
            return []
        except OSError:
            self.eof = True
            return self._rest()
        if not chunk:
            self.eof = True
            return self._rest()
        self.buf.extend(chunk)
        return self._pop()

    def _pop(self) -> list[str]:
        lines: list[str] = []
        while True:
            nl = self.buf.find(b"\n")
            if nl < 0:
                if len(self.buf) > MAX_LINE_BYTES:
                    raise ProtocolLineError
                return lines
            raw = bytes(self.buf[: nl + 1])
            del self.buf[: nl + 1]
            if len(raw) > MAX_LINE_BYTES:
                raise ProtocolLineError
            lines.append(self._decode(raw))
        return lines

    def _rest(self) -> list[str]:
        if not self.buf:
            return []
        if len(self.buf) > MAX_LINE_BYTES:
            raise ProtocolLineError
        line = self._decode(bytes(self.buf))
        self.buf.clear()
        return [line]

    def _decode(self, raw: bytes) -> str:
        text = raw.decode("utf-8", errors="replace")
        if not text.endswith("\n"):
            text += "\n"
        return self.prefix + text if self.prefix else text
