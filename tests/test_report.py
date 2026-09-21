"""`status` and `stats` (CONTRACT.md §1) over run directories written by hand: the totals, each
breakdown, the filters, and ideas reported beside the totals rather than counted twice."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ratchetloop.lifecycle import write_lock
from ratchetloop.report import render, results, stats, status


def _launch(role, provider, family, tin, tout, cost, source="measured"):
    return {"role": role, "provider": provider, "family": family, "tokens_in": tin,
            "tokens_out": tout, "cost_usd": cost, "usage_source": source}


def _result(key, run_id, *, kind="code", disposition="ACCEPT", repo="/r/a", launches=(),
            started="2026-09-11T10:00:00Z", lines=0, tests=0, rounds=1, findings=None):
    return {"task_key": key, "run_id": run_id, "kind": kind, "disposition": disposition,
            "repo": repo, "started": started, "usage": {"launches": list(launches)},
            "velocity": {"lines_added": lines, "tests_added": tests, "review_rounds": rounds,
                         "findings": findings or {"bug": 0, "suggestion": 0, "nit": 0}}}


def _write(root: Path, key: str, run_id: str, result: dict | None, events=()) -> Path:
    run_dir = root / key / run_id
    run_dir.mkdir(parents=True)
    if result is not None:
        (run_dir / "result.json").write_text(json.dumps(result))
    if events:
        (run_dir / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    return run_dir


@pytest.fixture
def root(tmp_path) -> Path:
    root = tmp_path / "runs"
    _write(root, "add-mul", "20260911T100000Z", _result(
        "add-mul", "20260911T100000Z", lines=10, tests=2,
        findings={"bug": 1, "suggestion": 0, "nit": 1},
        launches=[_launch("coder", "grok", "xai", 100, 10, 0.02),
                  _launch("reviewer", "codex", "openai", 50, 5, None)]))
    _write(root, "plan-div", "20260911T110000Z", _result(
        "plan-div", "20260911T110000Z", kind="doc", repo="/r/b", lines=30,
        launches=[_launch("coder", "codex", "openai", 80, 8, None),
                  _launch("reviewer", "claude", "anthropic", 70, 7, 0.08)]))
    _write(root, "add-div", "20260912T090000Z", _result(
        "add-div", "20260912T090000Z", disposition="FAILED", started="2026-09-12T09:00:00Z",
        launches=[_launch("coder", "grok", "xai", 40, 4, 0.01, source="unknown")]))
    _write(root, "calc", "20260912T100000Z", {
        "idea_key": "calc", "run_id": "20260912T100000Z", "kind": "idea", "disposition": "ACCEPT",
        "started": "2026-09-12T10:00:00Z", "stages": [{}, {}, {}],
        "usage": {"cost_usd": 9.99, "launches": []}, "velocity": {"phases_accepted": 2}})
    (root / ".locks").mkdir()
    return root


def test_totals_count_each_task_launch_once_and_ideas_beside_them(root):
    report = stats(results(root))
    t = report["totals"]
    assert (t["runs"], t["accepted"], t["launches"]) == (3, 2, 5)
    assert (t["tokens_in"], t["tokens_out"]) == (300, 30)  # the unknown launch is not summed
    assert t["cost_usd"] == pytest.approx(0.10) and t["launches_with_unknown_usage"] == 1
    assert t["launches_without_cost"] == 2  # the codex launches: tokens measured, no cost
    assert t["dispositions"] == {"ACCEPT": 2, "FAILED": 1}
    assert t["cost_per_accepted_usd"] == pytest.approx(0.05)
    assert t["findings"] == {"bug": 1, "suggestion": 0, "nit": 1}
    assert t["findings_per_review"] == 0.667  # 2 findings over 3 review rounds, rounded
    assert t["tests_per_100_lines"] == 20.0  # code runs only: 2 tests over 10 lines
    assert report["ideas"] == {"calc": {"disposition": "ACCEPT", "stage": None, "stages": 3,
                                        "phases_accepted": 2, "phases_carried": 0,
                                        "residuals": 0, "open_residuals": 0, "cost_usd": 9.99}}


def test_breakdowns_by_provider_family_kind_and_repo(root):
    report = stats(results(root))
    assert report["by_provider"]["grok"]["launches"] == 2
    assert report["by_provider"]["grok"]["cost_usd"] == pytest.approx(0.02)
    assert report["by_family"]["openai"]["tokens_in"] == 130
    doc = report["by_kind"]["doc"]
    assert (doc["runs"], doc["accepted"], doc["launches"], doc["tokens_in"]) == (1, 1, 2, 150)
    assert doc["cost_usd"] == pytest.approx(0.08) and doc["launches_without_cost"] == 1
    assert report["by_repo"]["/r/b"]["cost_usd"] == pytest.approx(0.08)
    assert "by provider" in render(report) and "idea calc: ACCEPT" in render(report)


def test_filters_by_start_and_by_repository(root, tmp_path):
    assert [r.get("task_key") for r in results(root, since="2026-09-12")] == ["add-div", None]
    here = tmp_path / "a-repo"
    here.mkdir()
    _write(root, "local", "20260913T000000Z", _result("local", "20260913T000000Z",
                                                      repo=str(here.resolve())))
    # An idea carries its repo too, or `--repo` dropped every idea (Phase 6 review, finding 1).
    _write(root, "zidea", "20260913T000000Z", {"idea_key": "zidea", "kind": "idea",
                                               "disposition": "ACCEPT", "repo": str(here.resolve())})
    assert [r["kind"] for r in results(root, repo=here)] == ["code", "idea"]
    assert list(stats(results(root, repo=here))["ideas"]) == ["zidea"]


def test_review_records_add_spend_but_not_tasks(root):
    # Phase 6 review, finding 5: a re-review counted as another accepted task.
    _write(root, "add-mul.review", "20260913T000000Z", {
        **_result("add-mul", "20260913T000000Z",
                  launches=[_launch("reviewer", "claude", "anthropic", 30, 3, 0.05)]),
        "kind": "review"})
    report = stats(results(root))
    t = report["totals"]
    assert (t["runs"], t["accepted"], t["launches"]) == (3, 2, 6)
    assert t["cost_usd"] == pytest.approx(0.15)
    assert (report["by_kind"]["review"]["runs"], report["by_kind"]["review"]["launches"]) == (1, 1)


def test_status_shows_the_record_or_the_last_event_of_a_run_without_one(root):
    assert status(root, "add-mul")["disposition"] == "ACCEPT"
    assert status(root, "nope") is None
    run_dir = _write(root, "add-mul", "20260913T000000Z", None,
                     events=[{"event": "admit"}, {"event": "worker_start", "role": "coder"}])
    (run_dir / "lock").write_text(f"{os.getpid()} none 0\n")
    dead = status(root, "add-mul")
    assert dead["last_event"]["event"] == "worker_start" and dead["live"] is False
    write_lock(run_dir)  # this very process: alive
    assert status(root, "add-mul")["live"] is True
    assert status(root, "add-mul", "20260911T100000Z")["run_id"] == "20260911T100000Z"
