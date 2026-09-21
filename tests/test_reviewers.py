"""Reviewer choice by model family and cooldown (D6, D17, D19), and model-name families."""
from datetime import datetime, timedelta, timezone

import pytest

from ratchetloop.model_policy import family_of_model
from ratchetloop.reviewers import cooled, pick, record_quota

FAMILY = {"grok": "xai", "codex": "openai", "claude": "anthropic", "copilot": None}.get


def test_pick_skips_the_coders_family_and_unknown_families(tmp_path):
    assert pick("xai", FAMILY, runs_root=tmp_path) == "codex"
    assert pick("openai", FAMILY, runs_root=tmp_path) == "claude"
    assert pick("anthropic", FAMILY, runs_root=tmp_path) == "codex"


def test_pick_skips_cooled_and_excluded_providers(tmp_path):
    record_quota(tmp_path, "codex")
    assert pick("xai", FAMILY, runs_root=tmp_path) == "claude"
    assert pick("xai", FAMILY, runs_root=tmp_path, exclude={"claude"}) is None


def test_copilot_is_picked_only_with_a_known_family(tmp_path):
    with_google = {"copilot": "google"}
    assert pick("xai", lambda p: with_google.get(p), runs_root=tmp_path) == "copilot"


def test_cooldowns_expire(tmp_path):
    past = datetime.now(timezone.utc) - timedelta(hours=7)
    record_quota(tmp_path, "codex", now=past)
    assert "codex" not in cooled(tmp_path)
    record_quota(tmp_path, "claude")
    assert cooled(tmp_path) == {"claude"}


@pytest.mark.parametrize("model,family", [
    ("claude-sonnet-5", "anthropic"), ("sonnet", "anthropic"), ("gpt-5.6-terra", "openai"),
    ("grok-4.6-build", "xai"), ("gemini-3.7-flash", "google"), ("kimi-k3", "moonshot"),
    ("mai-code-1.1-flash", "microsoft"), ("something-else", None), (None, None),
])
def test_family_of_model(model, family):
    assert family_of_model(model) == family
