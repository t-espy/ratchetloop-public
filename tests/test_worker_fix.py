"""Item 70 fix-round: paths, result-only completion, reads, precedence, kill."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ratchetloop.worker_io import (
    Event, WorkerOutcome, claude_events, copilot_events, codex_events,
    grok_events, load_worker_outcome, outcome_payload,
)
from ratchetloop.worker_run import interpret_run
from ratchetloop.workers import PROVIDERS, copilot_argv, run_worker
from test_workers import FAKE, _install_fake, _pipe_popen

CODEX_JSONL = Path(__file__).resolve().parent / "fixtures" / "codex_exec_jsonl.jsonl"
_SCHEMA = json.dumps({"status": "done", "reason": "", "summary": "ok"})


def _result(status="done", reason="", summary="ok", seq=-1) -> Event:
    raw = json.dumps({
        "type": "result", "status": status, "reason": reason, "summary": summary,
    })
    return Event("result", summary, raw, seq)


def _error(code="boom", seq=-1) -> Event:
    raw = json.dumps({"type": "error", "code": code, "text": str(code)})
    return Event("error", str(code), raw, seq)


def test_codex_dash_o_is_sidecar_not_log(tmp_path, monkeypatch):
    out = str(tmp_path / "out")
    last = out + ".last"

    def argv(brief, workdir, out_file, role="coder"):
        return [sys.executable, str(FAKE), "honors_o", "-o", str(out_file) + ".last"]

    monkeypatch.setitem(PROVIDERS, "codex", {**PROVIDERS["codex"], "argv": argv})
    (tmp_path / "b").write_text("brief\n")  # codex reads the brief on stdin
    outcome = run_worker(
        "coder", "codex", str(tmp_path / "b"), str(tmp_path), out, timeout=5)
    assert outcome.kind == "done"
    log_text = Path(out).read_text()
    last_text = Path(last).read_text()
    assert last_text == "last-message-sidecar\n"
    assert last_text not in log_text
    assert '"type": "result"' in log_text
    assert Path(out).resolve() != Path(last).resolve()


def test_schema_in_assistant_prose_is_not_result(tmp_path, monkeypatch):
    line = json.dumps({
        "type": "assistant",
        "content": json.dumps({"status": "done", "reason": "", "summary": "ok"}),
    })
    for parser in (grok_events, claude_events, copilot_events):
        ev = parser(line)
        assert ev is not None
        assert ev.kind != "result"
    bare = grok_events('{"status":"done","reason":"","summary":"ok"}')
    assert bare is not None and bare.kind == "unknown"
    _install_fake(monkeypatch, "prose_result")
    out = str(tmp_path / "out")
    outcome = run_worker(
        "coder", "grok", str(tmp_path / "b"), str(tmp_path), out, timeout=5)
    assert outcome.kind == "invalid_result"


@pytest.mark.parametrize("provider", ["claude", "copilot"])
def test_type_result_without_schema_is_invalid(tmp_path, monkeypatch, provider):
    captured: dict = {}
    payload = (json.dumps({"type": "result", "status": "nope", "result": "ok"}) + "\n").encode()
    from test_workers import _pipe_popen
    monkeypatch.setattr(
        "ratchetloop.worker_run.subprocess.Popen",
        _pipe_popen(captured, payload),
    )
    brief = tmp_path / "b.md"
    brief.write_text("do it")
    outcome = run_worker(
        "coder", provider, str(brief), str(tmp_path), str(tmp_path / "out"), timeout=5)
    assert outcome.kind == "invalid_result"


def test_partial_line_silence_is_authoritative(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKER_SILENCE_S", "0.2")
    _install_fake(monkeypatch, "partial", ["30"])
    out = str(tmp_path / "out")
    t0 = time.time()
    outcome = run_worker(
        "coder", "grok", str(tmp_path / "b"), str(tmp_path), out,
        timeout=8, wall_budget_s=8)
    assert time.time() - t0 < 2.0
    assert outcome.kind == "silence"


def test_huge_line_is_protocol_error(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKER_SILENCE_S", "8")
    _install_fake(monkeypatch, "huge", ["8"])
    out = str(tmp_path / "out")
    outcome = run_worker(
        "coder", "grok", str(tmp_path / "b"), str(tmp_path), out,
        timeout=8, wall_budget_s=8)
    assert outcome.kind == "protocol_error"


def test_stderr_quota_event_is_not_parsed(tmp_path, monkeypatch):
    _install_fake(monkeypatch, "stderr_quota")
    out = str(tmp_path / "out")
    outcome = run_worker(
        "coder", "grok", str(tmp_path / "b"), str(tmp_path), out, timeout=5)
    assert outcome.kind == "done"
    log = Path(out).read_text()
    assert "err:" in log
    assert "429" in log


@pytest.mark.parametrize("quota,err,rc,status,expect", [
    (True, True, 0, "done", "quota"),
    (False, True, 0, "done", "error"),
    (False, True, 1, "done", "error"),
    (False, False, 7, "done", "error"),
    (False, False, 0, "blocked", "blocked"),
    (False, False, 0, "done", "done"),
    (False, False, 0, None, "invalid_result"),
])
def test_interpret_precedence_table(quota, err, rc, status, expect):
    result_ev = None if status is None else _result(status)
    error_ev = _error() if err else None
    got = interpret_run("grok", "log", rc, result_ev, error_ev, quota, {})
    assert got.kind == expect


def test_error_event_beats_later_result(tmp_path, monkeypatch):
    _install_fake(monkeypatch, "error_then_result")
    out = str(tmp_path / "out")
    outcome = run_worker(
        "coder", "grok", str(tmp_path / "b"), str(tmp_path), out, timeout=5)
    assert outcome.kind == "error"


def test_later_transient_does_not_drop_earlier_schema_error(tmp_path, monkeypatch):
    _install_fake(monkeypatch, "schema_then_reconnect_then_result")
    out = str(tmp_path / "out")
    outcome = run_worker(
        "coder", "grok", str(tmp_path / "b"), str(tmp_path), out, timeout=5)
    assert outcome.kind == "error"


def test_schema_error_plus_valid_result_at_rc0_is_error():
    raw = json.dumps({"type": "error", "message": "invalid_json_schema"})
    err = Event("error", "invalid_json_schema", raw)
    got = interpret_run("grok", "log", 0, _result(), err, False, {})
    assert got.kind == "error"


def test_reconnect_then_valid_result_at_live_rc0_is_done():
    raw = json.dumps({
        "type": "error", "message": "Reconnecting... (request timed out)",
    })
    err = Event("error", "Reconnecting... (request timed out)", raw, 1)
    res = _result(seq=2)
    got = interpret_run("grok", "log", 0, res, err, False, {})
    assert got.kind == "done"
    text = raw + "\n" + res.raw + "\n"
    got = interpret_run("grok", text, 0, res, err, False, {})
    assert got.kind == "done"


def test_transient_without_ordinals_is_not_recovered():
    raw = json.dumps({
        "type": "error", "message": "Reconnecting... (request timed out)",
    })
    err = Event("error", "Reconnecting... (request timed out)", raw)
    got = interpret_run("grok", "log", 0, _result(), err, False, {})
    assert got.kind == "error"


@pytest.mark.parametrize("msg", [
    "Reconnecting... (request timed out)",
    "transport-fallback",
])
def test_result_then_transient_error_at_live_rc0_is_error(msg):
    raw = json.dumps({"type": "error", "message": msg})
    err = Event("error", msg, raw, 2)
    res = _result(seq=1)
    text = res.raw + "\n" + raw + "\n"
    got = interpret_run("grok", text, 0, res, err, False, {})
    assert got.kind == "error"


def test_later_echo_of_result_does_not_recover_result_then_transient():
    raw = json.dumps({
        "type": "error", "message": "Reconnecting... (request timed out)",
    })
    res = _result(seq=1)
    err = Event("error", "Reconnecting... (request timed out)", raw, 2)
    text = res.raw + "\n" + raw + "\n" + res.raw + "\n"
    got = interpret_run("grok", text, 0, res, err, False, {})
    assert got.kind == "error"


@pytest.mark.parametrize("msg", [
    "invalid_json_schema: transport-fallback",
    "api_error: reconnecting",
    "API error: Reconnecting... (request timed out)",
    "invalid_request_error: reconnecting",
    "APIError: Reconnecting...",
    "bad_request_error: transport-fallback",
    "Reconnecting... APIError (request timed out)",
])
def test_schema_api_error_with_transient_text_is_error(msg):
    raw = json.dumps({"type": "error", "message": msg})
    err = Event("error", msg, raw, 1)
    res = _result(seq=2)
    got = interpret_run("grok", raw + "\n" + res.raw, 0, res, err, False, {})
    assert got.kind == "error"


@pytest.mark.parametrize("code", [
    "invalid_request_error",
    "APIError",
    "bad_request_error",
])
def test_schema_api_code_never_recovers_retry_text(code):
    msg = "Reconnecting... (request timed out)"
    raw = json.dumps({"type": "error", "code": code, "message": msg})
    err = Event("error", msg, raw, 1)
    res = _result(seq=2)
    got = interpret_run("grok", raw + "\n" + res.raw, 0, res, err, False, {})
    assert got.kind == "error"


def test_top_level_type_apierror_never_recovers_retry_text():
    msg = "Reconnecting... (request timed out)"
    raw = json.dumps({"type": "APIError", "is_error": True, "message": msg})
    err = Event("error", msg, raw, 1)
    res = _result(seq=2)
    got = interpret_run("grok", raw + "\n" + res.raw, 0, res, err, False, {})
    assert got.kind == "error"


def test_result_then_transient_error_live_is_error(tmp_path, monkeypatch):
    _install_fake(monkeypatch, "result_then_reconnect")
    out = str(tmp_path / "out")
    outcome = run_worker(
        "coder", "grok", str(tmp_path / "b"), str(tmp_path), out, timeout=5)
    assert outcome.kind == "error"


def test_outcome_from_log_does_not_assume_rc_0(tmp_path):
    from ratchetloop.worker_run import outcome_from_log
    out = tmp_path / "out"
    out.write_text(
        json.dumps({"type": "error", "message": "invalid_json_schema"}) + "\n"
        + json.dumps({
            "type": "result", "status": "done", "reason": "", "summary": "ok",
        }) + "\n")
    got = outcome_from_log("grok", str(out), grok_events)
    assert got is not None and got.kind == "error"


def test_outcome_from_log_does_not_recover_transient_without_live_rc(tmp_path):
    from ratchetloop.worker_run import outcome_from_log
    out = tmp_path / "out"
    out.write_text(
        json.dumps({
            "type": "error", "message": "Reconnecting... (request timed out)",
        }) + "\n"
        + json.dumps({
            "type": "result", "status": "done", "reason": "", "summary": "ok",
        }) + "\n")
    got = outcome_from_log("grok", str(out), grok_events)
    assert got is not None and got.kind == "error"


def test_outcome_payload_no_implicit_done():
    assert outcome_payload(None)["kind"] == "invalid_result"
    assert outcome_payload("hello")["kind"] == "invalid_result"
    assert outcome_payload({})["kind"] == "invalid_result"
    assert outcome_payload({"kind": None})["kind"] == "invalid_result"
    assert outcome_payload({"kind": "unknown"})["kind"] == "invalid_result"
    assert outcome_payload(WorkerOutcome(kind="done", text="x"))["kind"] == "done"


def test_legacy_raw_zero_needs_structured_result(tmp_path):
    out = str(tmp_path / "out")
    Path(out + ".exit").write_text("0\n")
    Path(out).write_text("plain\n")
    got = load_worker_outcome(out)
    assert got is not None and got.kind == "invalid_result"
    Path(out).write_text(json.dumps({
        "type": "result", "status": "done", "reason": "", "summary": "ok",
    }) + "\n")
    got = load_worker_outcome(out)
    assert got is not None and got.kind == "done"


def test_copilot_reviewer_denies_write(tmp_path):
    brief = tmp_path / "b.md"
    brief.write_text("review")
    argv = copilot_argv(str(brief), str(tmp_path), str(tmp_path / "out"), role="reviewer")
    assert "--deny-tool=write" in argv
    assert "--allow-tool=write" not in argv
    assert "--add-dir" in argv


def test_failed_kill_is_cleanup_failed(tmp_path, monkeypatch):
    proc = subprocess.Popen(
        ["sleep", "30"], start_new_session=True, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pgid = os.getpgid(proc.pid)
    real = os.killpg

    def fake_killpg(pg, sig):
        if sig == 0:
            return real(pg, 0)
        return None

    monkeypatch.setattr("ratchetloop.worker_proc.os.killpg", fake_killpg)
    (tmp_path / "out.pid").write_text(str(pgid))
    try:
        outcome = run_worker(
            "coder", "grok", str(tmp_path / "b"), str(tmp_path),
            str(tmp_path / "out"), timeout=5)
        assert outcome.kind == "cleanup_failed"
        real(pgid, 0)
    finally:
        try:
            real(pgid, 9)
        except OSError:
            pass
        proc.wait(timeout=5)


def test_codex_item_completed_agent_message_is_result():
    line = (
        '{"type":"item.completed","item":{"id":"item_0",'
        '"type":"agent_message","text":"ok"}}'
    )
    assert grok_events(line).kind == "progress"
    assert claude_events(line).kind == "progress"
    assert copilot_events(line).kind == "progress"
    assert codex_events(line).kind == "result"
    tool = '{"type":"item.completed","item":{"type":"command_execution"}}'
    assert codex_events(tool).kind == "progress"
    assert codex_events('{"type":"result"}').kind == "result"


def _run_codex_capture(tmp_path, monkeypatch, payload: bytes, last: str | None):
    captured: dict = {}
    monkeypatch.setattr(
        "ratchetloop.worker_run.subprocess.Popen",
        _pipe_popen(captured, payload),
    )
    brief = tmp_path / "b.md"
    brief.write_text("do it")
    out = str(tmp_path / "out")
    if last is not None:
        Path(out + ".last").write_text(last)
    return run_worker(
        "coder", "codex", str(brief), str(tmp_path), out, timeout=5)


def test_codex_last_message_schema_is_done(tmp_path, monkeypatch):
    outcome = _run_codex_capture(
        tmp_path, monkeypatch, CODEX_JSONL.read_bytes(), _SCHEMA)
    assert outcome.kind == "done"
    assert outcome.summary == "ok"


def test_codex_last_message_prose_is_invalid(tmp_path, monkeypatch):
    outcome = _run_codex_capture(
        tmp_path, monkeypatch, CODEX_JSONL.read_bytes(), "ok")
    assert outcome.kind == "invalid_result"


def test_kill_pg_polls_until_the_group_is_gone(monkeypatch):
    # Phase 1 spike, 2026-09-11: a real grok killed on silence was recorded cleanup_failed because
    # _kill_pg checked the group once, while members were still dying; it was dead moments later.
    from ratchetloop import worker_proc
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    real_alive = worker_proc._alive
    calls = {"n": 0}

    def still_dying(pgid):
        calls["n"] += 1
        return True if calls["n"] <= 3 else real_alive(pgid)

    monkeypatch.setattr(worker_proc, "_alive", still_dying)
    assert worker_proc._kill_pg(proc) is True
    assert calls["n"] > 3
    assert proc.poll() is not None


def test_kill_pg_kills_members_after_the_leader_is_reaped():
    # Review finding 8: with the leader reaped, getpgid failed and _kill_pg returned True while the
    # rest of the group lived.
    from ratchetloop import worker_proc
    proc = subprocess.Popen(["sh", "-c", "sleep 30 & exec true"], start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc.wait(timeout=5)
    assert worker_proc._alive(proc.pid)  # the background sleep is still in the group
    assert worker_proc._kill_pg(proc) is True
    assert not worker_proc._alive(proc.pid)


def test_later_partial_report_does_not_erase_reported_usage(tmp_path, monkeypatch):
    # Review finding 4: a result line with fewer numbers overwrote an earlier usage event.
    captured: dict = {}
    payload = (json.dumps({"type": "usage", "usage": {"input_tokens": 40, "output_tokens": 9},
                           "total_cost_usd": 0.3}) + "\n"
               + json.dumps({"type": "result", "status": "done", "reason": "", "summary": "ok",
                             "usage": {"input_tokens": 41}}) + "\n").encode()
    monkeypatch.setattr("ratchetloop.worker_run.subprocess.Popen", _pipe_popen(captured, payload))
    brief = tmp_path / "b.md"
    brief.write_text("do it")
    out = run_worker("coder", "grok", str(brief), str(tmp_path), str(tmp_path / "out"), timeout=5)
    assert out.kind == "done"
    assert (out.usage["tokens_in"], out.usage["tokens_out"], out.usage["cost_usd"]) == (41, 9, 0.3)


def test_wall_budget_is_its_own_kind(tmp_path, monkeypatch):
    # Review finding 1: a wall-budget stop was recorded as kind "error".
    monkeypatch.setenv("WORKER_SILENCE_S", "30")
    _install_fake(monkeypatch, "silence", ["30"])
    outcome = run_worker("coder", "grok", str(tmp_path / "b"), str(tmp_path),
                         str(tmp_path / "out"), timeout=0.5, wall_budget_s=30)
    assert (outcome.kind, outcome.reason) == ("timeout", "timeout")


def test_codex_item_text_schema_is_done(tmp_path, monkeypatch):
    payload = (json.dumps({
        "type": "item.completed",
        "item": {"id": "item_0", "type": "agent_message", "text": _SCHEMA},
    }) + "\n").encode()
    outcome = _run_codex_capture(tmp_path, monkeypatch, payload, None)
    assert outcome.kind == "done"
    assert outcome.summary == "ok"
