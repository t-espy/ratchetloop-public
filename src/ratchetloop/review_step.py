"""The review round (CONTRACT.md §4): a reviewer from another model family reads the committed change
read-only. Its verdict is the result object's `reason` and nothing else (D8). A blocked reviewer is
never a verdict (predecessor item 90); a reviewer that changed the tree invalidates its own review; a
quota failure moves on to the next reviewer that is still of another family (D19)."""
from __future__ import annotations

from typing import Any

from . import gitops, workers
from .model_choice import for_launch
from .model_policy import family_of_model
from .reviewers import pick, record_quota
from .scope import check, snapshot, violation
from .state import Finish, RunState
from .steps import account, guard, launch_budget
from .worker_brief import assemble_brief, reviewer_task_brief

REVIEWER_FAILED = {"invalid_result": "invalid_result", "protocol_error": "invalid_result",
                   "timeout": "budget_wall"}
VERDICTS = ("APPROVE", "REQUEST_CHANGES")


def _meta(st: RunState, provider: str, diff: str | None) -> dict[str, Any]:
    return for_launch("reviewer", provider, spec=st.spec, task_runs=st.task_runs, diff=diff,
                      policy_path=st.policy_path)


def _next_reviewer(st: RunState) -> str:
    st.reviewers_tried.add(st.reviewer)
    nxt = pick(st.coder_family, lambda p: _meta(st, p, None)["family"],
               runs_root=st.runs_root, exclude=st.reviewers_tried, avoid=st.avoid_families)
    if nxt is None:
        raise Finish("FAILED", "quota_exhausted", "no reviewer of another family left")
    return nxt


def _rejected(st: RunState, out: Any, after: dict[str, Any], unchanged: bool,
              verdict: str | None) -> Finish | None:
    """Why this launch's review cannot stand, or None (a quota outcome is not a rejection)."""
    if not unchanged:
        return Finish("FAILED", "reviewer_wrote",
                      violation(after) or "changed: " + ", ".join(after["changed"]))
    if out.kind == "quota":
        return None
    if out.kind == "blocked":
        return Finish("FAILED", "reviewer_blocked", out.reason)
    if out.kind != "done":
        return Finish("FAILED", REVIEWER_FAILED.get(out.kind, "worker_error"), out.reason)
    reported = (out.usage or {}).get("model_reported")
    if reported is None and st.reviewer == "copilot":  # the one CLI that runs other families (D27)
        return Finish("FAILED", "invalid_result", "copilot reported no model; its family is unverified")
    ran = family_of_model(reported)
    if ran is not None and ran in st.avoid_families:
        return Finish("FAILED", "invalid_result", f"the review ran on the coder's family ({ran})")
    if verdict not in VERDICTS:
        return Finish("FAILED", "invalid_result", f"verdict {verdict!r} is not {VERDICTS}")
    return None


def review(st: RunState, round_: int, checks: list[dict[str, Any]]) -> str:
    """Run the round's review; return APPROVE or REQUEST_CHANGES, or end the run."""
    diff = gitops.git("diff", f"{st.base_sha}..HEAD", cwd=st.tree)
    while True:
        guard(st)
        meta = _meta(st, st.reviewer, diff)
        if meta["family"] is None or meta["family"] in st.avoid_families:
            raise Finish("FAILED", "invalid_result",
                         f"reviewer {st.reviewer} is not of another model family (D6)")
        name = f"reviewer.{round_}" + (f".{st.reviewer}" if st.reviewers_tried else "")
        brief = st.run_dir / f"brief.{name}.md"
        assemble_brief("reviewer", reviewer_task_brief(
            st.task, st.tree, round_=round_, max_rounds=st.task.max_review_rounds,
            base_sha=st.base_sha, diff=diff, checks=checks,
            prior_findings=st.findings if round_ > 1 else None), brief, kind=st.task.kind)
        before = snapshot(st.tree)
        out = workers.run_worker(
            "reviewer", st.reviewer, str(brief), str(st.tree), str(st.run_dir / f"{name}.log"),
            timeout=launch_budget(st), attempt=round_, wall_budget_s=launch_budget(st),
            model_meta=meta, sink=st.sink)
        account(st, out)
        after = check(st.tree, before)
        unchanged = not (after["changed"] or after["head_moved"] or after["git_dir_touched"])
        verdict = out.reason if out.kind == "done" else None
        findings = (out.result or {}).get("findings")
        blocking = (out.result or {}).get("blocking") is True
        rejected = _rejected(st, out, after, unchanged, verdict)
        # A rejected review records no verdict: an APPROVE event before a FAILED disposition
        # misreads (Phase 3 review, finding 9).
        st.sink.emit("review", round=round_, reviewer=st.reviewer,
                     verdict=None if rejected else verdict, findings=findings, blocking=blocking,
                     summary=out.summary, head_sha=st.head_sha, tree_unchanged=unchanged)
        if rejected:
            raise rejected
        if out.kind == "quota":
            record_quota(st.runs_root, st.reviewer)
            st.reviewer = _next_reviewer(st)
            continue
        st.review_rounds, st.review_summary, st.last_findings = round_, out.summary, findings
        st.review_blocking, st.reviewer_meta = blocking, meta
        return verdict
