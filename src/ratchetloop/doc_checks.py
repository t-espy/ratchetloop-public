"""Built-in checks for `kind: doc` tasks (CONTRACT.md §2.1, D25): a document must have changed, and
every relative markdown link in a changed file must resolve. Staying inside `allowed_paths` is the
scope check's job. These judge presence and placement, never content; content is the reviewer's."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
MARKDOWN = (".md", ".markdown")


def broken_links(tree: Path, paths: Iterable[str]) -> list[str]:
    out: list[str] = []
    for rel in paths:
        path = Path(tree) / rel
        if path.suffix.lower() not in MARKDOWN or not path.is_file():
            continue
        for target in LINK.findall(path.read_text(errors="replace")):
            if "://" in target or target.startswith(("#", "mailto:")):
                continue
            local = target.split("#", 1)[0]
            if local and not (path.parent / local).exists():
                out.append(f"{rel}: {target}")
    return out


def doc_check_results(tree: Path, changed: list[str]) -> list[dict[str, Any]]:
    """Results in `run_checks`' shape, so the loop treats them like any other check."""
    broken = broken_links(tree, changed)
    return [
        {"cmd": "[built-in] a document changed", "exit": 0 if changed else 1, "wall_s": 0.0,
         "tail": "" if changed else "no file changed"},
        {"cmd": "[built-in] relative links resolve", "exit": 1 if broken else 0, "wall_s": 0.0,
         "tail": "\n".join(broken)},
    ]
