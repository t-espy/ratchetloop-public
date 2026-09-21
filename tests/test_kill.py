"""Kill drills against a real pipeline process (the Phase 4 exit, in fake form): `ratchetloop run`
runs as a subprocess with a fake grok CLI on PATH that leaves a half-done change and hangs. The
pipeline is SIGKILLed or SIGTERMed during that launch; what the run directory says afterwards, what
is left running, and whether `--continue` (in-process, fake workers) finishes the task are checked."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ratchetloop.cli import main
from runkit import MUL, done, install, make_repo, result_of, runs_root, verdict, write_task

FAKE_GROK = """#!/bin/sh
# Stands in for grok: a half-done change, a note that it started, then a hang.
printf 'def add(a, b):\\n    return a + b\\n\\n\\ndef mul(a, b):\\n    return a *\\n' > calc.py
if [ -f data.txt ]; then echo forged > data.txt; fi
echo "$$" > "$FAKE_GROK_STARTED.tmp" && mv "$FAKE_GROK_STARTED.tmp" "$FAKE_GROK_STARTED"
exec sleep 300
"""


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path)


@pytest.fixture
def fake_grok(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("grok", FAKE_GROK), ("codex", "#!/bin/sh\nexit 1\n")):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    started = tmp_path / "grok.started"
    monkeypatch.setenv("FAKE_GROK_STARTED", str(started))
    return started


def _pipeline(task) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "ratchetloop.cli", "run", str(task)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)


def _grok_pid(started, proc, timeout: float = 30.0) -> int:
    deadline = time.monotonic() + timeout
    while not started.exists():
        if proc.poll() is not None:
            raise AssertionError(f"the pipeline ended early: {proc.communicate()}")
        if time.monotonic() > deadline:
            proc.kill()
            raise AssertionError("the fake grok never started")
        time.sleep(0.05)
    return int(started.read_text().strip())


def _gone(pid: int, timeout: float = 10.0) -> bool:
    """True once `pid` is dead (a zombie awaiting its new parent counts); checks at least once."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.kill(pid, 0)
            with open(f"/proc/{pid}/stat") as fh:
                if fh.read().rsplit(")", 1)[1].split()[0] == "Z":
                    return True
        except (ProcessLookupError, FileNotFoundError):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def test_sigkill_mid_coder_leaves_a_record_a_later_run_completes(repo, tmp_path, monkeypatch,
                                                                 fake_grok):
    task = write_task(tmp_path, repo)
    proc = _pipeline(task)
    grok = _grok_pid(fake_grok, proc)
    proc.kill()
    proc.wait()
    dead_run = sorted((runs_root() / "add-mul").iterdir())[0]
    assert (dead_run / "lock").exists() and not (dead_run / "result.json").exists()
    assert not _gone(grok, timeout=0.5)  # the dead pipeline left its worker running
    try:
        fake = install(monkeypatch, [(lambda t: (t / "calc.py").write_text(MUL), done())],
                       [(None, verdict("APPROVE"))])
        assert main(["run", str(task), "--continue"]) == 0
        assert _gone(grok)  # reaped by the run that marked the dead one abandoned
    finally:
        _kill_group(grok)
    record = json.loads((dead_run / "result.json").read_text())
    assert (record["disposition"], record["reason"]) == ("FAILED", "abandoned")
    assert "last event: worker_start role=coder" in record["detail"]
    assert not (dead_run / "lock").exists()
    assert result_of()["disposition"] == "ACCEPT"
    assert "- calc.py" in fake.calls[0]["brief"].split("## Previous attempt")[1]


def _kill_group(pgid: int) -> None:
    try:  # never leave the fake behind, whatever failed
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def test_sigterm_mid_coder_stops_the_run_and_reaps_its_worker(repo, tmp_path, fake_grok):
    proc = _pipeline(write_task(tmp_path, repo))
    grok = _grok_pid(fake_grok, proc)
    try:
        proc.send_signal(signal.SIGTERM)
        _out, err = proc.communicate(timeout=60)
        assert proc.returncode == 5, err
        res = result_of()
        assert (res["disposition"], res["reason"]) == ("STOPPED", "killed")
        assert "SIGTERM" in res["detail"] and res["worktree"]
        assert _gone(grok)
        assert not list(runs_root().glob("add-mul/*/lock"))
    finally:
        proc.kill()
        _kill_group(grok)


def test_a_detached_run_returns_at_once_and_outlives_its_caller(repo, tmp_path, fake_grok,
                                                                capsys):
    task = write_task(tmp_path, repo)
    assert main(["run", str(task), "--detach"]) == 0
    out = json.loads(capsys.readouterr().out)
    pid = out["detached"]
    try:
        deadline = time.monotonic() + 30
        while not fake_grok.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert fake_grok.exists(), open(out["log"]).read()
        grok = int(fake_grok.read_text().strip())
        assert os.getsid(pid) == pid != os.getsid(0)  # its own session: our exit cannot take it
        assert out["run_dir"] and (Path(out["run_dir"]) / "lock").exists()
    finally:
        _kill_group(pid)
        if fake_grok.exists():
            _kill_group(int(fake_grok.read_text().strip()))
    assert _gone(grok)


def test_a_setup_output_the_dead_coder_rewrote_is_its_change(repo, tmp_path, monkeypatch,
                                                            fake_grok):
    # Phase 4 review, finding 4: leftovers were split by path only, so this rewrite escaped scope.
    task = write_task(tmp_path, repo, setup=["echo seed > data.txt"])
    proc = _pipeline(task)
    grok = _grok_pid(fake_grok, proc)
    proc.kill()
    proc.wait()
    try:
        install(monkeypatch, [(lambda t: (t / "calc.py").write_text(MUL), done())], [])
        assert main(["run", str(task), "--continue"]) == 4
    finally:
        _kill_group(grok)
    res = result_of()
    assert res["reason"] == "scope_violation" and "data.txt" in res["detail"]
