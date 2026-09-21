"""Model ladders: pick the cheapest adequate model for one worker launch.

The operator declares, per provider, an ordered ladder of {model, effort, family} rungs, cheapest
first, in the `models:` key of the policy file's yaml block. A launch's tier (light / standard /
heavy) picks the starting rung; each earlier failed run of the same role on the same task climbs
one rung. Pure functions: no files, no git, no environment. Carried from predecessor; `family` is new
(DECISIONS.md D6, D27): reviewer independence compares model families, not CLIs.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

TIERS = ("light", "standard", "heavy")
ROLES = ("coder", "reviewer")
# Effort values each CLI accepts: grok --reasoning-effort, claude --effort and
# copilot --effort (--help), codex model_reasoning_effort (models_cache.json),
# all checked 2026-09-11.
EFFORTS = {
    "grok": frozenset({"low", "medium", "high", "xhigh"}),
    "codex": frozenset({"low", "medium", "high", "xhigh", "max", "ultra"}),
    "claude": frozenset({"low", "medium", "high", "xhigh", "max"}),
    "copilot": frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"}),
}
# copilot runs models from several families (`copilot help config`, 1.0.83), so its rungs must
# name one; the other CLIs run only their maker's models.
FAMILIES = frozenset({"anthropic", "openai", "google", "microsoft", "moonshot", "xai"})
PROVIDER_FAMILY = {"grok": "xai", "codex": "openai", "claude": "anthropic"}
# The family of a model name a CLI reports running (CONTRACT.md §7: the reviewer family check uses
# the model actually run). Prefixes from `copilot help config` and the CLIs' own model names.
_MODEL_PREFIX_FAMILY = (
    ("claude", "anthropic"), ("sonnet", "anthropic"), ("opus", "anthropic"), ("haiku", "anthropic"),
    ("fable", "anthropic"), ("gpt", "openai"), ("codex", "openai"), ("o1", "openai"),
    ("o3", "openai"), ("o4", "openai"), ("gemini", "google"), ("grok", "xai"),
    ("kimi", "moonshot"), ("mai-", "microsoft"),
)


def family_of_model(model: str | None) -> str | None:
    """The family a model name belongs to, or None when the name does not say."""
    name = (model or "").strip().lower()
    for prefix, family in _MODEL_PREFIX_FAMILY:
        if name.startswith(prefix):
            return family
    return None


DEFAULTS: dict[str, Any] = {
    "tier_start": {"light": 0, "standard": 1, "heavy": 2},
    "default_tier": {"coder": "standard", "reviewer": "standard"},
    "review_light_max_lines": 80,
    "review_heavy_min_lines": 400,
}
_KEYS = frozenset({"ladders", *DEFAULTS})


class ModelPolicyError(ValueError):
    """The models block or a task's tier is malformed; callers fail closed."""


@dataclass(frozen=True)
class Choice:
    model: str | None
    effort: str | None
    tier: str | None
    rung: int | None
    reason: str
    family: str | None = None

    def meta(self) -> dict[str, Any]:
        return {"model": self.model, "effort": self.effort, "tier": self.tier,
                "rung": self.rung, "family": self.family, "select_reason": self.reason}


def _int(value: Any, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ModelPolicyError(f"{key} must be a non-negative int")
    return value


def _family(provider: str, raw: Any, where: str) -> str:
    fixed = PROVIDER_FAMILY.get(provider)
    if raw is None:
        if fixed is None:
            raise ModelPolicyError(
                f"{where}.family is required: {provider} runs models from several families")
        return fixed
    if raw not in FAMILIES:
        raise ModelPolicyError(f"{where}.family {raw!r} must be one of {sorted(FAMILIES)}")
    if fixed is not None and raw != fixed:
        raise ModelPolicyError(f"{where}.family {raw!r}: {provider} only runs {fixed} models")
    return raw


def _rung(provider: str, raw: Any, where: str) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) - {"model", "effort", "family"}:
        raise ModelPolicyError(f"{where} must be a mapping of model, optional effort and family")
    model = raw.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ModelPolicyError(f"{where}.model must be a non-empty string")
    if provider == "copilot" and model.strip() == "auto":
        raise ModelPolicyError(f"{where}.model 'auto' is refused: its family cannot be known")
    rung = {"model": model.strip(), "family": _family(provider, raw.get("family"), where)}
    effort = raw.get("effort")
    if effort is not None:
        if effort not in EFFORTS[provider]:
            allowed = ", ".join(sorted(EFFORTS[provider])) or "none"
            raise ModelPolicyError(
                f"{where}.effort {effort!r} is not accepted by {provider} ({allowed})")
        rung["effort"] = effort
    return rung


def _ladders(raw: Any) -> dict[str, list[dict[str, str]]]:
    if not isinstance(raw, dict) or not raw:
        raise ModelPolicyError("ladders must be a non-empty mapping of provider -> rungs")
    out: dict[str, list[dict[str, str]]] = {}
    for provider, rungs in raw.items():
        if provider not in EFFORTS:
            raise ModelPolicyError(f"ladders: unknown provider {provider!r}")
        if not isinstance(rungs, list) or not rungs:
            raise ModelPolicyError(f"ladders.{provider} must be a non-empty list")
        out[provider] = [_rung(provider, r, f"ladders.{provider}[{i}]")
                         for i, r in enumerate(rungs)]
    return out


def _tier_start(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict) or set(raw) != set(TIERS):
        raise ModelPolicyError(f"tier_start must map exactly {list(TIERS)}")
    starts = {t: _int(raw[t], f"tier_start.{t}") for t in TIERS}
    if not starts["light"] <= starts["standard"] <= starts["heavy"]:
        raise ModelPolicyError("tier_start must satisfy light <= standard <= heavy")
    return starts


def validate_models(raw: Any) -> dict[str, Any]:
    """Normalise the `models:` block, filling defaults; raise on anything malformed."""
    if not isinstance(raw, dict):
        raise ModelPolicyError("models must be a mapping")
    unknown = set(raw) - _KEYS
    if unknown:
        raise ModelPolicyError(f"unknown models keys: {sorted(unknown)}")
    out: dict[str, Any] = {
        "ladders": _ladders(raw.get("ladders")),
        "tier_start": _tier_start(raw.get("tier_start", DEFAULTS["tier_start"])),
    }
    defaults = raw.get("default_tier", {})
    if not isinstance(defaults, dict) or set(defaults) - set(ROLES):
        raise ModelPolicyError(f"default_tier keys must be among {list(ROLES)}")
    for role, tier in defaults.items():
        if tier not in TIERS:
            raise ModelPolicyError(f"default_tier.{role} must be one of {list(TIERS)}")
    out["default_tier"] = {**DEFAULTS["default_tier"], **defaults}
    for key in ("review_light_max_lines", "review_heavy_min_lines"):
        out[key] = _int(raw.get(key, DEFAULTS[key]), key)
    if out["review_light_max_lines"] >= out["review_heavy_min_lines"]:
        raise ModelPolicyError("review_light_max_lines must be below review_heavy_min_lines")
    return out


def _tier(models: dict[str, Any], role: str, spec: dict[str, Any],
          diff_lines: int | None) -> tuple[str, str]:
    explicit = spec.get("tier")
    if explicit is not None:
        if explicit not in TIERS:
            raise ModelPolicyError(f"task tier {explicit!r} must be one of {list(TIERS)}")
        return explicit, "task tier"
    if role == "reviewer" and diff_lines is not None:
        if diff_lines <= models["review_light_max_lines"]:
            return "light", f"diff {diff_lines} lines"
        if diff_lines >= models["review_heavy_min_lines"]:
            return "heavy", f"diff {diff_lines} lines"
    return models["default_tier"].get(role, "standard"), f"default for {role}"


def choose(models: dict[str, Any] | None, provider: str, role: str, *,
           spec: dict[str, Any] | None = None, diff_lines: int | None = None,
           escalation: int = 0) -> Choice:
    """The rung for one launch. No policy or no ladder keeps the CLI default model, whose family
    is known only for single-family CLIs."""
    if models is None:
        return Choice(None, None, None, None, "no models policy", PROVIDER_FAMILY.get(provider))
    ladder = models["ladders"].get(provider)
    if not ladder:
        return Choice(None, None, None, None, f"no ladder for {provider}",
                      PROVIDER_FAMILY.get(provider))
    tier, why = _tier(models, role, spec or {}, diff_lines)
    climb = max(0, int(escalation))
    rung = min(models["tier_start"][tier] + climb, len(ladder) - 1)
    if climb:
        why += f", +{climb} after prior failures"
    pick = ladder[rung]
    return Choice(pick["model"], pick.get("effort"), tier, rung, why, pick["family"])
