"""events.jsonl is the run record: append-only, one object per line, readable after a crash."""
import json

import pytest

from ratchetloop.events import EventSink, read_events


def test_emit_appends_one_line_per_event(tmp_path):
    sink = EventSink(tmp_path / "run" / "events.jsonl", "20260911T201500Z")
    sink.emit("admit", task_key="add-mul")
    sink.emit("worker_end", role="coder", round=1, kind="done", tokens_in=None)
    lines = (tmp_path / "run" / "events.jsonl").read_text().splitlines()
    assert len(lines) == 2
    first, second = (json.loads(line) for line in lines)
    assert first["event"] == "admit" and first["run_id"] == "20260911T201500Z"
    assert first["t"].endswith("Z")
    assert second["tokens_in"] is None  # unknown stays null, never zero


def test_reserved_fields_are_refused(tmp_path):
    sink = EventSink(tmp_path / "events.jsonl", "r")
    with pytest.raises(ValueError, match="reserved"):
        sink.emit("admit", run_id="other")
    with pytest.raises(ValueError):
        sink.emit("")
    assert not (tmp_path / "events.jsonl").exists()


def test_read_skips_a_torn_last_line(tmp_path):
    path = tmp_path / "events.jsonl"
    EventSink(path, "r").emit("admit")
    with path.open("a") as fh:
        fh.write('{"t": "x", "event": "work')  # killed mid-write
    events = read_events(path)
    assert [e["event"] for e in events] == ["admit"]
    assert read_events(tmp_path / "missing.jsonl") == []
