"""Append-only run events (CONTRACT.md §3.1): one JSON object per line, flushed and fsynced
before the caller moves on, so a run killed at any point leaves every earlier step on disk."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RESERVED = frozenset({"t", "run_id", "event"})


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class EventSink:
    """Writes `<run dir>/events.jsonl`. One sink per run."""

    def __init__(self, path: Path, run_id: str):
        self.path = Path(path)
        self.run_id = run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        if not event:
            raise ValueError("an event needs a name")
        clash = RESERVED & set(fields)
        if clash:
            raise ValueError(f"reserved event fields: {sorted(clash)}")
        record = {"t": utc_now(), "run_id": self.run_id, "event": event, **fields}
        line = json.dumps(record, separators=(",", ":"), default=str) + "\n"
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        return record


def read_events(path: Path) -> list[dict[str, Any]]:
    """Every well-formed event in order; a torn last line (death mid-write) is skipped."""
    p = Path(path)
    if not p.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in p.read_text().splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out
