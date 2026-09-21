"""Who is running a task, and what a later run does about one that died (CONTRACT.md §3,
FAILURE_MODES.md §7).

Two locks. An exclusive flock per task (`<runs_root>/.locks/<task_key>.lock`), taken at the start of
admit and held for the run: two runs of one task cannot both get past admit, and the kernel drops it
when its holder dies. And each run's `lock` file, naming the pipeline's pid and the kernel's start
time for it (D33), so a later run can tell which run died. That later run marks it abandoned: it
reaps the process groups the dead run recorded (workers and checks write theirs to `*.pid`), appends
a FAILED `abandoned` disposition naming the last step on disk, and writes the result.json the dead
run never wrote — or, if it did write one, leaves it alone."""
from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any

from . import gitops
from .events import EventSink, read_events
from .result import build_result, write_result
from .state import Refused, RunState
from .task import TaskError, load_task
from .worker_proc import _reap_pgid
from .worktree import _worktree_path

LOCK = "lock"


class TaskHold:
    """The task's flock, held from admit to the end of the run (Phase 4 review, finding 2: the run
    lock alone was written after admit, so two starts could both pass it)."""

    def __init__(self, fh: Any):
        self.fh = fh

    def release(self) -> None:
        if self.fh is not None:
            try:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            finally:
                self.fh.close()
                self.fh = None


def hold_task(runs_root: Path, task_key: str) -> TaskHold:
    path = Path(runs_root) / ".locks" / f"{task_key}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise Refused(f"task {task_key} is running (another ratchetloop holds {path})") from None
    return TaskHold(fh)


def _stat_fields(pid: int) -> list[str] | None:
    """`/proc/<pid>/stat` from field 3 on (after the command name, which may hold spaces)."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return raw.rsplit(")", 1)[1].split()


def start_ticks(pid: int) -> str | None:
    fields = _stat_fields(pid)
    return fields[19] if fields and len(fields) > 19 else None  # field 22: starttime


def write_lock(run_dir: Path) -> None:
    pid = os.getpid()
    (Path(run_dir) / LOCK).write_text(f"{pid} {start_ticks(pid)} {time.time():.0f}\n")


def read_lock(run_dir: Path) -> tuple[int, str | None, float] | None:
    """(pid, start ticks, unix time). A Phase 3 lock is `pid time`: its start is unknown."""
    try:
        parts = (Path(run_dir) / LOCK).read_text().split()
        pid = int(parts[0])
        at = float(parts[-1])
    except (OSError, ValueError, IndexError):
        return None
    return pid, (parts[1] if len(parts) > 2 else None), at


def lock_alive(run_dir: Path) -> bool:
    held = read_lock(run_dir)
    if held is None:
        return False
    pid, start, _ = held
    now = start_ticks(pid)
    return now is not None and (start is None or now == start)


def _members(pgid: int) -> list[int]:
    out = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            fields = _stat_fields(int(entry.name))
            if fields and len(fields) > 2 and fields[2] == str(pgid):  # field 5: pgrp
                out.append(int(entry.name))
    return out


def _inside(pid: int, roots: list[Path]) -> bool:
    try:
        cwd = Path(os.readlink(f"/proc/{pid}/cwd"))
    except OSError:
        return False
    return any(cwd == r or r in cwd.parents for r in roots)


def reap_groups(run_dir: Path, roots: list[Path]) -> list[int]:
    """Kill the process groups a dead run recorded, but only a group with a member working inside
    `roots` (its worktree or run directory): a recycled pgid belongs to someone else. A launch that
    wrote its `.exit` file ended normally, so its pgid is skipped outright (Phase 4 review,
    finding 5)."""
    roots = [Path(r).resolve() for r in roots]
    reaped = []
    for pidfile in sorted(Path(run_dir).glob("*.pid")):
        if pidfile.with_suffix(".exit").exists():
            continue
        try:
            pgid = int(pidfile.read_text().split()[0])
        except (OSError, ValueError, IndexError):
            continue
        if any(_inside(pid, roots) for pid in _members(pgid)) and _reap_pgid(pgid):
            reaped.append(pgid)
    return reaped


def _describe(ev: dict[str, Any] | None) -> str:
    if not ev:
        return "none"
    bits = [str(ev.get("event"))] + [f"{k}={ev[k]}" for k in ("role", "round", "provider")
                                     if ev.get(k) is not None]
    return " ".join(bits) + f" at {ev.get('t')}"


def _read_result(run_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((run_dir / "result.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _state(run_dir: Path, runs_root: Path, task: Any, admit: dict[str, Any], events: list,
           sink: EventSink, detail: str, started: float | None, tree: Path) -> RunState:
    st = RunState(task=task, run_id=run_dir.name, run_dir=run_dir, runs_root=runs_root, sink=sink,
                  coder=admit.get("coder") or "", reviewer=admit.get("reviewer") or "",
                  coder_family=admit.get("coder_family") or "", branch=admit.get("branch") or "",
                  base_sha=admit.get("base_sha") or "", disposition="FAILED", reason="abandoned",
                  detail=detail)
    st.started = started or st.started
    st.head_sha = gitops.git_try("rev-parse", "--verify", "-q", f"refs/heads/{st.branch}",
                                 cwd=task.repo) or ""
    st.worktree_kept = str(tree) if tree.exists() else None
    for ev in events:  # the last launch of each role names the model it ran
        if ev.get("event") == "worker_start":
            meta = {k: ev.get(k) for k in ("model", "effort", "family")}
            if ev.get("role") == "coder":
                st.coder_meta = meta
            elif ev.get("role") == "reviewer":
                st.reviewer_meta, st.reviewer = meta, ev.get("provider") or st.reviewer
    return st


def abandon(run_dir: Path, runs_root: Path) -> dict[str, Any]:
    """Record a dead run: reap what it left running, then its disposition and result.json."""
    run_dir = Path(run_dir)
    events = read_events(run_dir / "events.jsonl")
    admit = next((e for e in events if e.get("event") == "admit"), None)
    held = read_lock(run_dir)
    try:
        task = load_task(run_dir / "task.yaml")
    except (TaskError, OSError):
        task = None
    tree = _worktree_path(task.repo, task.task_key) if task else None
    reaped = reap_groups(run_dir, [p for p in (tree, run_dir) if p is not None])
    written = _read_result(run_dir)
    if written is not None:
        # It wrote its result and died before removing its lock. The record stands: an ACCEPT
        # rewritten as abandoned would let --continue resume an accepted task (Phase 4 review,
        # finding 1).
        (run_dir / LOCK).unlink(missing_ok=True)
        return written
    detail = (f"the pipeline died (pid {held[0] if held else '?'}); last event: "
              f"{_describe(events[-1] if events else None)}")
    if reaped:
        detail += f"; reaped process groups {reaped}"
    sink = EventSink(run_dir / "events.jsonl", run_dir.name)
    sink.emit("disposition", disposition="FAILED", reason="abandoned", detail=detail)
    if task is None or admit is None:
        result = {"task_key": run_dir.parent.name, "run_id": run_dir.name,
                  "disposition": "FAILED", "reason": "abandoned", "detail": detail}
    else:
        result = build_result(_state(run_dir, runs_root, task, admit, events, sink, detail,
                                     held[2] if held else None, tree))
    write_result(run_dir / "result.json", result)
    (run_dir / LOCK).unlink(missing_ok=True)
    return result


def settle(task_runs: Path, runs_root: Path, *, dry: bool = False) -> list[str]:
    """With the task held: refuse if a run's lock still names a live process (one started without
    the hold); mark each dead one abandoned (not when `dry`, i.e. under `--check`)."""
    task_runs = Path(task_runs)
    if not task_runs.is_dir():
        return []
    done = []
    for run_dir in sorted(p for p in task_runs.iterdir() if (p / LOCK).exists()):
        if lock_alive(run_dir):
            held = read_lock(run_dir)
            raise Refused(f"task {task_runs.name} is running (pid {held[0] if held else '?'}, "
                          f"{run_dir})")
        if not dry:
            abandon(run_dir, runs_root)
            done.append(run_dir.name)
    return done
