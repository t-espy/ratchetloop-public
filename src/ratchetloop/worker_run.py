"""CLI process loop: events, silence, wrapper .exit / .events sidecars."""
from __future__ import annotations

import os
import re
import select
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .worker_io import (
    Event, WorkerOutcome, last_line_fields,
    loads_obj, result_fields, write_exit, write_heartbeat,
)
from .worker_quota import event_is_quota, text_is_quota
from .worker_proc import (
    ProtocolLineError, StreamBuf, _kill_pg, _kill_stale, _nonblock, _reap_pgid, worker_env,
)
from .signals import RunKilled, killed_why, set_active_pgid
from .worker_usage import copilot_usage_file, usage_from_obj

WORKER_SILENCE_S = 900.0

# protocol_error > quota > error (schema/API never recover; retry shape only if error.seq < result.seq at live rc==0) > exit > blocked > done > invalid_result.
def _transient_error(ev: Event) -> bool:
    obj = loads_obj(ev.raw) or {}
    nest = obj["error"] if isinstance(obj.get("error"), dict) else {}
    bits = (obj.get("type"), obj.get("code"), obj.get("name"), None if nest else obj.get("error"),
            nest.get("type"), nest.get("code"), nest.get("name"))
    meta = " ".join(str(x).lower().replace("-", "_").replace(" ", "_") for x in bits if x)
    if any(p in meta for p in ("schema", "api_error", "apierror", "invalid_request", "bad_request")):
        return False
    t = " ".join(ev.text.lower().split())
    return t == "transport-fallback" or bool(re.fullmatch(
        r"reconnecting\.\.\.( \d+/\d+)? \(request timed out\)", t))


def _keep_error(prev: Event | None, ev: Event) -> Event:
    if prev is None or _transient_error(prev) or not _transient_error(ev):
        return ev
    return prev


def _recovered_transient(
    error_ev: Event, fields: dict[str, str] | None, rc: int | None,
    result_ev: Event | None = None) -> bool:
    i, j = error_ev.seq, result_ev.seq if result_ev else -1
    return bool(fields) and rc == 0 and _transient_error(error_ev) and 0 <= i < j


def _last_sidecar(out_file: str) -> str | None:
    path = Path(str(out_file) + ".last")
    return path.read_text() if path.is_file() else None


def _codex_schema(
    obj: dict[str, Any] | None, last_text: str | None,
) -> dict[str, str] | None:
    item = obj.get("item") if isinstance(obj, dict) and isinstance(
        obj.get("item"), dict) else {}
    for blob in (item, item.get("text"), last_text):
        cand = blob if isinstance(blob, dict) else (
            loads_obj(blob) if isinstance(blob, str) else None)
        fields = result_fields(cand)
        if fields:
            return fields
    return None


def silence_s(wall_budget_s: float) -> float:
    raw = os.environ.get("WORKER_SILENCE_S")
    default = float(raw) if raw not in (None, "") else WORKER_SILENCE_S
    return min(default, float(wall_budget_s))


def _usage_update(ev: Event) -> dict[str, Any]:
    """Usage reported on a usage event or on the result line itself. predecessor read usage events
    only, and claude reports its tokens and cost on its result line, so none were ever recorded."""
    return usage_from_obj(loads_obj(ev.raw)) or {}


def _merge_usage(usage: dict[str, Any], got: dict[str, Any]) -> None:
    """Fill in reported values only: a later, partial report must not erase an earlier number
    (Phase 1 review, finding 4)."""
    for key, value in got.items():
        if value is not None:
            usage[key] = value


def interpret_run(
    provider: str, text: str, rc: int | None, result_ev: Event | None,
    error_ev: Event | None, quota: bool, usage: dict[str, Any],
    last_text: str | None = None,
) -> WorkerOutcome:
    if not quota and error_ev is not None and text_is_quota(error_ev.text, provider):
        quota = True
    if quota:
        return WorkerOutcome.quota(
            reason="quota", text=text, exit_code=rc, usage=usage)
    obj = loads_obj(result_ev.raw) if result_ev is not None else None
    fields = result_fields(obj)
    if not fields and result_ev is not None:
        fields = last_line_fields(result_ev.text)
    if not fields and provider == "codex" and result_ev is not None:
        fields = _codex_schema(obj, last_text)
        if not fields and last_text:
            fields = last_line_fields(last_text)
    if error_ev is not None and not _recovered_transient(error_ev, fields, rc, result_ev):
        why = error_ev.text or "error event"
        return WorkerOutcome.error(reason=why, text=text, exit_code=rc, usage=usage)
    if rc not in (0, None):
        return WorkerOutcome.error(reason=f"exit {rc}", text=text, exit_code=rc, usage=usage)
    if result_ev is None:
        return WorkerOutcome(
            kind="invalid_result", reason="missing result object",
            text=text, exit_code=rc, usage=usage)
    if fields and fields["status"] in ("blocked", "done"):
        return WorkerOutcome(
            kind=fields["status"], text=text, reason=fields["reason"],
            summary=fields["summary"], exit_code=rc, result=fields, usage=usage)
    return WorkerOutcome(
        kind="invalid_result", reason="invalid result object",
        text=text, exit_code=rc, usage=usage)


def _finish(out_file: str, outcome: WorkerOutcome, started: float,
            rc: int | None) -> WorkerOutcome:
    usage = dict(outcome.usage or {})
    usage["exit"] = rc if rc is not None else outcome.exit_code
    usage["error"] = outcome.kind not in ("done", "blocked")
    usage["wall_s"] = time.time() - started
    outcome.usage = usage
    if outcome.exit_code is None:
        outcome.exit_code = rc
    write_exit(out_file, outcome)
    return outcome


def _stopped(out_file: str, outcome: WorkerOutcome, proc: subprocess.Popen,
             started: float, rc: int) -> WorkerOutcome:
    text = Path(out_file).read_text() if Path(out_file).is_file() else outcome.text
    if not _kill_pg(proc):
        return _finish(out_file, WorkerOutcome(
            kind="cleanup_failed", reason="cleanup_failed", text=text,
            exit_code=rc, usage=outcome.usage), started, rc)
    outcome.text = text
    return _finish(out_file, outcome, started, rc)


def outcome_from_log(
    provider: str, out_file: str, parser: Callable[[str], Event | None],
) -> WorkerOutcome | None:
    """Rebuild a typed outcome from wrapper-flushed JSONL after a parent kill."""
    path = Path(out_file)
    if not path.is_file():
        return None
    text = path.read_text()
    result_ev: Event | None = None
    error_ev: Event | None = None
    quota, n = False, 0
    usage: dict[str, Any] = {}
    for line in text.splitlines(True):
        if line.startswith("err:"):
            continue
        ev = parser(line)
        if ev is None:
            continue
        n += 1
        ev = Event(ev.kind, ev.text, ev.raw, n)
        if ev.kind == "result":
            result_ev = ev
        elif ev.kind == "error":
            error_ev = _keep_error(error_ev, ev)
        if ev.kind in ("usage", "result"):
            _merge_usage(usage, _usage_update(ev))
        if event_is_quota(ev, provider):
            quota = True
    if result_ev is None and error_ev is None and not quota:
        return None
    last = _last_sidecar(out_file) if provider == "codex" else None
    return interpret_run(
        provider, text, None, result_ev, error_ev, quota, usage, last)


def _apply_stdout(
    provider: str, line: str, parser: Callable[[str], Event | None],
    result_ev: Event | None, error_ev: Event | None, quota: bool,
    usage: dict[str, Any], n_events: int, out_file: str,
) -> tuple[Event | None, Event | None, bool, int, float]:
    ev = parser(line)
    if ev is None:
        return result_ev, error_ev, quota, n_events, 0.0
    last, n_events = time.time(), n_events + 1
    ev = Event(ev.kind, ev.text, ev.raw, n_events)
    write_heartbeat(out_file, last, n_events, ev.kind)
    if ev.kind == "result":
        result_ev = ev
    elif ev.kind == "error":
        error_ev = _keep_error(error_ev, ev)
    if ev.kind in ("usage", "result"):
        _merge_usage(usage, _usage_update(ev))
    if event_is_quota(ev, provider):
        quota = True
    return result_ev, error_ev, quota, n_events, last


def run_cli(
    provider: str, argv: list[str], workdir: str, out_file: str,
    timeout: float, parser: Callable[[str], Event | None], wall_budget_s: float,
    stdin_path: str | None = None,
) -> WorkerOutcome:
    out_path = Path(out_file)
    pidfile = Path(str(out_file) + ".pid")
    env = worker_env()
    started = time.time()
    idle = silence_s(wall_budget_s)
    last_event_at = started
    n_events = 0
    result_ev: Event | None = None
    error_ev: Event | None = None
    quota = False
    usage: dict[str, Any] = {}
    if not _kill_stale(pidfile):
        return _finish(out_file, WorkerOutcome(
            kind="cleanup_failed", reason="cleanup_failed"), started, None)
    try:
        stdin_fh = open(stdin_path, "rb") if stdin_path else None
    except OSError:
        return _finish(out_file, WorkerOutcome.error(
            reason=f"brief not found: {stdin_path}"), started, None)
    blocked = {signal.SIGTERM, signal.SIGINT}

    def _child_unblock():
        signal.pthread_sigmask(signal.SIG_UNBLOCK, blocked)

    prev_mask = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
    try:
        proc = subprocess.Popen(
            argv, stdin=stdin_fh or subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True,
            cwd=workdir, env=env, bufsize=0, preexec_fn=_child_unblock,
        )
        pgid = os.getpgid(proc.pid)
        pidfile.write_text(str(pgid))
        set_active_pgid(pgid)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, prev_mask)
        if stdin_fh:
            stdin_fh.close()
    _nonblock(proc.stdout)
    _nonblock(proc.stderr)
    out_buf = StreamBuf(proc.stdout)
    err_buf = StreamBuf(proc.stderr, prefix="err:")
    log = out_path.open("w", encoding="utf-8")
    rc: int | None = None
    keep_pgid = False
    try:
        while True:
            why = killed_why()
            if why:
                raise RunKilled(why)
            now = time.time()
            if now - started > timeout:
                return _stopped(out_file, WorkerOutcome(
                    kind="timeout", reason="timeout", exit_code=-9, usage=usage),
                    proc, started, -9)
            if now - last_event_at > idle:
                return _stopped(out_file, WorkerOutcome(
                    kind="silence", reason="silence", exit_code=-9, usage=usage),
                    proc, started, -9)
            fds = [b.fd for b in (out_buf, err_buf) if not b.eof and b.fd >= 0]
            wait = min(0.25, idle - (now - last_event_at), timeout - (now - started))
            if fds:
                try:
                    ready, _, _ = select.select(fds, [], [], max(0.0, wait))
                except InterruptedError as e:
                    raise RunKilled(killed_why() or "SIGINT") from e
                except (ValueError, OSError):
                    ready = []
            else:
                ready = []
                if proc.poll() is not None:
                    rc = proc.wait()
                    break
            try:
                for buf in (out_buf, err_buf):
                    if buf.fd not in ready:
                        continue
                    for line in buf.feed():
                        log.write(line)
                        log.flush()
                        if buf is err_buf:
                            continue
                        result_ev, error_ev, quota, n_events, last = _apply_stdout(
                            provider, line, parser, result_ev, error_ev, quota,
                            usage, n_events, out_file)
                        if last:
                            last_event_at = last
            except ProtocolLineError:
                return _stopped(out_file, WorkerOutcome(
                    kind="protocol_error", reason="protocol_error",
                    exit_code=-9, usage=usage), proc, started, -9)
            if out_buf.eof and err_buf.eof:
                rc = proc.wait()
                break
    except RunKilled:
        keep_pgid = True
        raise
    finally:
        log.close()
        if not keep_pgid:
            set_active_pgid(None)
    if rc is None:
        rc = proc.wait()
    # What the worker left in its group (a build server, a test daemon) ends with it; never our own group.
    if pgid != os.getpgrp() and not _reap_pgid(pgid):
        return _finish(out_file, WorkerOutcome(kind="cleanup_failed", reason="cleanup_failed",
                                               exit_code=rc, usage=usage), started, rc)
    if provider == "copilot":
        _merge_usage(usage, copilot_usage_file(out_file))
    text = out_path.read_text() if out_path.is_file() else ""
    last = _last_sidecar(out_file) if provider == "codex" else None
    return _finish(
        out_file,
        interpret_run(provider, text, rc, result_ev, error_ev, quota, usage, last),
        started, rc,
    )
