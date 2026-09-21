"""Residual findings (CONTRACT.md §9.5): parsing what a review still holds open — the two real
summaries that stopped the market-update build on 2026-09-16, the `fixed:`/`unfixed:` lines the
round-2 brief mandates — and closing them from a later review."""
from __future__ import annotations

from pathlib import Path

from ratchetloop.residuals import (
    as_lines, mark_resolved, notes_section, parse_findings, parse_notes,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / f"review_summary_{name}.txt").read_text()


def test_the_parse_summary_stop_yields_its_one_tagged_finding():
    found = parse_findings(_fixture("parse-summary"), "mu--parse-summary", 2)
    assert len(found) == 1
    r = found[0]
    assert (r["tag"], r["file"], r["line"], r["status"]) == (
        "bug", "src/market_update/metrics.py", "215-216", "open")
    assert r["text"].startswith("active_reductions still reports missing_column")
    assert (r["stage"], r["round"], r["resolved_by"]) == ("mu--parse-summary", 2, None)


def test_the_area_filter_stop_yields_its_one_unfixed_finding():
    # "Finding 2 remains: …" between two "is fixed" statements, none in the tagged shape.
    found = parse_findings(_fixture("area-filter"), "mu--area-filter", 2)
    assert len(found) == 1
    r = found[0]
    assert (r["tag"], r["status"], r["file"]) == ("bug", "unfixed", None)
    assert "require_area_columns()" in r["text"] and "Finding 3" not in r["text"]


def test_fix_check_lines_decide_status_and_notes_become_nits():
    summary = ("fixed: 1 — the date parser is strict now\n"
               "unfixed: 2 — src/a.py:10 the guard is still never called\n"
               "notes: a docstring in src/b.py:4 could name the contract\n")
    found = parse_findings(summary, "s", 2)
    assert [(r["tag"], r["status"], r["file"], r["line"]) for r in found] == [
        ("bug", "unfixed", "src/a.py", "10"), ("nit", "open", "src/b.py", "4")]
    assert found[1]["text"] == "a docstring in src/b.py:4 could name the contract"
    assert parse_notes(summary, "s", 2) == found[1:]


def test_tagged_findings_take_any_dash_and_need_no_file():
    found = parse_findings("bug - src/b.py:7: off by one\n\nnit — wording in the README", "s", 1)
    assert [(r["tag"], r["file"], r["line"], r["text"]) for r in found] == [
        ("bug", "src/b.py", "7", "off by one"), ("nit", None, None, "wording in the README")]


def test_prose_that_fits_no_shape_is_kept_whole_as_a_note():
    found = parse_findings("The change looks incomplete but I could not pin it down.", "s", 2)
    assert [(r["tag"], r["file"]) for r in found] == [("note", None)]
    assert found[0]["text"] == "The change looks incomplete but I could not pin it down."


def test_a_later_review_closes_residuals_it_names_and_no_others():
    residuals = parse_findings("bug — src/a.py:10: guard never called\n\n"
                               "bug — src/c.py:3: wrong sign\n\nnit — wording", "one", 2)
    closed = mark_resolved(residuals, "two", "resolved: src/a.py:10 the guard runs in filter()\n"
                                             "fixed: 2 — c.py sign corrected")
    assert closed == ["src/a.py:10", "src/c.py:3"]
    assert [r["resolved_by"] for r in residuals] == ["two", "two", None]
    assert mark_resolved(residuals, "three", "unfixed: 1 — src/a.py:10 still") == []
    assert as_lines(residuals) == ["nit (open, from one round 2): wording"]


def test_notes_section_lists_open_residuals_under_the_stage():
    residuals = parse_findings("bug — src/a.py:10: guard never called", "calc--mul", 2)
    section = notes_section("calc--mul", "20260916T000000Z", residuals)
    assert section.startswith("\n## calc--mul (run 20260916T000000Z)\n")
    assert "- bug `src/a.py:10` (open, from calc--mul round 2): guard never called" in section
    assert "(nothing parsed)" in notes_section("x", "r", [])
