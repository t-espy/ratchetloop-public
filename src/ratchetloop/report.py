"""`ratchetloop status` and `ratchetloop stats` (CONTRACT.md §1, D24): readers of run directories.
Nothing here writes, and there is no ledger besides the result.json files. Runs, acceptances and
velocity count task runs (`code`, `doc`); spend also counts review records (`review`), which are not
tasks. An idea's usage is already the sum of its stages' own results, so ideas are reported beside
the totals, never added to them."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .events import read_events
from .lifecycle import LOCK, lock_alive
from .resume import run_dirs

FINDING_TAGS = ("bug", "suggestion", "nit")


def _read(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def status(root: Path, key: str, run_id: str | None = None) -> dict[str, Any] | None:
    """The latest (or the named) run of a task or idea: its result.json, or for a run without one
    its last event and whether its process still lives."""
    dirs = [d for d in run_dirs(Path(root) / key) if run_id in (None, d.name)]
    if not dirs:
        return None
    run_dir = dirs[-1]
    result = _read(run_dir / "result.json")
    if result is not None:
        if result.get("kind") == "idea":  # §9.5: what was carried and what is still open
            result["carried_stages"] = [s.get("task_key") for s in result.get("stages") or []
                                        if s.get("carried")]
            result["open_residuals"] = [r for r in result.get("residuals") or []
                                        if not r.get("resolved_by")]
        return result
    events = read_events(run_dir / "events.jsonl")
    return {"key": key, "run_id": run_dir.name,
            "live": (run_dir / LOCK).exists() and lock_alive(run_dir),
            "last_event": events[-1] if events else None}


def results(root: Path, *, since: str | None = None, repo: Path | None = None) -> list[dict]:
    """Every finished run's result.json under `root`, oldest first; `since` compares with
    `started` (an ISO date or time), `repo` with the target repository."""
    root, want = Path(root), str(Path(repo).expanduser().resolve()) if repo else None
    out = []
    for key_dir in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")) \
            if root.is_dir() else []:
        for run_dir in run_dirs(key_dir):
            r = _read(run_dir / "result.json")
            if not r or not r.get("disposition"):
                continue
            if since and str(r.get("started") or "") < since:
                continue
            if want and r.get("repo") != want:
                continue
            out.append(r)
    return out


def _bucket() -> dict[str, Any]:
    return {"runs": 0, "accepted": 0, "launches": 0, "tokens_in": 0, "tokens_out": 0,
            "cost_usd": 0.0, "launches_with_unknown_usage": 0, "launches_without_cost": 0}


def _num(value: Any) -> float:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _add_launch(bucket: dict[str, Any], launch: dict[str, Any]) -> None:
    bucket["launches"] += 1
    if launch.get("usage_source") in ("measured", "estimated"):
        for key in ("tokens_in", "tokens_out", "cost_usd"):
            bucket[key] += _num(launch.get(key))
        cost = launch.get("cost_usd")
        if not isinstance(cost, (int, float)) or isinstance(cost, bool):
            # Tokens measured, cost not reported (codex): cost_usd leaves it out, so the total
            # says so (Phase 6 review, finding 2).
            bucket["launches_without_cost"] += 1
    else:
        bucket["launches_with_unknown_usage"] += 1


def _rounded(bucket: dict[str, Any]) -> dict[str, Any]:
    return {**bucket, "cost_usd": round(bucket["cost_usd"], 6)}


def stats(runs: list[dict[str, Any]]) -> dict[str, Any]:
    tasks = [r for r in runs if r.get("kind") in ("code", "doc")]
    total = _bucket()
    by = {name: defaultdict(_bucket) for name in ("provider", "family", "kind", "repo")}
    dispositions: dict[str, int] = defaultdict(int)
    findings = dict.fromkeys(FINDING_TAGS, 0)
    rounds = lines = tests = 0
    for r in tasks:
        accepted = r.get("disposition") == "ACCEPT"
        dispositions[r["disposition"]] += 1
        for bucket in (total, by["kind"][r["kind"]], by["repo"][str(r.get("repo"))]):
            bucket["runs"] += 1
            bucket["accepted"] += accepted
        for launch in (r.get("usage") or {}).get("launches") or []:
            for bucket in (total, by["kind"][r["kind"]], by["repo"][str(r.get("repo"))],
                           by["provider"][str(launch.get("provider"))],
                           by["family"][str(launch.get("family"))]):
                _add_launch(bucket, launch)
        vel = r.get("velocity") or {}
        rounds += _num(vel.get("review_rounds"))
        for tag in FINDING_TAGS:
            findings[tag] += _num((vel.get("findings") or {}).get(tag))
        if r["kind"] == "code":
            lines += _num(vel.get("lines_added"))
            tests += _num(vel.get("tests_added"))
    for r in (r for r in runs if r.get("kind") == "review"):  # spend, not tasks (review, 5)
        by["kind"]["review"]["runs"] += 1
        by["kind"]["review"]["accepted"] += r.get("disposition") == "ACCEPT"
        for launch in (r.get("usage") or {}).get("launches") or []:
            for bucket in (total, by["kind"]["review"], by["repo"][str(r.get("repo"))],
                           by["provider"][str(launch.get("provider"))],
                           by["family"][str(launch.get("family"))]):
                _add_launch(bucket, launch)
    accepted = total["accepted"]
    ideas: dict[str, dict[str, Any]] = {}
    for r in runs:
        if r.get("kind") == "idea":  # latest build of each idea wins
            ideas[str(r.get("idea_key"))] = {
                "disposition": r.get("disposition"), "stage": r.get("stage"),
                "stages": len(r.get("stages") or []),
                "phases_accepted": (r.get("velocity") or {}).get("phases_accepted"),
                "phases_carried": sum(bool(s.get("carried")) for s in r.get("stages") or []),
                "residuals": len(r.get("residuals") or []),
                "open_residuals": sum(not x.get("resolved_by") for x in r.get("residuals") or []),
                "cost_usd": (r.get("usage") or {}).get("cost_usd")}
    return {
        "totals": {**_rounded(total), "dispositions": dict(dispositions),
                   "cost_per_accepted_usd": round(total["cost_usd"] / accepted, 6) if accepted else None,
                   "tokens_per_accepted": round((total["tokens_in"] + total["tokens_out"]) / accepted)
                   if accepted else None,
                   "review_rounds": rounds, "findings": findings,
                   "findings_per_review": round(sum(findings.values()) / rounds, 3) if rounds else None,
                   "tests_per_100_lines": round(100 * tests / lines, 2) if lines else None},
        **{f"by_{name}": {k: _rounded(v) for k, v in sorted(groups.items())}
           for name, groups in by.items()},
        "ideas": ideas,
    }


def _table(title: str, groups: dict[str, dict[str, Any]]) -> list[str]:
    rows = [f"{title:<34} {'runs':>5} {'acc':>4} {'launch':>6} {'tokens in':>11} {'out':>8} "
            f"{'cost $':>9} {'unk':>4}"]
    for name, b in groups.items():
        rows.append(f"{name[-34:]:<34} {b['runs']:>5} {b['accepted']:>4} {b['launches']:>6} "
                    f"{b['tokens_in']:>11} {b['tokens_out']:>8} {b['cost_usd']:>9.4f} "
                    f"{b['launches_with_unknown_usage']:>4}")
    return rows


def render(report: dict[str, Any]) -> str:
    t = report["totals"]
    lines = [f"task runs {t['runs']}, accepted {t['accepted']}; launches {t['launches']} "
             f"({t['launches_with_unknown_usage']} with unknown usage, "
             f"{t['launches_without_cost']} without a reported cost); tokens {t['tokens_in']} in, "
             f"{t['tokens_out']} out; ${t['cost_usd']:.4f} measured",
             f"dispositions {t['dispositions']}; cost per accepted ${t['cost_per_accepted_usd']}; "
             f"findings per review {t['findings_per_review']}; tests per 100 lines "
             f"{t['tests_per_100_lines']}", ""]
    for name in ("provider", "family", "kind", "repo"):
        lines += _table(f"by {name}", report[f"by_{name}"]) + [""]
    for key, idea in report["ideas"].items():
        lines.append(f"idea {key}: {idea['disposition']}, {idea['stages']} stages, "
                     f"{idea['phases_accepted']} phases accepted, {idea['phases_carried']} carried "
                     f"({idea['open_residuals']} of {idea['residuals']} residuals open), "
                     f"${idea['cost_usd']}")
    return "\n".join(lines).rstrip() + "\n"
