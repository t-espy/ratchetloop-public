"""Model choice for one launch: the policy file, earlier failed runs of the task, and diff size.

Carried from predecessor; prior failures are counted from earlier runs' events.jsonl instead of
Postgres, and the policy comes from ratchetloop's own file (DECISIONS.md D21).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .events import read_events
from .model_policy import Choice, PROVIDER_FAMILY, choose
from .policy import load_policy

POLICY_ENV = "RATCHETLOOP_POLICY"
# Outcome kinds that mean the model could not do the job. Quota, launch and cleanup failures are
# infrastructure; provider cooldown handles those, not a bigger model.
ESCALATE_KINDS = ("blocked", "invalid_result", "protocol_error")


def default_policy_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "ratchetloop" / "WORKFLOW.md"


def policy_models(path: Path | None = None) -> dict[str, Any] | None:
    """Validated ladders, or None when no policy is configured (each CLI's default model).

    An explicit path (the --policy flag) wins, then RATCHETLOOP_POLICY, then the default file if it
    exists. An explicit path or a set variable whose file is missing or malformed raises
    PolicyError: fail closed."""
    if path is None:
        raw = os.environ.get(POLICY_ENV)
        if raw:
            path = Path(raw).expanduser()
        else:
            path = default_policy_path()
            if not path.is_file():
                return None
    return load_policy(path).models


def prior_failures(task_runs: Path, role: str) -> int:
    """Runs of this task (one directory each under `task_runs`) in which `role` failed: a worker
    in that role ended blocked, invalid or with a protocol error, or, for the coder, the run's last
    checks were red — a red fixed by the one fix launch (D14) is not a failure (Phase 3 review,
    finding 6). One climb per run, however many failing events it holds."""
    if not Path(task_runs).is_dir():
        return 0
    count = 0
    for run_dir in sorted(p for p in Path(task_runs).iterdir() if p.is_dir()):
        events = read_events(run_dir / "events.jsonl")
        failed_worker = any(ev.get("event") == "worker_end" and ev.get("role") == role
                            and ev.get("kind") in ESCALATE_KINDS for ev in events)
        checks = [ev.get("passed") for ev in events if ev.get("event") == "checks"]
        failed_checks = role == "coder" and bool(checks) and checks[-1] is False
        count += failed_worker or failed_checks
    return count


def diff_lines(diff: str) -> int:
    """Added plus removed lines, file headers excluded."""
    return sum(1 for line in diff.splitlines()
               if line.startswith(("+", "-")) and not line.startswith(("+++", "---")))


def for_launch(role: str, provider: str, *, spec: dict[str, Any] | None = None,
               task_runs: Path | None = None, diff: str | None = None,
               policy_path: Path | None = None) -> dict[str, Any]:
    """Model meta for one worker launch; a None model keeps the CLI default."""
    models = policy_models(policy_path)
    if models is None:
        return Choice(None, None, None, None, "no models policy",
                      PROVIDER_FAMILY.get(provider)).meta()
    climb = prior_failures(task_runs, role) if task_runs is not None else 0
    lines = diff_lines(diff) if diff is not None else None
    return choose(models, provider, role, spec=spec or {}, diff_lines=lines,
                  escalation=climb).meta()
