"""Structured worker events, typed outcomes, wrapper sidecars."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EVENT_KINDS = frozenset({"progress", "tool", "result", "error", "usage", "unknown"})
RESULT_STATUS = frozenset({"done", "blocked"})
QUOTA_CODES = frozenset({429, "429", "rate_limit", "quota", "usage_limit"})
# incident 60: documented exhaustion text/codes, next to QUOTA_CODES
QUOTA_TEXT: dict[str, tuple[str, ...]] = {
    "codex": ("out of credits", "usage limit"),
    "claude": ("rate_limit_error", "billing_error"),
    "grok": ("rate_limit", "usage_limit", "429"),
    "copilot": ("quota exceeded", "rate_limit", "you've hit a rate limit"),
}

RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["done", "blocked"]},
        "reason": {"type": "string"}, "summary": {"type": "string"},
    },
    "required": ["status", "reason", "summary"],
    # codex structured-output rejects a schema without additionalProperties
    "additionalProperties": False,
}
RESULT_SCHEMA_JSON = json.dumps(RESULT_SCHEMA, separators=(",", ":"))
FINDING_TAGS = ("bug", "suggestion", "nit")
# Reviewers also report finding counts (CONTRACT.md §4): velocity data, never control flow, so they
# may be null. codex strict structured output needs every property listed as required, so
# `findings` is required but nullable (Phase 1 review, finding 5).
REVIEWER_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **RESULT_SCHEMA["properties"],
        "findings": {"anyOf": [
            {
                "type": "object",
                "properties": {tag: {"type": "integer"} for tag in FINDING_TAGS},
                "required": list(FINDING_TAGS),
                "additionalProperties": False,
            },
            {"type": "null"},
        ]},
        # §9.5: a bug that leaves the task's acceptance criteria unmet; required but nullable for
        # the same strict-output reason as `findings`.
        "blocking": {"anyOf": [{"type": "boolean"}, {"type": "null"}]},
    },
    "required": ["status", "reason", "summary", "findings", "blocking"],
    "additionalProperties": False,
}
REVIEWER_RESULT_SCHEMA_JSON = json.dumps(REVIEWER_RESULT_SCHEMA, separators=(",", ":"))
_PROGRESS = frozenset({
    "progress", "stream_event", "content_block_delta", "content_block_start",
    "message_start", "message_delta", "message_stop", "assistant", "system",
    "thread.started", "turn.started", "item.completed", "user"})
_TOOL = frozenset({"tool", "tool_use", "tool_call", "function_call", "item.started"})
_RESULT = frozenset({"result"})
_ERROR = frozenset({"error", "exception"})
_USAGE = frozenset({"usage", "token_usage", "turn.completed"})

@dataclass(frozen=True)
class Event:
    kind: str
    text: str
    raw: str
    seq: int = -1

@dataclass
class WorkerOutcome:
    kind: str
    text: str = ""
    reason: str = ""
    summary: str = ""
    exit_code: int | None = None
    result: dict[str, Any] | None = None
    usage: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def quota(cls, reason: str = "quota", **kw: Any) -> WorkerOutcome:
        return cls(kind="quota", reason=reason, **kw)

    @classmethod
    def error(cls, reason: str = "error", **kw: Any) -> WorkerOutcome:
        return cls(kind="error", reason=reason, **kw)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "text": self.text, "reason": self.reason,
            "summary": self.summary, "exit_code": self.exit_code,
        }


def _kind_or_invalid(kind: Any) -> str:
    if kind in (None, "", "unknown"):
        return "invalid_result"
    return str(kind)


def outcome_payload(result: Any) -> dict[str, Any]:
    if isinstance(result, WorkerOutcome):
        payload = result.as_dict()
        payload["kind"] = _kind_or_invalid(payload.get("kind"))
        payload["usage"] = result.usage or {}
        return payload
    if isinstance(result, dict) and "kind" in result:
        return {
            "kind": _kind_or_invalid(result.get("kind")),
            "text": result.get("text") or "",
            "reason": result.get("reason") or "",
            "summary": result.get("summary") or "",
            "exit_code": result.get("exit_code"),
        }
    text = "" if result is None else str(result)
    return {"kind": "invalid_result", "text": text, "reason": "invalid_result",
            "summary": "", "exit_code": None}


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        os.write(fd, text.encode())
        os.close(fd)
        fd = -1
        os.replace(tmp, path)
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_heartbeat(out_file: str, at: float, n: int, kind: str) -> None:
    atomic_write(
        Path(str(out_file) + ".events"),
        json.dumps({"at": at, "n": n, "kind": kind}, separators=(",", ":")) + "\n",
    )


def write_exit(out_file: str, outcome: WorkerOutcome) -> None:
    atomic_write(
        Path(str(out_file) + ".exit"),
        json.dumps({
            "exit": outcome.exit_code, "kind": outcome.kind,
            "reason": outcome.reason, "summary": outcome.summary,
        }, separators=(",", ":")) + "\n",
    )


def _legacy_zero_outcome(out_file: str) -> WorkerOutcome:
    """Raw `0` sidecar is done only when the log also has a structured result."""
    path = Path(out_file)
    if path.is_file():
        for line in path.read_text().splitlines():
            if line.startswith("err:"):
                continue
            obj = loads_obj(line)
            if not obj or str(obj.get("type") or "") != "result":
                continue
            fields = result_fields(obj)
            if fields:
                return WorkerOutcome(
                    kind=fields["status"], exit_code=0,
                    reason=fields["reason"], summary=fields["summary"],
                    result=fields,
                )
    return WorkerOutcome(
        kind="invalid_result", exit_code=0,
        reason="legacy exit without result")


def load_worker_outcome(out_file: str) -> WorkerOutcome | None:
    """Read `<out_file>.exit` written by the wrapper. Item 69 import surface."""
    path = Path(str(out_file) + ".exit")
    if not path.is_file():
        return None
    raw = path.read_text().strip()
    if not raw:
        return None
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        try:
            body = int(raw.splitlines()[0].strip())
        except ValueError:
            return None
    if isinstance(body, int):
        if body == 0:
            return _legacy_zero_outcome(out_file)
        return WorkerOutcome(kind="error", exit_code=body, reason=f"exit {body}")
    if not isinstance(body, dict):
        return None
    rc = body.get("exit")
    try:
        rc_i = int(rc) if rc is not None else None
    except (TypeError, ValueError):
        rc_i = None
    return WorkerOutcome(
        kind=_kind_or_invalid(body.get("kind")),
        reason=str(body.get("reason") or ""),
        summary=str(body.get("summary") or ""),
        exit_code=rc_i,
    )


def loads_obj(line: str) -> dict[str, Any] | None:
    s = line.strip()
    if not s:
        return None
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _text_of(obj: dict[str, Any]) -> str:
    for key in ("text", "message", "summary", "result"):
        val = obj.get(key)
        if isinstance(val, str):
            return val
        if isinstance(val, dict) and isinstance(val.get("text"), str):
            return val["text"]
    return ""


def _findings(raw: Any) -> dict[str, int] | None:
    """Well-formed finding counts, else None: counts never invalidate a result."""
    if not isinstance(raw, dict):
        return None
    counts = {tag: raw.get(tag) for tag in FINDING_TAGS}
    if all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in counts.values()):
        return counts
    return None


def result_fields(obj: dict[str, Any] | None) -> dict[str, Any] | None:
    """Schema fields from a terminal result envelope only (not content/prose). A reviewer's
    `findings` counts ride along when present; malformed counts become None."""
    if not obj:
        return None
    candidates: list[dict[str, Any]] = [obj]
    inner = obj.get("result")
    if isinstance(inner, dict):
        candidates.append(inner)
    elif isinstance(inner, str):
        parsed = loads_obj(inner) or loads_obj(_last_nonempty(inner))
        if parsed:
            candidates.append(parsed)
    for c in candidates:
        status, reason, summary = c.get("status"), c.get("reason"), c.get("summary")
        if (status in RESULT_STATUS and isinstance(reason, str)
                and isinstance(summary, str)):
            fields: dict[str, Any] = {"status": status, "reason": reason, "summary": summary}
            if "findings" in c:
                fields["findings"] = _findings(c.get("findings"))
            if "blocking" in c:
                fields["blocking"] = c.get("blocking") is True
            return fields
    return None


def _last_nonempty(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def last_line_fields(text: str) -> dict[str, str] | None:
    return result_fields(loads_obj(_last_nonempty(text)))


def _event(kind: str, obj: dict[str, Any], raw: str) -> Event:
    kind = kind if kind in EVENT_KINDS else "unknown"
    return Event(kind, _text_of(obj), raw.rstrip("\n"))


def classify_type(typ: str, obj: dict[str, Any]) -> str:
    if typ in _ERROR or obj.get("is_error") is True:
        return "error"
    if typ in _USAGE:
        return "usage"
    if typ in _RESULT:
        return "result"
    if typ in _TOOL:
        return "tool"
    return "progress" if typ in _PROGRESS else "unknown"


def parse_jsonl_event(line: str) -> Event | None:
    obj = loads_obj(line)
    if obj is None:
        return None
    typ = str(obj.get("type") or "")
    block = obj.get("content_block") if isinstance(obj.get("content_block"), dict) else {}
    if typ == "content_block_start" and block.get("type") == "tool_use":
        return _event("tool", obj, line)
    item = obj.get("item") if isinstance(obj.get("item"), dict) else {}
    if typ == "item.started" and item.get("type") in (
            "command_execution", "command", "tool", "mcp_tool_call"):
        return _event("tool", obj, line)
    return _event(classify_type(typ, obj), obj, line)

def codex_events(line: str) -> Event | None:
    ev = parse_jsonl_event(line)
    obj = loads_obj(ev.raw) if ev else None
    item = obj.get("item") if isinstance(obj, dict) else None
    if (ev and isinstance(item, dict)
            and str(obj.get("type") or "") == "item.completed"
            and item.get("type") == "agent_message"):
        return Event("result", str(item.get("text") or ev.text), ev.raw)
    return ev


grok_events = claude_events = copilot_events = parse_jsonl_event
