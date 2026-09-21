"""Regression tests for porcelain parsing + the allowed-paths guard.

Ported from the pre-registered bake-off gate (round 3) when the fixes
merged into the mainline: C-quoted paths decode (a legit edit to a file
with a space in its name was reported as a stray, live), renames check
BOTH sides, and _within_allowed rejects traversal segments.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def ctl():
    # The gate was written against the pre-split controller; the functions
    # live in ratchetloop.porcelain now (re-exported via gitops).
    import ratchetloop.porcelain as p
    return p


ALLOWED = ["src", "tests"]


# --- unquoting ---------------------------------------------------------------

def test_plain_path_passes_through(ctl):
    assert ctl._unquote_porcelain_path("src/a.py") == "src/a.py"


def test_quoted_space(ctl):
    assert ctl._unquote_porcelain_path('"src/my file.py"') == "src/my file.py"


def test_quoted_escapes(ctl):
    assert ctl._unquote_porcelain_path(r'"src/a\tb.py"') == "src/a\tb.py"
    assert ctl._unquote_porcelain_path(r'"src/a\\b.py"') == "src/a\\b.py"
    assert ctl._unquote_porcelain_path(r'"src/a\"b.py"') == 'src/a"b.py'


def test_octal_escapes_are_utf8_bytes(ctl):
    """Consecutive octal escapes are BYTES of one UTF-8 character, not
    separate characters — decoding them one at a time yields mojibake."""
    assert ctl._unquote_porcelain_path(r'"src/caf\303\251.py"') == "src/café.py"


# --- stray detection ---------------------------------------------------------

def test_plain_allowed_is_not_stray(ctl):
    assert ctl._stray_paths(" M src/a.py\n", ALLOWED) == []


def test_plain_outside_is_stray(ctl):
    assert ctl._stray_paths(" M docs/a.md\n", ALLOWED) == ["docs/a.md"]


def test_quoted_allowed_is_not_stray(ctl):
    """The live failure: a legitimate edit to a file with a space in its name
    was reported as a stray because the quotes were never removed."""
    assert ctl._stray_paths(' M "src/my file.py"\n', ALLOWED) == []


def test_quoted_outside_is_stray_and_decoded(ctl):
    assert ctl._stray_paths(' M "docs/my file.md"\n', ALLOWED) == ["docs/my file.md"]


def test_rename_inside_allowed_is_not_stray(ctl):
    assert ctl._stray_paths("R  src/a.py -> src/b.py\n", ALLOWED) == []


def test_rename_from_outside_is_stray(ctl):
    """A rename REMOVES the old path. If that is outside the allowlist, the
    commit touches something out of scope."""
    strays = ctl._stray_paths("R  docs/a.md -> src/b.py\n", ALLOWED)
    assert "docs/a.md" in strays


def test_rename_to_outside_is_stray(ctl):
    strays = ctl._stray_paths("R  src/a.py -> docs/b.md\n", ALLOWED)
    assert "docs/b.md" in strays


def test_rename_with_quoted_sides(ctl):
    strays = ctl._stray_paths('R  "docs/my file.md" -> "src/b.py"\n', ALLOWED)
    assert strays == ["docs/my file.md"]


def test_filename_containing_the_arrow_is_not_split(ctl):
    """A quoted filename may legitimately contain ' -> '. Searching the whole
    line for it truncates the path and can turn a stray into an allowed one."""
    assert ctl._stray_paths(' M "docs/a -> b.md"\n', ALLOWED) == ["docs/a -> b.md"]


def test_order_and_no_duplicates(ctl):
    status = " M docs/a.md\n M src/ok.py\n M docs/b.md\n M docs/a.md\n"
    assert ctl._stray_paths(status, ALLOWED) == ["docs/a.md", "docs/b.md"]


def test_blank_lines_ignored(ctl):
    assert ctl._stray_paths("\n M src/a.py\n\n", ALLOWED) == []


# --- parse_porcelain compatibility -------------------------------------------

def test_parse_porcelain_keeps_shape(ctl):
    out = ctl._parse_porcelain(" M src/a.py\n?? src/b.py\n")
    assert out == [(" M", "src/a.py"), ("??", "src/b.py")]


def test_parse_porcelain_decodes_and_takes_new_side(ctl):
    out = ctl._parse_porcelain('R  "src/a b.py" -> "src/c d.py"\n')
    assert out == [("R ", "src/c d.py")]


# --- the guard must not weaken ----------------------------------------------

def test_within_allowed_still_rejects_traversal(ctl):
    assert ctl._within_allowed("src/../../etc/passwd", ALLOWED) is False


def test_within_allowed_plain_cases_unchanged(ctl):
    assert ctl._within_allowed("src/a.py", ALLOWED) is True
    assert ctl._within_allowed("src", ALLOWED) is True
    assert ctl._within_allowed("srcextra/a.py", ALLOWED) is False
