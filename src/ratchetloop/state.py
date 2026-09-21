"""The one explicit state record a run's steps read and update (D4: steps are functions over it, so a
graph runtime could wire the same steps later without rewriting them)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .events import EventSink
from .task import Task

# CONTRACT.md §6: disposition -> exit code. A refused task exits 2 with no run directory.
DISPOSITIONS = {"ACCEPT": 0, "HUMAN_REVIEW": 3, "FAILED": 4, "STOPPED": 5}
REFUSED_EXIT = 2


class Finish(Exception):
    """Ends the run with a disposition and a reason from CONTRACT.md §6."""

    def __init__(self, disposition: str, reason: str, detail: str = ""):
        if disposition not in DISPOSITIONS:
            raise ValueError(f"unknown disposition {disposition!r}")
        super().__init__(f"{disposition} {reason} {detail}".strip())
        self.disposition, self.reason, self.detail = disposition, reason, detail


class Refused(Exception):
    """Admit refused the task before any run directory existed (exit 2)."""


@dataclass
class RunState:
    task: Task
    run_id: str
    run_dir: Path
    runs_root: Path
    sink: EventSink
    coder: str
    reviewer: str
    coder_family: str
    policy_path: Path | None = None
    started: float = field(default_factory=time.time)
    branch: str = ""
    base_sha: str = ""
    tree: Path | None = None
    env: dict[str, str] = field(default_factory=dict)
    head_sha: str = ""
    findings: str | None = None
    review_rounds: int = 0
    review_summary: str = ""
    last_findings: dict[str, int] | None = None
    review_blocking: bool = False  # the last review flagged a bug that leaves the task unmet
    checks_passed: bool | None = None
    checks_head: str = ""
    checks_wall_s: float = 0.0
    check_fix_used: bool = False
    # Untracked paths the pipeline itself left (setup outputs, check residue): the only ones an
    # ACCEPTed tree may be force-removed with (Phase 3 review, finding 4).
    pipeline_dirt: set[str] = field(default_factory=set)
    # `--continue` (D15): model families of earlier coders on the branch, the run picked up,
    # whether the worktree was reused, and the uncommitted paths adopted from a dead run.
    avoid: set[str] = field(default_factory=set)
    continued_from: str | None = None
    reused: bool = False
    adopted: list[str] = field(default_factory=list)
    reviewers_tried: set[str] = field(default_factory=set)
    cost_usd: float = 0.0
    coder_meta: dict[str, Any] = field(default_factory=dict)
    reviewer_meta: dict[str, Any] = field(default_factory=dict)
    disposition: str = ""
    reason: str = ""
    detail: str = ""
    worktree_kept: str | None = None

    @property
    def task_runs(self) -> Path:
        return self.runs_root / self.task.task_key

    @property
    def avoid_families(self) -> set[str]:
        """Families a reviewer may not be: this run's coder's and every earlier coder's (D6)."""
        return {self.coder_family, *self.avoid} - {""}

    @property
    def spec(self) -> dict[str, Any]:
        return {"tier": self.task.tier} if self.task.tier else {}

    def remaining_s(self) -> float:
        return self.task.wall_budget_s - (time.time() - self.started)
