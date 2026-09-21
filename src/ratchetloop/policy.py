"""The policy file: the first fenced ```yaml block of a markdown document, in predecessor's
WORKFLOW.md format (DECISIONS.md D21).

Fail closed: a missing file, a malformed block or an unknown key raises PolicyError. Nothing falls
back to defaults silently; a silent default would hide a broken policy file.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .model_policy import ModelPolicyError, validate_models

KNOWN_KEYS = frozenset({"models"})


class PolicyError(RuntimeError):
    """The policy file is missing, malformed, or has keys ratchetloop does not know."""


@dataclass(frozen=True)
class Policy:
    # Validated model ladders (model_policy.validate_models); None keeps each CLI's default model.
    models: dict | None = None


def load_policy(path: Path) -> Policy:
    path = Path(path)
    if not path.is_file():
        raise PolicyError(f"policy file not found: {path}")
    match = re.search(r"```yaml\n(.*?)```", path.read_text(encoding="utf-8"),
                      re.DOTALL | re.IGNORECASE)
    if not match:
        raise PolicyError(f"no fenced yaml block in {path}")
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as e:
        raise PolicyError(f"invalid YAML in {path}: {e}") from e
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise PolicyError(f"yaml block in {path} is not a mapping")
    unknown = set(data) - KNOWN_KEYS
    if unknown:
        raise PolicyError(f"unknown keys {sorted(unknown)} in {path}")
    models = None
    if "models" in data:
        try:
            models = validate_models(data["models"])
        except ModelPolicyError as e:
            raise PolicyError(f"key 'models' in {path}: {e}") from e
    return Policy(models=models)
