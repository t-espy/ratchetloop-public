"""Residual review findings (CONTRACT.md §9.5, D35): what a reviewer still asked for after a stage's
last review round, parsed from the review summary into data, written as `docs/REVIEW_NOTES.md` on
the stage branch and as `residuals.json` in the idea's run directory, and closed when a later
stage's review says a finding was fixed. The parser reads the shapes the reviewer briefs mandate —
`bug — file:line: text` findings and `fixed:` / `unfixed:` lines — and keeps anything else whole."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

TAGS = ("bug", "suggestion", "nit")
NOTES_PATH = "docs/REVIEW_NOTES.md"
RESIDUALS = "residuals.json"
NOTES_HEADER = (
    "# Review notes\n\nResidual findings the pipeline carried forward when a stage's review rounds "
    "ran out with green checks (CONTRACT.md §9.5). A later phase fixes the ones its files touch; "
    "the rest wait for the merge.\n")
_DASH = r"\s*[—–-]+\s*"
_TAGGED = re.compile(
    r"^\s*(?P<tag>bug|suggestion|nit)" + _DASH
    + r"(?:`?(?P<file>[\w./-]+\.\w+)`?(?::(?P<line>\d+(?:-\d+)?))?\s*:\s*)?(?P<text>.*)$",
    re.S | re.I)
_FILE_LINE = re.compile(r"(?P<file>[\w./-]+\.\w+):(?P<line>\d+(?:-\d+)?)")
# Chunk boundaries: a tagged finding at a line start, a "finding N" statement, a fixed:/unfixed:/
# resolved:/notes: line. Each chunk is classified on its own.
_SPLIT = re.compile(
    r"(?:\n(?=\s*(?:bug|suggestion|nit)\s*[—–-])|(?<!round-1 )(?=finding \d+\b)"
    r"|(?=round-1 finding \d+\b)|\n(?=\s*(?:fixed|unfixed|resolved|notes?):))", re.I)
_UNFIXED = re.compile(
    r"^\s*unfixed:|\bfinding \d+\b.*?\b(?:remains|unfixed|not (?:yet )?(?:fixed|addressed|"
    r"resolved)|still|is open)\b", re.I | re.S)
_FIXED = re.compile(
    r"^\s*(?:fixed|resolved):|\bfinding \d+\b.*?\b(?:fixed|addressed|resolved|closed)\b",
    re.I | re.S)
_NOTES = re.compile(r"^\s*notes?:", re.I)


def _chunks(summary: str) -> list[str]:
    out: list[str] = []
    for para in re.split(r"\n\s*\n", summary or ""):
        out += [c.strip() for c in _SPLIT.split(para) if c and c.strip()]
    return out


def _entry(stage: str, round_: int, tag: str, file: str | None, line: str | None, text: str,
           status: str) -> dict[str, Any]:
    return {"stage": stage, "round": round_, "tag": tag, "file": file, "line": line,
            "text": " ".join(text.split()), "status": status, "resolved_by": None}


def parse_findings(summary: str, stage: str, round_: int) -> list[dict[str, Any]]:
    """The findings a review summary still holds open. A tagged finding keeps its tag, file and
    line; a "finding N remains" or `unfixed:` statement is an unfixed bug (its file:line when the
    text names one); a `notes:` observation is a nit; a "finding N is fixed" or `fixed:`/`resolved:`
    statement is dropped; anything else is kept whole as a `note` with no file, so nothing a
    reviewer wrote is lost."""
    out: list[dict[str, Any]] = []
    for chunk in _chunks(summary):
        tagged = _TAGGED.match(chunk)
        if tagged:
            out.append(_entry(stage, round_, tagged["tag"].lower(), tagged["file"],
                              tagged["line"], tagged["text"], "open"))
        elif _UNFIXED.search(chunk):
            loc = _FILE_LINE.search(chunk)
            out.append(_entry(stage, round_, "bug", loc["file"] if loc else None,
                              loc["line"] if loc else None, chunk, "unfixed"))
        elif _NOTES.match(chunk):
            out.append(_note_entry(stage, round_, chunk))
        elif _FIXED.search(chunk):
            continue
        else:
            out.append(_entry(stage, round_, "note", None, None, chunk, "open"))
    return out


def _note_entry(stage: str, round_: int, chunk: str) -> dict[str, Any]:
    loc = _FILE_LINE.search(chunk)
    text = _NOTES.sub("", chunk, count=1).strip()
    return _entry(stage, round_, "nit", loc["file"] if loc else None,
                  loc["line"] if loc else None, text, "open")


def parse_notes(summary: str, stage: str, round_: int) -> list[dict[str, Any]]:
    """Only the `notes:` observations of a review — what an ACCEPTed round 2 still carries."""
    return [_note_entry(stage, round_, c) for c in _chunks(summary) if _NOTES.match(c)]


def resolved_statements(summary: str) -> list[str]:
    """The chunks of a later review that say something was fixed or resolved."""
    return [c for c in _chunks(summary) if _FIXED.search(c) and not _UNFIXED.search(c)]


def mark_resolved(residuals: list[dict[str, Any]], stage: str, summary: str) -> list[str]:
    """Close every open residual with a file that a fixed/resolved statement in `summary` names
    (by `file:line` or by the file's path); returns what was closed."""
    statements = resolved_statements(summary)
    closed: list[str] = []
    for r in residuals:
        if r.get("resolved_by") or not r.get("file"):
            continue
        loc = f"{r['file']}:{r['line']}" if r.get("line") else r["file"]
        if any(loc in s or r["file"] in s or Path(r["file"]).name in s for s in statements):
            r["resolved_by"] = stage
            closed.append(loc)
    return closed


def as_lines(residuals: list[dict[str, Any]]) -> list[str]:
    """One line per open residual, the shape the coder and reviewer briefs carry."""
    lines = []
    for r in residuals:
        if r.get("resolved_by"):
            continue
        loc = f"`{r['file']}:{r['line']}`" if r.get("line") else (f"`{r['file']}`" if r.get("file") else "")
        where = f" {loc}" if loc else ""
        lines.append(f"{r['tag']}{where} ({r['status']}, from {r['stage']} round {r['round']}): "
                     f"{r['text']}")
    return lines


def notes_section(stage: str, run_id: str, residuals: list[dict[str, Any]]) -> str:
    body = "\n".join(f"- {ln}" for ln in as_lines(residuals)) or "- (nothing parsed)"
    return f"\n## {stage} (run {run_id})\n\n{body}\n"


def load(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def save(path: Path, residuals: list[dict[str, Any]]) -> None:
    Path(path).write_text(json.dumps(residuals, indent=1) + "\n")
