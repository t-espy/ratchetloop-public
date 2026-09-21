"""Model choice per launch: CLI flags, run_worker forwarding and events, policy file, prior failures."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ratchetloop import model_choice
from ratchetloop.events import EventSink, read_events
from ratchetloop.policy import PolicyError
from ratchetloop.workers import (
    PROVIDERS, claude_argv, codex_argv, copilot_argv, grok_argv, model_flags,
    outcome_payload, run_worker,
)

FAKE = Path(__file__).resolve().parent / "fake_struct_worker.py"
META = {"model": "terra", "effort": "medium", "tier": "standard", "rung": 1,
        "family": "openai", "select_reason": "default for coder"}
POLICY = ("# policy\n\n```yaml\nmodels:\n  ladders:\n    codex:\n"
          "      - {model: luna, effort: low}\n      - {model: terra}\n"
          "      - {model: sol, effort: high}\n```\n")


@pytest.mark.parametrize("provider,expected", [
    ("grok", ["-m", "M", "--reasoning-effort", "low"]),
    ("codex", ["-m", "M", "-c", 'model_reasoning_effort="low"']),
    ("claude", ["--model", "M", "--effort", "low"]),
    ("copilot", ["--model", "M", "--effort", "low"]),
])
def test_model_flags_per_provider(provider, expected):
    assert model_flags(provider, "M", "low") == expected
    assert model_flags(provider, None, None) == []


def test_builders_append_flags_only_when_chosen(tmp_path):
    brief = tmp_path / "b.md"
    brief.write_text("do it")
    args = (str(brief), str(tmp_path), str(tmp_path / "out"))
    cases = [
        (grok_argv, ["-m", "g", "--reasoning-effort", "high"]),
        (codex_argv, ["-m", "g", "-c", 'model_reasoning_effort="high"']),
        (claude_argv, ["--model", "g", "--effort", "high"]),
        (copilot_argv, ["--model", "g", "--effort", "high"]),
    ]
    for builder, tail in cases:
        plain = builder(*args)
        chosen = builder(*args, model="g", effort="high")
        assert chosen == plain + tail, builder.__name__
        assert "-m" not in plain and "--model" not in plain


def _capture_builder(monkeypatch, seen: list):
    def argv(brief, workdir, out_file, role="coder", **kw):
        seen.append(kw)
        return [sys.executable, str(FAKE)]
    monkeypatch.setitem(PROVIDERS, "grok", {**PROVIDERS["grok"], "argv": argv})


@pytest.mark.parametrize("meta,expected", [
    (None, {}),
    ({"model": None, "family": "xai", "select_reason": "no models policy"}, {}),
    ({"model": "grok-4.6", "effort": "low", "family": "xai"},
     {"model": "grok-4.6", "effort": "low"}),
])
def test_run_worker_forwards_model_only_when_chosen(tmp_path, monkeypatch, meta, expected):
    seen: list = []
    _capture_builder(monkeypatch, seen)
    brief = tmp_path / "b.md"
    brief.write_text("x")
    out = run_worker("coder", "grok", str(brief), str(tmp_path), str(tmp_path / "out"),
                     timeout=10, model_meta=meta)
    assert out.kind == "done"
    assert seen == [expected]


def test_run_worker_emits_start_and_end_events(tmp_path, monkeypatch):
    _capture_builder(monkeypatch, [])
    sink = EventSink(tmp_path / "run" / "events.jsonl", "r1")
    brief = tmp_path / "b.md"
    brief.write_text("x")
    out = run_worker("coder", "grok", str(brief), str(tmp_path), str(tmp_path / "out"),
                     timeout=10, attempt=2, model_meta=META, sink=sink)
    start, end = read_events(tmp_path / "run" / "events.jsonl")
    assert (start["event"], start["role"], start["round"], start["provider"]) == (
        "worker_start", "coder", 2, "grok")
    assert (start["model"], start["effort"], start["family"]) == ("terra", "medium", "openai")
    assert start["out_file"] == str(tmp_path / "out")
    assert (end["event"], end["kind"], end["round"]) == ("worker_end", "done", 2)
    assert end["usage_source"] in ("measured", "unknown")
    assert "findings" not in end  # coders report no findings
    assert outcome_payload(out)["usage"]["model_choice"] == META


def test_reused_outcome_is_recorded_as_reused(tmp_path, monkeypatch):
    out = tmp_path / "out.txt"
    out.write_text("hello\n")
    (tmp_path / "out.txt.exit").write_text(
        json.dumps({"exit": 0, "kind": "done", "reason": "", "summary": "ok"}) + "\n")

    def boom(*a, **k):
        raise AssertionError("must not launch")

    monkeypatch.setattr("ratchetloop.worker_run.subprocess.Popen", boom)
    sink = EventSink(tmp_path / "events.jsonl", "r1")
    got = run_worker("reviewer", "codex", str(tmp_path / "b"), str(tmp_path), str(out), sink=sink)
    assert got.kind == "done"
    (end,) = read_events(tmp_path / "events.jsonl")
    assert end["event"] == "worker_end" and end["reused"] is True
    assert end["findings"] is None


def test_policy_sources_in_order(tmp_path, monkeypatch):
    assert model_choice.policy_models() is None  # nothing configured
    default = model_choice.default_policy_path()
    assert default == tmp_path / "config" / "ratchetloop" / "WORKFLOW.md"
    default.parent.mkdir(parents=True)
    default.write_text(POLICY)
    assert model_choice.policy_models()["ladders"]["codex"][1] == {
        "model": "terra", "family": "openai"}
    monkeypatch.setenv("RATCHETLOOP_POLICY", str(tmp_path / "missing.md"))
    with pytest.raises(PolicyError, match="not found"):
        model_choice.policy_models()
    explicit = tmp_path / "p.md"
    explicit.write_text(POLICY.replace("effort: high", "effort: turbo"))
    with pytest.raises(PolicyError):
        model_choice.policy_models(explicit)


def test_diff_lines_skips_file_headers():
    diff = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1,2 @@\n-a\n+b\n+c\n keep\n"
    assert model_choice.diff_lines(diff) == 3


def _run(task_runs: Path, name: str, *events: tuple[str, dict]) -> None:
    sink = EventSink(task_runs / name / "events.jsonl", name)
    for event, fields in events:
        sink.emit(event, **fields)


def test_prior_failures_counts_runs_not_events(tmp_path):
    runs = tmp_path / "add-mul"
    _run(runs, "r1", ("worker_end", {"role": "coder", "kind": "blocked"}),
         ("worker_end", {"role": "coder", "kind": "invalid_result"}))
    _run(runs, "r2", ("worker_end", {"role": "coder", "kind": "done"}),
         ("checks", {"passed": False}))
    _run(runs, "r3", ("worker_end", {"role": "coder", "kind": "quota"}))
    _run(runs, "r4", ("worker_end", {"role": "reviewer", "kind": "invalid_result"}))
    # D14's red fixed by the one fix launch is not a failure (Phase 3 review, finding 6).
    _run(runs, "r5", ("checks", {"passed": False}), ("checks", {"passed": True}))
    assert model_choice.prior_failures(runs, "coder") == 2
    assert model_choice.prior_failures(runs, "reviewer") == 1
    assert model_choice.prior_failures(tmp_path / "none", "coder") == 0
    assert "quota" not in model_choice.ESCALATE_KINDS


def test_for_launch_without_policy_keeps_defaults():
    meta = model_choice.for_launch("coder", "codex")
    assert meta["model"] is None and meta["select_reason"] == "no models policy"
    assert meta["family"] == "openai"
    assert model_choice.for_launch("reviewer", "copilot")["family"] is None


def test_for_launch_climbs_on_prior_failures_and_sizes_reviews(tmp_path):
    policy = tmp_path / "p.md"
    policy.write_text(POLICY)
    runs = tmp_path / "task"
    _run(runs, "r1", ("worker_end", {"role": "coder", "kind": "blocked"}))
    coder = model_choice.for_launch("coder", "codex", task_runs=runs, policy_path=policy)
    assert (coder["model"], coder["rung"], coder["family"]) == ("sol", 2, "openai")
    review = model_choice.for_launch("reviewer", "codex", task_runs=runs, diff="+a\n-b\n",
                                     policy_path=policy)
    assert (review["model"], review["tier"]) == ("luna", "light")
