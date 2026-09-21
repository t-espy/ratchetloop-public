"""Token and cost usage from a worker's structured output (CONTRACT.md §4).

Numbers come only from a provider's structured stream or usage file, never from prose.
`usage_source` is "measured" when the CLI reported numbers and "unknown" otherwise; unknown values
stay None, never zero. predecessor returned "unknown" for grok without reading its output, although
grok's JSON result carries `usage` and `total_cost_usd`.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

UNKNOWN: dict[str, Any] = {
    "tokens_in": None, "tokens_out": None, "cost_usd": None, "usage_source": "unknown",
}
_IN = ("input_tokens", "inputTokens", "input", "prompt_tokens")
_OUT = ("output_tokens", "outputTokens", "output", "completion_tokens")
_COST = ("total_cost_usd", "cost_usd", "costUSD")


def _first(body: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = body.get(key)
        if value is not None and not isinstance(value, bool):
            return value
    return None


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _input_tokens(body: dict[str, Any]) -> tuple[Any, int | None]:
    """All input tokens processed, and the cached share. OpenAI-style usage (codex) counts cached
    reads inside input_tokens; Anthropic-style usage (claude, grok) reports cache reads and writes
    beside it, so claude's input_tokens alone read 6 for a launch that processed 78,355 (Phase 1
    spike, 2026-09-11)."""
    base = _first(body, _IN)
    if _count(base) is None:
        return base, None
    if "cached_input_tokens" in body:
        return base, _count(body.get("cached_input_tokens"))
    reads = _count(body.get("cache_read_input_tokens"))
    writes = _count(body.get("cache_creation_input_tokens"))
    if reads is None and writes is None:
        return base, None
    return base + (reads or 0) + (writes or 0), reads


def _main_model(models: dict[str, Any]) -> str:
    """The model that did the work. claude also lists the small model it uses for side tasks
    (haiku next to sonnet in the spike), so take the costliest, then the most output."""
    def weight(item: tuple[str, Any]) -> tuple[float, int]:
        stats = item[1] if isinstance(item[1], dict) else {}
        cost = stats.get("costUSD")
        cost = cost if isinstance(cost, (int, float)) and not isinstance(cost, bool) else -1.0
        out = _count(stats.get("outputTokens"))
        return cost, out if out is not None else -1
    return max(models.items(), key=weight)[0]


def usage_from_obj(obj: Any) -> dict[str, Any] | None:
    """Measured usage from one JSON object (a result envelope or a usage event), else None.

    Reads a `usage` or `token_usage` sub-object, or the object itself. A reported 0 is kept. The
    model the CLI says did the work (from `modelUsage`) is returned as `model_reported`."""
    if not isinstance(obj, dict):
        return None
    body = obj
    for key in ("usage", "token_usage"):
        if isinstance(obj.get(key), dict):
            body = obj[key]
            break
    tokens_in, cached = _input_tokens(body)
    tokens_out = _first(body, _OUT)
    if tokens_in is None and tokens_out is None:
        return None
    cost = _first(obj, _COST)
    out: dict[str, Any] = {
        "tokens_in": tokens_in, "tokens_cached": cached, "tokens_out": tokens_out,
        "cost_usd": cost if cost is not None else _first(body, _COST),
        "usage_source": "measured",
    }
    models = obj.get("modelUsage")
    if isinstance(models, dict) and models:
        out["model_reported"] = _main_model(models)
    return out


def copilot_usage_file(out_file: str) -> dict[str, Any]:
    """copilot writes its final usage to `--usage-output-file` (`<out_file>.usage`), not stdout.
    Its field names are checked in the Phase 1 spike; unreported numbers leave this empty."""
    path = Path(str(out_file) + ".usage")
    if not path.is_file():
        return {}
    try:
        obj = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    return usage_from_obj(obj) or {}


def parse_usage(stdout: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Usage from one parsed body, or from a whole JSONL stdout (the last line that reports it)."""
    if body is not None:
        return usage_from_obj(body) or dict(UNKNOWN)
    found: dict[str, Any] | None = None
    for line in (stdout or "").splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        got = usage_from_obj(obj)
        if got:
            found = got
    return found or dict(UNKNOWN)
