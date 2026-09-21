"""Setup and checks: an explicit environment, exit codes decide, empty checks never pass, and a
command past its timeout has its whole process group killed."""
import os
import sys
import time
from pathlib import Path

import pytest

from ratchetloop.checks import check_env, checks_cwd, run_checks, run_setup


def test_empty_check_list_fails_closed(tmp_path):
    assert run_checks([], tmp_path, check_env()) == (False, [])


def test_exit_codes_decide_and_output_is_logged(tmp_path):
    log = tmp_path / "run" / "checks.1.log"
    ok, results = run_checks(["echo fine", "echo broken >&2; exit 3"], tmp_path, check_env(),
                             log=log)
    assert ok is False
    assert [r["exit"] for r in results] == [0, 3]
    assert "broken" in results[1]["tail"]
    text = log.read_text()
    assert "$ echo fine" in text and "[exit 3]" in text


def test_a_failing_command_in_a_pipeline_is_red(tmp_path):
    # Phase 2 review finding 4: `pytest | tee log` was green whenever tee succeeded.
    ok, results = run_checks(["false | cat"], tmp_path, check_env())
    assert not ok and results[0]["exit"] == 1


def test_missing_interpreter_is_red_not_rewritten(tmp_path):
    ok, results = run_checks(["venv/bin/python -c 'pass'"], tmp_path, check_env())
    assert not ok and results[0]["exit"] == 127


def test_timeout_kills_the_whole_process_group(tmp_path):
    marker = tmp_path / "grandchild.pid"
    t0 = time.monotonic()
    ok, results = run_checks([f"(sleep 30 & echo $! > {marker}; wait)"], tmp_path, check_env(),
                             timeout=0.5)
    assert not ok and results[0]["exit"] == 124 and "timed out" in results[0]["tail"]
    assert time.monotonic() - t0 < 10
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


def test_environment_is_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAKY_SECRET", "x")
    monkeypatch.setenv("PYTHONPATH", "/somewhere")
    monkeypatch.setenv("VIRTUAL_ENV", sys.prefix)
    env = check_env({"DB_DSN": "dbname=test"})
    assert not {"LEAKY_SECRET", "PYTHONPATH", "VIRTUAL_ENV", "PYTHONSAFEPATH"} & set(env)
    assert env["DB_DSN"] == "dbname=test" and "HOME" in env
    assert str(Path(sys.prefix) / "bin") not in env["PATH"].split(os.pathsep)
    assert env["GIT_CONFIG_VALUE_0"] == "never"  # a check cannot push either
    ok, results = run_checks(['test -z "$LEAKY_SECRET" && test "$DB_DSN" = dbname=test'],
                             tmp_path, env)
    assert ok, results


def test_setup_stops_at_the_first_failure(tmp_path):
    ok, results = run_setup(["true", "exit 2", "touch never"], tmp_path, check_env())
    assert not ok and len(results) == 2
    assert not (tmp_path / "never").exists()
    assert run_setup([], tmp_path, check_env()) == (True, [])


def test_checks_cwd_must_stay_inside_the_worktree(tmp_path):
    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    assert checks_cwd(tree, "sub") == (tree / "sub").resolve()
    assert checks_cwd(tree, ".") == tree.resolve()
    (tree / "escape").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="leaves the worktree"):
        checks_cwd(tree, "escape")
