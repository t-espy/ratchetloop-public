"""Model ladders: validation, families and rung choice (carried from predecessor.model_policy)."""
from __future__ import annotations

from pathlib import Path

import pytest

from ratchetloop.model_policy import DEFAULTS, ModelPolicyError, choose, validate_models
from ratchetloop.policy import PolicyError, load_policy

LADDERS = {
    "codex": [{"model": "luna", "effort": "medium"}, {"model": "terra"},
              {"model": "sol", "effort": "high"}],
    "grok": [{"model": "grok-4.6", "effort": "low"}],
}


def _models(**extra):
    return validate_models({"ladders": LADDERS, **extra})


def test_defaults_are_filled():
    m = _models()
    assert m["tier_start"] == DEFAULTS["tier_start"]
    assert m["default_tier"] == DEFAULTS["default_tier"]
    assert (m["review_light_max_lines"], m["review_heavy_min_lines"]) == (80, 400)
    assert m["ladders"]["codex"][1] == {"model": "terra", "family": "openai"}


@pytest.mark.parametrize("raw,needle", [
    ([], "must be a mapping"),
    ({"ladders": LADDERS, "extra": 1}, "unknown models keys"),
    ({}, "ladders must be"),
    ({"ladders": {"gemini": [{"model": "x"}]}}, "unknown provider"),
    ({"ladders": {"codex": []}}, "non-empty list"),
    ({"ladders": {"codex": [{"model": ""}]}}, "non-empty string"),
    ({"ladders": {"codex": [{"model": "x", "temp": 1}]}}, "mapping of model"),
    ({"ladders": {"codex": [{"model": "x", "effort": "turbo"}]}}, "not accepted by codex"),
    ({"ladders": {"copilot": [{"model": "x", "family": "google", "effort": "ultra"}]}},
     "not accepted by copilot"),
    ({"ladders": LADDERS, "tier_start": {"light": 0, "standard": 1}}, "map exactly"),
    ({"ladders": LADDERS, "tier_start": {"light": 2, "standard": 1, "heavy": 3}},
     "light <= standard"),
    ({"ladders": LADDERS, "tier_start": {"light": True, "standard": 1, "heavy": 2}},
     "non-negative int"),
    ({"ladders": LADDERS, "default_tier": {"tester": "light"}}, "default_tier keys"),
    ({"ladders": LADDERS, "default_tier": {"coder": "huge"}}, "default_tier.coder"),
    ({"ladders": LADDERS, "review_light_max_lines": 400}, "must be below"),
    ({"ladders": LADDERS, "review_heavy_min_lines": -1}, "non-negative int"),
    ({"ladders": LADDERS, "default_tier": {"planner": "heavy"}}, "default_tier keys"),
    ({"ladders": {"copilot": [{"model": "gpt-5.4"}]}}, "family is required"),
    ({"ladders": {"copilot": [{"model": "auto", "family": "openai"}]}}, "'auto' is refused"),
    ({"ladders": {"copilot": [{"model": "x", "family": "acme"}]}}, "must be one of"),
    ({"ladders": {"claude": [{"model": "sonnet", "family": "openai"}]}}, "only runs anthropic"),
])
def test_validate_refuses(raw, needle):
    with pytest.raises(ModelPolicyError, match=needle):
        validate_models(raw)


def test_no_policy_or_no_ladder_keeps_cli_default():
    assert choose(None, "codex", "coder").model is None
    c = choose(_models(), "claude", "coder")
    assert c.model is None and c.reason == "no ladder for claude"
    assert c.family == "anthropic"
    assert choose(None, "copilot", "reviewer").family is None  # copilot's default is unknowable


def test_default_tier_per_role():
    m = _models()
    assert choose(m, "codex", "coder").meta() == {
        "model": "terra", "effort": None, "tier": "standard", "rung": 1,
        "family": "openai", "select_reason": "default for coder",
    }


def test_copilot_rung_carries_its_declared_family():
    m = validate_models({"ladders": {"copilot": [
        {"model": "gemini-3.7-flash", "family": "google", "effort": "low"}]}})
    c = choose(m, "copilot", "reviewer")
    assert (c.model, c.effort, c.family) == ("gemini-3.7-flash", "low", "google")


def test_explicit_task_tier_wins():
    c = choose(_models(), "codex", "coder", spec={"tier": "light", "foundational": True})
    assert (c.model, c.effort, c.tier, c.reason) == ("luna", "medium", "light", "task tier")


def test_bad_task_tier_fails_closed():
    with pytest.raises(ModelPolicyError, match="task tier"):
        choose(_models(), "codex", "coder", spec={"tier": "cheap"})


@pytest.mark.parametrize("lines,tier,model", [
    (0, "light", "luna"), (80, "light", "luna"), (81, "standard", "terra"),
    (399, "standard", "terra"), (400, "heavy", "sol"),
])
def test_review_tier_follows_diff_size(lines, tier, model):
    c = choose(_models(), "codex", "reviewer", diff_lines=lines)
    assert (c.tier, c.model) == (tier, model)


def test_diff_size_only_sizes_reviews():
    assert choose(_models(), "codex", "coder", diff_lines=5).tier == "standard"


def test_escalation_climbs_and_clamps():
    m = _models()
    c = choose(m, "codex", "coder", escalation=1)
    assert (c.model, c.rung) == ("sol", 2)
    assert "+1 after prior failures" in c.reason
    assert choose(m, "codex", "coder", escalation=9).rung == 2
    assert choose(m, "codex", "coder", escalation=-3).rung == 1
    assert choose(m, "grok", "reviewer", diff_lines=500).rung == 0


def _write(tmp_path: Path, block: str) -> Path:
    p = tmp_path / "WORKFLOW.md"
    p.write_text(f"# policy\n\n```yaml\n{block}```\n")
    return p


def test_load_policy_carries_models(tmp_path):
    p = _write(tmp_path, "models:\n  ladders:\n"
                         "    grok:\n      - {model: grok-4.6, effort: low}\n")
    assert load_policy(p).models["ladders"]["grok"] == [
        {"model": "grok-4.6", "family": "xai", "effort": "low"}]


def test_load_policy_without_models_is_none(tmp_path):
    assert load_policy(_write(tmp_path, "")).models is None


def test_load_policy_refuses_bad_models(tmp_path):
    p = _write(tmp_path, "models:\n  ladders:\n    codex:\n      - {model: x, effort: turbo}\n")
    with pytest.raises(PolicyError, match="key 'models'"):
        load_policy(p)


def test_repo_workflow_template_validates():
    template = Path(__file__).resolve().parents[1] / "WORKFLOW.md"
    models = load_policy(template).models
    assert models is not None
    assert {"codex", "grok", "claude"} <= set(models["ladders"])
