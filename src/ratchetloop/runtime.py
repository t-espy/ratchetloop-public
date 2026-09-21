"""Environment helpers for worker launches."""
from __future__ import annotations

import os


def safepath_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """A copy of the environment with PYTHONSAFEPATH=1, so Python never puts the working directory
    on sys.path and silently imports the tree it is editing (predecessor spark #440-8)."""
    env = dict(base if base is not None else os.environ)
    env["PYTHONSAFEPATH"] = "1"
    return env
