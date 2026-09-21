"""SIGTERM/SIGINT during a worker launch: remember why, reap the active worker's process group, and
raise RunKilled so the caller's `finally` can record what happened. Carried from predecessor without
its graph and lab recording."""
from __future__ import annotations

import signal
from contextlib import contextmanager
from typing import Iterator


class RunKilled(BaseException):
    """Bypasses `except Exception`, so the run's `finally` still records the kill."""


_active_pgid: int | None = None
_killed_why: str | None = None
_closing = False


def set_active_pgid(pgid: int | None) -> None:
    global _active_pgid
    _active_pgid = pgid


def killed_why() -> str | None:
    return _killed_why


def reap_active_worker() -> bool:
    global _active_pgid
    pgid = _active_pgid
    _active_pgid = None
    if pgid is None:
        return True
    from .worker_proc import _reap_pgid
    return _reap_pgid(pgid)


def install_kill_handler() -> None:
    global _killed_why, _closing
    _killed_why, _closing = None, False

    def _handler(signum, _frame):
        global _killed_why
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = str(signum)
        _killed_why = name
        reap_active_worker()
        if not _closing:
            raise RunKilled(name)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def closing() -> None:
    """From here on a signal is recorded, not raised: the run is writing its result."""
    global _closing
    _closing = True


@contextmanager
def kill_handler() -> Iterator[None]:
    """The kill handler for one run, the previous handlers restored after it. Never installed, a
    SIGTERM ended `ratchetloop run` without result.json (Phase 4)."""
    global _closing
    previous = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    was_closing = _closing
    install_kill_handler()
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        # A build nests one run per stage: after a stage closes, a signal to the build must still
        # stop it, so the outer run's state comes back with its handler.
        _closing = was_closing
