"""Parse `git status --porcelain` v1 — including C-quoted paths — and the
allowed-paths guard built on it.

These functions are the only thing standing between a misbehaving builder
and a commit outside its task's `allowed_paths`, so every ambiguity here
resolves fail-closed: a path we cannot confidently decode is reported as a
stray (blocking a commit) rather than silently allowed.

Git C-quotes any path containing a space, double quote, backslash, control
character, or non-ASCII byte: the path is wrapped in double quotes and
special characters become backslash escapes (\\ \" \t \n \r, plus \\ooo for
a single BYTE as three octal digits). Consecutive octal escapes are the
bytes of one UTF-8 character and must be decoded together — decoding them
one character at a time yields mojibake that never matches an allowlist.
"""
from __future__ import annotations

from pathlib import PurePosixPath

_OCTAL = frozenset("01234567")
_SIMPLE_ESCAPES = {"\\": b"\\", '"': b'"', "t": b"\t", "n": b"\n", "r": b"\r"}


def _unquote_porcelain_path(raw: str) -> str:
    if not (len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"'):
        return raw
    inner = raw[1:-1]
    out = bytearray()  # accumulate BYTES so \ooo runs decode as UTF-8
    i = 0
    while i < len(inner):
        ch = inner[i]
        if ch != "\\":
            out += ch.encode("utf-8")
            i += 1
            continue
        nxt = inner[i + 1:i + 2]
        if nxt in _SIMPLE_ESCAPES:
            out += _SIMPLE_ESCAPES[nxt]
            i += 2
        elif len(inner) >= i + 4 and set(inner[i + 1:i + 4]) <= _OCTAL:
            out.append(int(inner[i + 1:i + 4], 8))
            i += 4
        else:
            # Unknown escape: keep the raw quoted form so the path can only
            # ever be reported as a stray, never matched as allowed.
            return raw
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return raw  # same fail-closed posture as above


def _find_unquoted_arrow(rest: str) -> int | None:
    """Index of the ' -> ' rename separator, skipping quoted regions.

    When either side of a rename needs quoting, git quotes that side
    individually — the separator itself is never inside quotes. A quoted
    filename may legitimately CONTAIN ' -> ', so a plain str.find would
    split inside the name and can turn a stray into an allowed path.
    """
    in_quotes = False
    i = 0
    while i < len(rest):
        ch = rest[i]
        if in_quotes and ch == "\\":
            i += 2  # an escaped \" must not toggle the quote state
        elif ch == '"':
            in_quotes = not in_quotes
            i += 1
        elif not in_quotes and rest.startswith(" -> ", i):
            return i
        else:
            i += 1
    return None


def _split_entry(line: str) -> tuple[str, str, str | None]:
    """One porcelain line -> (code, path, old_path-or-None), decoded."""
    code, rest = line[:2], line[3:]
    if code[:1] in ("R", "C"):
        sep = _find_unquoted_arrow(rest)
        if sep is not None:
            old = _unquote_porcelain_path(rest[:sep])
            new = _unquote_porcelain_path(rest[sep + 4:])
            return code, new, old
    return code, _unquote_porcelain_path(rest), None


def _parse_porcelain(status: str) -> list[tuple[str, str]]:
    """(code, path) pairs; renames report the NEW side, as callers expect."""
    return [(_e[0], _e[1]) for _e in map(_split_entry, _lines(status))]


def _lines(status: str) -> list[str]:
    return [ln for ln in status.splitlines() if ln.strip()]


def _within_allowed(path: str, allowed_paths: list[str]) -> bool:
    pp = PurePosixPath(path)
    # PurePosixPath does NOT normalise traversal: 'src/../../etc/passwd'
    # keeps 'src' among its parents and would pass the prefix check while
    # pointing outside the tree. Any dotted segment fails the guard.
    if any(part in (".", "..") for part in pp.parts):
        return False
    for allowed in allowed_paths:
        pa = PurePosixPath(allowed)
        if pp == pa or pa in pp.parents:
            return True
    return False


def _stray_paths(status: str, allowed_paths: list[str]) -> list[str]:
    """Every changed path outside the allowlist, decoded, in order, once.

    Renames check BOTH sides: the old path is REMOVED by the rename, so an
    old side outside the allowlist is an out-of-scope change even when the
    new side lands inside it.
    """
    strays: list[str] = []
    seen: set[str] = set()
    for line in _lines(status):
        _code, new, old = _split_entry(line)
        for p in ([old] if old is not None else []) + [new]:
            if p not in seen and not _within_allowed(p, allowed_paths):
                strays.append(p)
                seen.add(p)
    return strays
