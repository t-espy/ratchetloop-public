"""The policy file fails closed: nothing falls back to defaults silently."""
import pytest

from ratchetloop.policy import PolicyError, load_policy

BLOCK = "# policy\n\n```yaml\n{body}```\n"


def _write(tmp_path, body: str):
    path = tmp_path / "WORKFLOW.md"
    path.write_text(BLOCK.format(body=body))
    return path


def test_models_are_validated_with_families(tmp_path):
    path = _write(tmp_path, "models:\n  ladders:\n    copilot:\n"
                            "      - {model: gemini-3.7-flash, family: google}\n")
    assert load_policy(path).models["ladders"]["copilot"] == [
        {"model": "gemini-3.7-flash", "family": "google"}]


def test_empty_block_means_no_models(tmp_path):
    assert load_policy(_write(tmp_path, "")).models is None


@pytest.mark.parametrize("body,match", [
    ("max_revise_rounds: 2\n", "unknown keys"),  # predecessor's dispatcher knobs are not policy here
    ("- a\n", "not a mapping"),
    ("models: [\n", "invalid YAML"),
    ("models:\n  ladders:\n    copilot:\n      - {model: auto, family: openai}\n", "auto"),
    ("models:\n  ladders:\n    copilot:\n      - {model: gpt-5.4}\n", "family is required"),
    ("models:\n  ladders:\n    grok:\n      - {model: grok-4.6, family: openai}\n", "only runs xai"),
])
def test_malformed_policy_is_refused(tmp_path, body, match):
    with pytest.raises(PolicyError, match=match):
        load_policy(_write(tmp_path, body))


def test_missing_file_and_missing_block_are_refused(tmp_path):
    with pytest.raises(PolicyError, match="not found"):
        load_policy(tmp_path / "nope.md")
    path = tmp_path / "x.md"
    path.write_text("no yaml here\n")
    with pytest.raises(PolicyError, match="no fenced yaml"):
        load_policy(path)
