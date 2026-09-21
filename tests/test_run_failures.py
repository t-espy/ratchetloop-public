"""Every way a run ends other than ACCEPT (CONTRACT.md §6), with fake workers. result.json is written
on each path, a crash included."""
from __future__ import annotations

import pytest

from ratchetloop.cli import main
from ratchetloop.reviewers import cooled
from ratchetloop.worker_io import WorkerOutcome
from runkit import (
    MUL, done, events_of, install, make_repo, outcome, result_of, runs_root, verdict, write,
    write_task,
)


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path)


def _run(tmp_path, repo, **task) -> int:
    return main(["run", str(write_task(tmp_path, repo, **task))])


def _ended(disposition: str, reason: str) -> dict:
    res = result_of()
    assert (res["disposition"], res["reason"]) == (disposition, reason), res["detail"]
    return res


def test_coder_blocked_is_human_review(repo, tmp_path, monkeypatch):
    install(monkeypatch, [(None, outcome("blocked", "the brief names a missing file"))], [])
    assert _run(tmp_path, repo) == 3
    assert "missing file" in _ended("HUMAN_REVIEW", "coder_blocked")["detail"]


def test_write_outside_allowed_paths_is_a_scope_violation_and_the_tree_is_kept(repo, tmp_path, monkeypatch):
    install(monkeypatch, [(lambda t: (write("calc.py", MUL)(t), write("other.py", "x = 1\n")(t)), done())], [])
    assert _run(tmp_path, repo) == 4
    res = _ended("FAILED", "scope_violation")
    assert "other.py" in res["detail"] and res["worktree"]


def test_a_reviewer_that_writes_invalidates_its_review(repo, tmp_path, monkeypatch):
    install(monkeypatch, [(write("calc.py", MUL), done())],
            [(write("notes.txt", "looks fine\n"), verdict("APPROVE"))])
    assert _run(tmp_path, repo) == 4
    _ended("FAILED", "reviewer_wrote")


def test_a_blocked_reviewer_saying_approve_is_not_a_verdict(repo, tmp_path, monkeypatch):
    # predecessor item 90: status=blocked with reason=APPROVE was remapped to done and accepted.
    install(monkeypatch, [(write("calc.py", MUL), done())], [(None, outcome("blocked", "APPROVE"))])
    assert _run(tmp_path, repo) == 4
    _ended("FAILED", "reviewer_blocked")


def test_a_review_that_ran_on_the_coders_family_is_invalid(repo, tmp_path, monkeypatch):
    install(monkeypatch, [(write("calc.py", MUL), done())],
            [(None, verdict("APPROVE", model_reported="grok-4.6-build"))])
    assert _run(tmp_path, repo) == 4
    assert "coder's family" in _ended("FAILED", "invalid_result")["detail"]
    # A rejected review records no verdict (Phase 3 review, finding 9).
    assert [e["verdict"] for e in events_of() if e["event"] == "review"] == [None]


def test_a_copilot_review_that_reports_no_model_is_invalid(repo, tmp_path, monkeypatch):
    # copilot is the one CLI that can run another family (D27): with no model reported, the review's
    # family is unverifiable (grok project review, 2026-09-12, finding 3).
    policy = tmp_path / "WORKFLOW.md"
    policy.write_text("```yaml\nmodels:\n  ladders:\n    copilot:\n"
                      "      - {model: gemini-3.7-flash, family: google}\n```\n")
    install(monkeypatch, [(write("calc.py", MUL), done())], [(None, verdict("APPROVE"))])
    task = write_task(tmp_path, repo)
    assert main(["run", str(task), "--reviewer", "copilot", "--policy", str(policy)]) == 4
    assert "copilot reported no model" in _ended("FAILED", "invalid_result")["detail"]


def test_a_worker_that_rewrites_a_setup_output_is_out_of_scope(repo, tmp_path, monkeypatch):
    install(monkeypatch, [(lambda t: (write("calc.py", MUL)(t), write("data.txt", "forged\n")(t)),
                           done())], [])
    assert _run(tmp_path, repo, setup=["echo seed > data.txt"]) == 4
    assert "data.txt" in _ended("FAILED", "scope_violation")["detail"]


@pytest.mark.parametrize("kind,reason", [
    ("invalid_result", "invalid_result"), ("protocol_error", "invalid_result"),
    ("error", "worker_error"), ("silence", "worker_error"), ("timeout", "budget_wall"),
])
def test_coder_outcomes_map_to_failed_reasons(repo, tmp_path, monkeypatch, kind, reason):
    install(monkeypatch, [(None, outcome(kind))], [])
    assert _run(tmp_path, repo) == 4
    _ended("FAILED", reason)


def test_coder_quota_fails_and_cools_the_provider(repo, tmp_path, monkeypatch):
    install(monkeypatch, [(None, outcome("quota"))], [])
    assert _run(tmp_path, repo) == 4
    _ended("FAILED", "quota_exhausted")
    assert "grok" in cooled(runs_root())


def test_no_reviewer_left_after_quota_fails(repo, tmp_path, monkeypatch):
    quota = WorkerOutcome(kind="quota", reason="quota")
    install(monkeypatch, [(write("calc.py", MUL), done())],
            [(None, quota), (None, quota), (None, quota)])
    assert _run(tmp_path, repo) == 4
    _ended("FAILED", "quota_exhausted")


def test_wall_budget_stops_before_any_launch(repo, tmp_path, monkeypatch):
    fake = install(monkeypatch, [], [])
    assert _run(tmp_path, repo, wall_budget_s=0) == 4
    _ended("FAILED", "budget_wall")
    assert fake.calls == []


def test_cost_budget_stops_at_the_next_boundary(repo, tmp_path, monkeypatch):
    fake = install(monkeypatch, [(write("calc.py", MUL), done(cost_usd=0.05))], [])
    assert _run(tmp_path, repo, cost_budget_usd=0.01) == 4
    _ended("FAILED", "budget_cost")
    assert fake.roles() == ["coder"]


def test_stop_sentinel_stops_the_run(repo, tmp_path, monkeypatch):
    fake = install(monkeypatch, [], [])
    runs_root().mkdir(parents=True, exist_ok=True)
    (runs_root() / "STOP").touch()
    assert _run(tmp_path, repo) == 5
    _ended("STOPPED", "stop_sentinel")
    assert fake.calls == []


def test_setup_failure_ends_the_run(repo, tmp_path, monkeypatch):
    install(monkeypatch, [], [])
    assert _run(tmp_path, repo, setup=["true", "exit 3"]) == 4
    assert _ended("FAILED", "setup_failed")["detail"] == "exit 3"


def test_a_coder_that_changes_nothing_fails(repo, tmp_path, monkeypatch):
    install(monkeypatch, [(None, done())], [])
    assert _run(tmp_path, repo) == 4
    _ended("FAILED", "no_change")


def test_a_crash_still_writes_the_result_and_is_re_raised(repo, tmp_path, monkeypatch):
    def boom(tree):
        raise RuntimeError("adapter exploded")
    install(monkeypatch, [(boom, done())], [])
    with pytest.raises(RuntimeError, match="adapter exploded"):
        _run(tmp_path, repo)
    res = _ended("FAILED", "crash")
    assert "adapter exploded" in res["detail"]
    assert res["usage"]["launches"] == []


def test_the_result_is_written_even_when_building_it_fails(repo, tmp_path, monkeypatch):
    # Phase 3 review, finding 1: nothing in _close before the write may lose result.json.
    def broken(st):
        raise RuntimeError("numstat exploded")
    monkeypatch.setattr("ratchetloop.run.build_result", broken)
    install(monkeypatch, [(write("calc.py", MUL), done())], [(None, verdict("APPROVE"))])
    assert _run(tmp_path, repo) == 0
    res = _ended("ACCEPT", "approved")
    assert "numstat exploded" in res["detail"] and res["head_sha"]
    assert not list(runs_root().glob("add-mul/*/lock"))
