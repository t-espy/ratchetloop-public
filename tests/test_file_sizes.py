"""Source files stay near the 300-line target: 300 is the goal, +10% is acceptable,
500 is the hard limit (operator, 2026-09-08). Package code is enforced at 330 (D12); test files
only at the 500-line hard limit. Carried from predecessor."""
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "ratchetloop"
TESTS = Path(__file__).resolve().parent
MAX_LINES = 330
HARD_LIMIT = 500


def test_package_exists():
    # A renamed or missing package would make the size check below pass vacuously.
    assert (PACKAGE / "__init__.py").is_file()


def test_package_files_within_size_cap():
    over = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        n = len(path.read_text().splitlines())
        if n > MAX_LINES:
            over.append(f"{path.relative_to(PACKAGE.parent)}: {n}")
    assert not over, "files over the 330-line cap (target 300, hard limit 500):\n" + "\n".join(over)


def test_test_files_within_hard_limit():
    # Phase 0 review finding 1: tests were ungated; predecessor test files ported in Phase 1
    # run up to 469 lines, so they fit the hard limit but not the package cap.
    over = []
    for path in sorted(TESTS.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        n = len(path.read_text().splitlines())
        if n > HARD_LIMIT:
            over.append(f"{path.relative_to(TESTS.parent)}: {n}")
    assert not over, "test files over the 500-line hard limit:\n" + "\n".join(over)
