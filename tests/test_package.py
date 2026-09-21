"""The suite must exercise this tree's own copy of the package, never another installed copy: in a
checkout (it has `.git`) the editable install's `src/`; in a pin (`deploy/pin.sh`, a `git archive`
tree) the pin's own `.venv`. Asserting `src/` alone failed the first pinned smoke (Phase 7)."""
from pathlib import Path

import ratchetloop

TREE = Path(__file__).resolve().parents[1]


def test_imports_this_trees_own_package():
    here = Path(ratchetloop.__file__).resolve()
    expected = TREE / "src" if (TREE / ".git").exists() else TREE / ".venv"
    assert here.is_relative_to(expected), f"{here} is not under {expected}"
