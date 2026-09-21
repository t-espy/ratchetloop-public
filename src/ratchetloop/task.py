"""Task files (CONTRACT.md §2): one bounded change, validated before any agent runs.

The pipeline and its callers validate with this same code (`ratchetloop run --check`, D22), so a
caller can never accept a task the pipeline would refuse (FAILURE_MODES.md §9). Unknown keys are
refused. Carried from predecessor's specs.py; grep-only checks are refused here, not just warned about.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

PROVIDERS = ("grok", "codex", "claude", "copilot")
SINGLE_FAMILY = ("grok", "codex", "claude")
KINDS = ("code", "doc")
TIERS = ("light", "standard", "heavy")
TASK_KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
KEYS = frozenset({
    "task_key", "kind", "repo", "objective", "brief_file", "acceptance_criteria", "checks",
    "base_branch", "checks_cwd", "setup", "allowed_paths", "coder", "reviewer", "tier",
    "max_review_rounds", "wall_budget_s", "cost_budget_usd", "env", "carried_findings",
})
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SOURCE_SUFFIXES = (".py", ".js", ".ts", ".go", ".rs")


class TaskError(ValueError):
    """The task file cannot be run as written; the message names the key."""


@dataclass(frozen=True)
class Task:
    task_key: str
    kind: str
    repo: Path
    acceptance_criteria: tuple[str, ...]
    checks: tuple[str, ...]
    objective: str | None = None
    brief_file: Path | None = None
    base_branch: str = "master"
    checks_cwd: str = "."
    setup: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] | None = None
    coder: str | None = None
    reviewer: str | None = None
    tier: str | None = None
    max_review_rounds: int = 2
    wall_budget_s: float = 7200.0
    cost_budget_usd: float = 25.0
    # Variables for setup and checks, which otherwise see only a short allow-list (checks.py).
    env: dict[str, str] = field(default_factory=dict)
    # Open residual findings carried from earlier stages of an idea (§9.5), one line each; the
    # coder fixes what its phase touches, the reviewer may close or re-raise them.
    carried_findings: tuple[str, ...] = ()


def _env(raw: Any) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise TaskError("`env` must be a mapping of NAME: value")
    out: dict[str, str] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not ENV_NAME.fullmatch(name):
            raise TaskError(f"`env` name {name!r} is not a valid variable name")
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise TaskError(f"`env.{name}` must be a string or a number")
        out[name] = str(value)
    return out


def grep_only_checks(checks: tuple[str, ...] | list[str]) -> list[str]:
    """Checks that only grep a source file: they prove text is present, not behaviour."""
    hits: list[str] = []
    for cmd in checks:
        toks = str(cmd).split()
        i = 0
        while i < len(toks) and "=" in toks[i] and not toks[i].startswith("-"):
            i += 1
        if i >= len(toks) or Path(toks[i]).name != "grep":
            continue
        if any(not t.startswith("-") and ("/" in t or t.endswith(_SOURCE_SUFFIXES))
               for t in toks[i + 1:]):
            hits.append(str(cmd))
    return hits


def _strings(raw: Any, key: str, *, required: bool) -> tuple[str, ...]:
    if raw is None:
        if required:
            raise TaskError(f"`{key}` is required")
        return ()
    if not isinstance(raw, list) or not all(isinstance(s, str) and s.strip() for s in raw):
        raise TaskError(f"`{key}` must be a list of non-empty strings")
    if required and not raw:
        raise TaskError(f"`{key}` must not be empty")
    return tuple(raw)


def _relative(value: str, key: str) -> str:
    p = PurePosixPath(value)
    if p.is_absolute() or ".." in p.parts:
        raise TaskError(f"`{key}` {value!r} must be a path inside the repository")
    return value


def _number(raw: Any, key: str, default: float) -> float:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0:
        raise TaskError(f"`{key}` must be a non-negative number")
    return float(raw)


def _text(raw: Any, key: str, *, prose: bool = False) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise TaskError(f"`{key}` must be a non-empty string")
    # Paths and refs that reach a command line must not look like flags (pin.sh --help,
    # FAILURE_MODES §9); prose such as an objective may start with "- ".
    if not prose and raw.startswith("-"):
        raise TaskError(f"`{key}` {raw!r} looks like a flag")
    return raw


def _providers(raw: dict[str, Any]) -> tuple[str | None, str | None]:
    picked = []
    for key in ("coder", "reviewer"):
        value = raw.get(key)
        if value is not None and value not in PROVIDERS:
            raise TaskError(f"`{key}` must be one of {list(PROVIDERS)}")
        picked.append(value)
    coder, reviewer = picked
    # Same single-family CLI = same model family (D6); copilot's family is checked when chosen.
    if coder is not None and coder == reviewer and coder in SINGLE_FAMILY:
        raise TaskError(f"`reviewer` {reviewer!r} is the coder's model family (D6)")
    return coder, reviewer


def validate_task(raw: Any, *, base_dir: Path | None = None) -> Task:
    """A validated Task. Relative `repo` and `brief_file` resolve against `base_dir`."""
    if not isinstance(raw, dict):
        raise TaskError("a task file must be a mapping")
    unknown = set(raw) - KEYS
    if unknown:
        raise TaskError(f"unknown keys: {sorted(unknown)}")
    key = raw.get("task_key")
    if not isinstance(key, str) or not TASK_KEY.fullmatch(key):
        raise TaskError("`task_key` must match [a-z0-9][a-z0-9-]{0,62}")
    kind = raw.get("kind", "code")
    if kind not in KINDS:
        raise TaskError(f"`kind` must be one of {list(KINDS)}")
    base = Path(base_dir) if base_dir is not None else Path.cwd()
    repo = (base / Path(_text(raw.get("repo"), "repo")).expanduser()).resolve()
    if ("objective" in raw) == ("brief_file" in raw):
        raise TaskError("set exactly one of `objective` or `brief_file`")
    objective = _text(raw["objective"], "objective", prose=True) if "objective" in raw else None
    brief = (base / Path(_text(raw["brief_file"], "brief_file")).expanduser()).resolve() \
        if "brief_file" in raw else None
    checks = _strings(raw.get("checks"), "checks", required=kind == "code")
    grep_only = grep_only_checks(checks)
    if grep_only:
        raise TaskError(f"checks must test behaviour, not grep source: {grep_only} (D22)")
    allowed = raw.get("allowed_paths")
    if kind == "doc" and allowed is None:
        raise TaskError("`allowed_paths` is required for a doc task")
    if allowed is not None:
        allowed = tuple(_relative(p, "allowed_paths")
                        for p in _strings(allowed, "allowed_paths", required=True))
    tier = raw.get("tier")
    if tier is not None and tier not in TIERS:
        raise TaskError(f"`tier` must be one of {list(TIERS)}")
    rounds = raw.get("max_review_rounds", 2)
    if isinstance(rounds, bool) or rounds not in (1, 2):
        raise TaskError("`max_review_rounds` must be 1 or 2")
    coder, reviewer = _providers(raw)
    return Task(
        task_key=key, kind=kind, repo=repo, objective=objective, brief_file=brief,
        acceptance_criteria=_strings(raw.get("acceptance_criteria"), "acceptance_criteria",
                                     required=True),
        checks=checks, base_branch=_text(raw.get("base_branch", "master"), "base_branch"),
        checks_cwd=_relative(_text(raw.get("checks_cwd", "."), "checks_cwd"), "checks_cwd"),
        setup=_strings(raw.get("setup"), "setup", required=False), allowed_paths=allowed,
        coder=coder, reviewer=reviewer, tier=tier, max_review_rounds=rounds,
        wall_budget_s=_number(raw.get("wall_budget_s"), "wall_budget_s", 7200.0),
        cost_budget_usd=_number(raw.get("cost_budget_usd"), "cost_budget_usd", 25.0),
        env=_env(raw.get("env")),
        carried_findings=_strings(raw.get("carried_findings"), "carried_findings", required=False),
    )


def load_task(path: Path) -> Task:
    """Read and validate a task file; also refuse a missing brief file or a non-git repo."""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise TaskError(f"cannot read task file {path}: {e}") from e
    except yaml.YAMLError as e:
        raise TaskError(f"invalid YAML in {path}: {e}") from e
    task = validate_task(raw, base_dir=path.resolve().parent)
    if task.brief_file is not None and not task.brief_file.is_file():
        raise TaskError(f"`brief_file` not found: {task.brief_file}")
    if not (task.repo / ".git").exists():
        raise TaskError(f"`repo` is not a git repository: {task.repo}")
    return task
