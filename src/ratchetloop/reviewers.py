"""Reviewer choice: a provider whose model family differs from the coder's and that is not cooling
down after a quota failure (D6, D17, D19). Carried from predecessor's rotation and cooldown file; its
read of the lab supervisor's cooldowns is gone, and exclusion is by model family, not CLI name."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

ORDER = ("codex", "claude", "copilot", "grok")  # D17
COOLDOWN_TTL = timedelta(hours=6)


def cooldown_path(runs_root: Path) -> Path:
    return Path(runs_root) / "cooldown.json"


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text()) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def cooled(runs_root: Path, now: datetime | None = None) -> set[str]:
    now = now or datetime.now(timezone.utc)
    out: set[str] = set()
    for name, until in _load(cooldown_path(runs_root)).items():
        try:
            ts = datetime.fromisoformat(str(until).replace("Z", "+00:00"))
        except ValueError:
            continue
        if (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)) > now:
            out.add(str(name))
    return out


def record_quota(runs_root: Path, provider: str, now: datetime | None = None) -> None:
    path = cooldown_path(runs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _load(path)
    data[provider] = ((now or datetime.now(timezone.utc)) + COOLDOWN_TTL).isoformat()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data) + "\n")
    tmp.replace(path)


def pick(coder_family: str | None, family_of: Callable[[str], str | None], *, runs_root: Path,
         exclude: Iterable[str] = (), avoid: Iterable[str] = ()) -> str | None:
    """The first provider in ORDER that is not cooling down or excluded and whose launch family is
    known and is neither the coder's nor in `avoid` (on `--continue`, earlier coders' families).
    `family_of(provider)` comes from the model policy: copilot's family is known only from a ladder
    rung that names it."""
    skip = cooled(runs_root) | set(exclude)
    barred = {coder_family, *avoid}
    for name in ORDER:
        if name in skip:
            continue
        family = family_of(name)
        if family is not None and family not in barred:
            return name
    return None
