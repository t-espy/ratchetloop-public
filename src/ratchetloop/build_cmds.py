"""The command-line side of `build` and `approve` (CONTRACT.md §1, §9), apart from the chain itself
(split from build.py at its size limit)."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from .build import APPROVAL, IDEA_EXIT, _iso, _refusals, build
from .detach import detach
from .idea import IdeaError, load_idea
from .lifecycle import hold_task
from .resume import run_dirs
from .run import runs_root
from .state import REFUSED_EXIT, Refused


def check_build(idea_path: Path, continue_: bool, root: Path) -> str:
    """Everything `build` refuses before an idea run exists, done in the foreground: `build
    --detach` must exit 2, not fail in a detached log (Phase 6 review, finding 4). Returns the
    idea's key."""
    try:
        idea = load_idea(Path(idea_path))
    except IdeaError as e:
        raise Refused(str(e)) from e
    hold = hold_task(root, idea.idea_key)
    try:
        _refusals(idea, root, continue_)
    finally:
        hold.release()
    return idea.idea_key


def approve(idea_key: str, root: Path | None = None) -> int:
    """Release an idea paused at `--gate plan`: the approval goes in its latest build's directory."""
    dirs = run_dirs((root or runs_root()) / idea_key)
    try:
        latest = json.loads((dirs[-1] / "result.json").read_text()) if dirs else {}
    except (OSError, json.JSONDecodeError):
        latest = {}
    if latest.get("disposition") != "PAUSED":
        print(f"ratchetloop: refused: idea {idea_key} is not paused at a gate", file=sys.stderr)
        return REFUSED_EXIT
    (dirs[-1] / APPROVAL).write_text(json.dumps({"at": _iso(time.time()),
                                                 "stage": latest.get("stage")}) + "\n")
    print(json.dumps({"idea_key": idea_key, "approved": dirs[-1].name}))
    return 0


def cmd_build(args: Any, argv: list[str]) -> int:
    root = runs_root()
    try:
        if args.detach:
            return detach(argv, check_build(Path(args.idea), args.continue_, root), root)
        res = build(Path(args.idea), gate=args.gate, continue_=args.continue_,
                    policy_path=args.policy, coder=args.coder, reviewer=args.reviewer, root=root)
    except Refused as e:
        print(f"ratchetloop: refused: {e}", file=sys.stderr)
        return REFUSED_EXIT
    print(json.dumps({k: res.get(k) for k in ("idea_key", "run_id", "disposition", "reason",
                                              "stage", "branch", "head_sha")}))
    residuals = res.get("residuals") or []
    if residuals:  # §9.5: the debt the merge reviews
        open_ = sum(not r.get("resolved_by") for r in residuals)
        print(f"{res['disposition']} with {len(residuals)} residual findings, {open_} open "
              f"(see {res['run_id']}/residuals.json and docs/REVIEW_NOTES.md on the branch)")
    return IDEA_EXIT[res["disposition"]]
