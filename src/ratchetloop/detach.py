"""`--detach` (CONTRACT.md §1): the same command line re-executed in its own session, so the run
outlives its caller — a systemd oneshot killed detached children (FAILURE_MODES.md §4), and a closed
terminal does the same. The caller has already been through admit, so a refusal still exits 2 in
the foreground; the child admits again for itself."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

WAIT_FOR_RUN_S = 10.0


def _runs(folder: Path) -> set[str]:
    return {p.name for p in folder.iterdir() if p.is_dir()} if folder.is_dir() else set()


def detach(argv: list[str], key: str, root: Path) -> int:
    """Start `ratchetloop <argv without --detach>` in a new session, logging to
    `<runs_root>/.detached/`; print its pid, its log and its run directory once it exists."""
    root, folder = Path(root), Path(root) / key
    logs = root / ".detached"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / f"{key}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.log"
    before = _runs(folder)
    with open(log, "w") as fh:
        proc = subprocess.Popen([sys.executable, "-m", "ratchetloop.cli",
                                 *[a for a in argv if a != "--detach"]],
                                stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                                start_new_session=True)
    run_dir = None
    deadline = time.monotonic() + WAIT_FOR_RUN_S
    while run_dir is None and proc.poll() is None and time.monotonic() < deadline:
        new = sorted(_runs(folder) - before)
        run_dir = folder / new[-1] if new else None
        time.sleep(0.1)
    print(json.dumps({"detached": proc.pid, "key": key,
                      "run_dir": str(run_dir) if run_dir else None, "log": str(log)}))
    code = proc.poll()
    if code not in (None, 0):  # it ended before its run began: say why here, not only in the log
        print(log.read_text()[-2000:], file=sys.stderr, end="")
    return 0 if code is None else code
