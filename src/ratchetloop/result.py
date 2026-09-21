"""result.json (CONTRACT.md §5): written on every exit path. Usage comes from the run's worker_end
events; velocity from git and the run state (§5.1)."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import gitops
from .events import read_events
from .worker_io import atomic_write

# predecessor's `--from-git` heuristic (`^\+\s*def test_`), plus JS/TS it()/test() and the C# test
# attributes of xunit, NUnit and MSTest — one per test method, not per data row (D26; a C# build's 47
# tests counted as none, 2026-09-12).
TEST_LINE = re.compile(
    r"^\+\s*(?:(?:async\s+)?def test_|(?:it|test)\(|\[(?:Fact|Theory|Test|TestMethod)\b)")
TEST_SUFFIXES = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".cs")
LAUNCH_KEYS = ("role", "round", "provider", "model", "effort", "family", "model_reported", "wall_s",
               "tokens_in", "tokens_cached", "tokens_out", "cost_usd", "ai_credits", "usage_source")


def numstat(repo: Path, base: str, head: str) -> tuple[int, int, int]:
    """(lines added, lines removed, files changed) between two commits; binary files count 0 lines."""
    out = gitops.git_try("diff", "--numstat", f"{base}..{head}", cwd=repo) or ""
    added = removed = files = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        files += 1
        added += int(parts[0]) if parts[0].isdigit() else 0
        removed += int(parts[1]) if parts[1].isdigit() else 0
    return added, removed, files


def tests_added(repo: Path, base: str, head: str) -> int | None:
    """Added test methods in Python, JS/TS or C# files; None when the change touched none."""
    names = (gitops.git_try("diff", "--name-only", f"{base}..{head}", cwd=repo) or "").splitlines()
    targets = [n for n in names if n.endswith(TEST_SUFFIXES)]
    if not targets:
        return None
    diff = gitops.git_try("diff", f"{base}..{head}", "--", *targets, cwd=repo) or ""
    return sum(1 for line in diff.splitlines() if TEST_LINE.match(line))


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def usage_block(ends: list[dict[str, Any]]) -> dict[str, Any]:
    launches = [{k: e.get(k) for k in LAUNCH_KEYS} for e in ends]
    known = [ln for ln in launches if ln["usage_source"] in ("measured", "estimated")]

    def total(key: str) -> float:
        return sum(ln[key] for ln in known if _number(ln[key]))

    return {"launches": launches, "tokens_in": total("tokens_in"),
            "tokens_out": total("tokens_out"), "cost_usd": round(total("cost_usd"), 6),
            "ai_credits": total("ai_credits"),
            "launches_with_unknown_usage": len(launches) - len(known)}


def attempt_number(task_runs: Path, run_id: str) -> int:
    earlier = [p for p in task_runs.iterdir() if p.is_dir() and p.name != run_id] \
        if task_runs.is_dir() else []
    return 1 + len(earlier)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _who(provider: str, meta: dict[str, Any]) -> dict[str, Any]:
    return {"provider": provider, **{k: meta.get(k) for k in ("model", "effort", "family")}}


def build_result(st: Any) -> dict[str, Any]:
    ends = [e for e in read_events(st.run_dir / "events.jsonl") if e.get("event") == "worker_end"]
    repo, base, head = st.task.repo, st.base_sha, st.head_sha or st.base_sha
    added = removed = files = 0
    tests = None
    if base and head:
        added, removed, files = numstat(repo, base, head)
        tests = tests_added(repo, base, head) if st.task.kind == "code" else None
    now = time.time()
    return {
        "task_key": st.task.task_key, "run_id": st.run_id, "kind": st.task.kind,
        "attempt": attempt_number(st.task_runs, st.run_id),
        "disposition": st.disposition, "reason": st.reason, "detail": st.detail,
        "repo": str(repo), "branch": st.branch, "base_sha": base or None, "head_sha": head or None,
        "coder": _who(st.coder, st.coder_meta), "reviewer": _who(st.reviewer, st.reviewer_meta),
        "checks": {"passed": st.checks_passed, "head_sha": st.checks_head or None},
        "review_summary": st.review_summary, "review_blocking": st.review_blocking,
        "worktree": st.worktree_kept,
        "usage": usage_block(ends),
        "velocity": {
            "lines_added": added, "lines_removed": removed, "files_changed": files,
            "tests_added": tests, "tests_added_method": "heuristic" if tests is not None else None,
            "review_rounds": st.review_rounds, "findings": st.last_findings,
            "checks_wall_s": round(st.checks_wall_s, 3), "wall_s": round(now - st.started, 3),
        },
        "started": _iso(st.started), "ended": _iso(now),
    }


def write_result(path: Path, result: dict[str, Any]) -> None:
    atomic_write(Path(path), json.dumps(result, indent=1, default=str) + "\n")
