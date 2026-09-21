"""Worker adapter: structured events, argv pins, silence, exit file."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ratchetloop import workers
from ratchetloop.worker_io import (
    claude_events, copilot_events, codex_events, grok_events,
    load_worker_outcome,
)
from ratchetloop.workers import (
    COPILOT_LAUNCHER, GROK_DENY_RULES, GROK_SANDBOX, PROVIDERS, _kill_stale,
    claude_argv, copilot_argv, codex_argv, grok_argv, run_worker, worker_env,
)

FAKE = Path(__file__).resolve().parent / "fake_struct_worker.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _install_fake(monkeypatch, mode: str, extra: list[str] | None = None) -> None:
    def argv(brief, workdir, out_file, role="coder"):
        cmd = [sys.executable, str(FAKE), mode]
        if extra:
            cmd.extend(extra)
        return cmd

    monkeypatch.setitem(PROVIDERS, "grok", {**PROVIDERS["grok"], "argv": argv})


def test_grok_argv_pinned(tmp_path):
    help_text = (FIXTURES / "grok_help.txt").read_text()
    for flag in ("--output-format", "streaming-messages-json",
                 "--include-partial-messages"):
        assert flag in help_text
    assert "Implies --output-format json" in help_text
    argv = grok_argv(str(tmp_path / "b.md"), str(tmp_path), str(tmp_path / "out"))
    assert argv[:6] == [
        "grok", "--prompt-file", str(tmp_path / "b.md"),
        "--permission-mode", "bypassPermissions", "--cwd",
    ]
    assert str(tmp_path) in argv
    assert "--sandbox" in argv and GROK_SANDBOX in argv
    for rule in GROK_DENY_RULES:
        assert rule in argv
    assert argv[argv.index("--deny") + 1] == "Bash(git push*)"
    assert "Bash(gh *)" in argv
    assert argv[argv.index("--output-format") + 1] == "streaming-messages-json"
    assert "--include-partial-messages" in argv
    assert "--json-schema" not in argv
    assert PROVIDERS["grok"]["transport"] == "cli"
    assert PROVIDERS["grok"]["mode"] == "structured"
    assert PROVIDERS["grok"]["argv"] is grok_argv
    assert PROVIDERS["grok"]["events"] is grok_events


def test_codex_argv_pinned(tmp_path):
    help_text = (FIXTURES / "codex_exec_help.txt").read_text()
    assert "--json" in help_text
    assert "--output-schema" in help_text
    brief = tmp_path / "b.md"
    brief.write_text("do the thing")
    out = str(tmp_path / "out")
    argv = codex_argv(str(brief), str(tmp_path), out)
    assert argv[:9] == [
        "codex", "exec", "-C", str(tmp_path), "-s", "workspace-write",
        "-c", "shell_environment_policy.inherit=all", "--json",
    ]
    assert "shell_environment_policy.inherit=all" in help_text  # the documented form
    assert "-o" in argv
    assert argv[argv.index("-o") + 1] == out + ".last"
    assert out not in argv
    assert "do the thing" not in argv  # brief goes in on stdin (item 79 run 2)
    assert PROVIDERS["codex"]["stdin_brief"] is True
    assert "--output-schema" in argv
    schema = Path(argv[argv.index("--output-schema") + 1])
    assert schema.is_file()
    assert json.loads(schema.read_text())["required"] == ["status", "reason", "summary"]
    assert PROVIDERS["codex"]["argv"] is codex_argv
    assert PROVIDERS["codex"]["mode"] == "structured"
    assert PROVIDERS["codex"]["events"] is codex_events


def test_codex_reviewer_is_read_only_and_findings_may_be_null(tmp_path):
    out = str(tmp_path / "out")
    argv = codex_argv(str(tmp_path / "b.md"), str(tmp_path), out, "reviewer")
    assert argv[argv.index("-s") + 1] == "read-only"
    schema = json.loads(Path(argv[argv.index("--output-schema") + 1]).read_text())
    assert schema["required"] == ["status", "reason", "summary", "findings", "blocking"]  # strict
    counts, null = schema["properties"]["findings"]["anyOf"]
    assert counts["required"] == ["bug", "suggestion", "nit"] and null == {"type": "null"}


def test_unknown_role_is_refused(tmp_path):
    for builder in (grok_argv, codex_argv, claude_argv, copilot_argv):
        with pytest.raises(ValueError, match="planner"):
            builder(str(tmp_path / "b.md"), str(tmp_path), str(tmp_path / "out"), "planner")


def test_claude_argv_pinned():
    help_text = (FIXTURES / "claude_help.txt").read_text()
    assert "--output-format" in help_text
    assert "stream-json" in help_text
    assert "--verbose" in help_text
    argv = claude_argv("b", "w", "o")
    assert argv[:5] == [
        "claude", "-p", "--output-format", "stream-json", "--verbose",
    ]
    assert "--permission-mode" in argv
    assert "--allowedTools" in argv
    assert PROVIDERS["claude"]["argv"] is claude_argv
    assert PROVIDERS["claude"]["mode"] == "structured"
    assert PROVIDERS["claude"]["events"] is claude_events
    assert PROVIDERS["claude"]["stdin_brief"] is True
    assert "via_run_role" not in PROVIDERS["claude"]


def test_claude_argv_verbose_next_to_print_and_stream_json():
    for role in ("coder", "reviewer"):
        argv = claude_argv("b", "w", "o", role)
        assert "-p" in argv
        assert "--verbose" in argv
        assert "stream-json" in argv
        p = argv.index("-p")
        assert argv[p:p + 4] == ["-p", "--output-format", "stream-json", "--verbose"]


def test_copilot_argv_pinned(tmp_path):
    help_text = (FIXTURES / "copilot_help.txt").read_text()
    for flag in ("--output-format", "--stream", "--usage-output-file",
                 "--add-dir", "--allow-tool", "--deny-tool", "--no-ask-user",
                 "--disable-builtin-mcps", "--max-ai-credits"):
        assert flag in help_text
    assert "required for" in help_text
    assert "non-interactive mode" in help_text
    assert "--yolo" in help_text and "--allow-all" in help_text
    brief = tmp_path / "in" / "brief.md"
    brief.parent.mkdir()
    brief.write_text("do the thing")
    out = str(tmp_path / "out")
    argv = copilot_argv(str(brief), str(tmp_path), out)
    assert argv[0] == "copilot"
    inbox = Path(out + ".in").resolve()
    assert argv[argv.index("-p") + 1] == COPILOT_LAUNCHER.format(brief=inbox / "brief.md")
    assert "do the thing" not in " ".join(argv)  # the brief stays in its file (D27)
    assert argv[argv.index("-C") + 1] == str(tmp_path)
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--stream") + 1] == "on"
    assert argv[argv.index("--usage-output-file") + 1] == out + ".usage"
    dirs = [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
    assert dirs == [str(tmp_path), str(inbox)]
    assert "--no-ask-user" in argv
    assert "--disable-builtin-mcps" in argv  # no GitHub API tools around git containment
    assert "--allow-tool=write" in argv
    assert "--deny-tool=shell" in argv  # copilot's shell is unsandboxed by default
    assert "--yolo" not in argv and "--allow-all" not in argv
    assert "--allow-all-tools" not in argv
    assert PROVIDERS["copilot"]["transport"] == "cli"
    assert PROVIDERS["copilot"]["mode"] == "structured"
    assert PROVIDERS["copilot"]["argv"] is copilot_argv
    assert PROVIDERS["copilot"]["events"] is copilot_events


def test_copilot_large_brief_never_reaches_argv(tmp_path):
    # predecessor put the whole brief on copilot's argv and failed typed above 100 KB (MAX_ARG_STRLEN).
    brief = tmp_path / "b.md"
    brief.write_text("\u4e00" * 200000)
    argv = copilot_argv(str(brief), str(tmp_path), str(tmp_path / "out"))
    assert sum(len(a.encode()) for a in argv) < 4096


@pytest.mark.parametrize("line,kind", [
    ('{"type":"progress","text":"hi"}', "progress"),
    ('{"type":"tool","name":"Read"}', "tool"),
    ('{"type":"result","status":"done","reason":"","summary":"ok"}', "result"),
    ('{"type":"error","code":"boom"}', "error"),
    ('{"type":"usage","input_tokens":1}', "usage"),
])
def test_parsers_each_jsonl_kind(line, kind):
    for parser in (grok_events, codex_events, claude_events, copilot_events):
        ev = parser(line)
        assert ev is not None
        assert ev.kind == kind
        assert ev.raw == line


def test_parser_provider_shapes():
    stream = grok_events('{"type":"stream_event","text":"delta"}')
    assert stream is not None and stream.kind == "progress"
    tool = codex_events(
        '{"type":"item.started","item":{"type":"command_execution"}}')
    assert tool is not None and tool.kind == "tool"
    result = claude_events(
        '{"type":"result","result":"ok","usage":{"input_tokens":1}}')
    assert result is not None and result.kind == "result"
    assert copilot_events("not json") is None


def test_fake_provider_emits_each_kind(tmp_path, monkeypatch):
    _install_fake(monkeypatch, "all_kinds")
    out = str(tmp_path / "out")
    outcome = run_worker(
        "coder", "grok", str(tmp_path / "b"), str(tmp_path), out, timeout=5)
    assert outcome.kind == "done"
    assert outcome.summary == "ok"
    hb = json.loads(Path(out + ".events").read_text())
    assert hb["kind"] == "result"
    assert hb["n"] == 4
    assert "at" in hb
    exit_body = json.loads(Path(out + ".exit").read_text())
    assert exit_body["kind"] == "done"
    assert exit_body["exit"] == 0
    loaded = load_worker_outcome(out)
    assert loaded is not None and loaded.kind == "done"


def test_silence_timeout_kills_and_reports(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKER_SILENCE_S", "0.4")
    _install_fake(monkeypatch, "silence", ["30"])
    out = str(tmp_path / "out")
    t0 = time.time()
    outcome = run_worker(
        "coder", "grok", str(tmp_path / "b"), str(tmp_path), out,
        timeout=8, wall_budget_s=8)
    assert time.time() - t0 < 4
    assert outcome.kind == "silence"
    assert outcome.reason == "silence"
    exit_body = json.loads(Path(out + ".exit").read_text())
    assert exit_body["kind"] == "silence"
    pidfile = Path(out + ".pid")
    if pidfile.is_file():
        try:
            pgid = int(pidfile.read_text().strip())
            os.killpg(pgid, 0)
            raise AssertionError("silence kill left the process group alive")
        except OSError:
            pass


def test_what_a_worker_leaves_in_its_group_dies_with_the_launch(tmp_path, monkeypatch):
    # A reviewer's `dotnet test` left a compiler server that broke the next builds (Donchian,
    # 2026-09-12); a group was reaped only when the pipeline stopped a launch, never on a clean exit.
    pidf = tmp_path / "child.pid"
    _install_fake(monkeypatch, "leave_child", [str(pidf)])
    out = str(tmp_path / "out")
    outcome = run_worker("coder", "grok", str(tmp_path / "b"), str(tmp_path), out, timeout=10)
    child = int(pidf.read_text())
    try:
        assert outcome.kind == "done"
        with pytest.raises(OSError):
            os.killpg(int(Path(out + ".pid").read_text()), 0)
    finally:
        try:
            os.kill(child, 9)
        except OSError:
            pass


def test_result_schema_violation_is_typed_failure(tmp_path, monkeypatch):
    _install_fake(monkeypatch, "bad_result")
    out = str(tmp_path / "out")
    outcome = run_worker(
        "coder", "grok", str(tmp_path / "b"), str(tmp_path), out, timeout=5)
    assert outcome.kind == "invalid_result"
    assert outcome.reason == "invalid result object"
    assert json.loads(Path(out + ".exit").read_text())["kind"] == "invalid_result"


def test_missing_result_is_typed_failure(tmp_path, monkeypatch):
    _install_fake(monkeypatch, "none")
    out = str(tmp_path / "out")
    outcome = run_worker(
        "coder", "grok", str(tmp_path / "b"), str(tmp_path), out, timeout=5)
    assert outcome.kind == "invalid_result"
    assert outcome.reason == "missing result object"


def test_exit_code_error_path(tmp_path, monkeypatch):
    _install_fake(monkeypatch, "exit", ["7"])
    out = str(tmp_path / "out")
    outcome = run_worker(
        "coder", "grok", str(tmp_path / "b"), str(tmp_path), out, timeout=5)
    assert outcome.kind == "error"
    assert outcome.exit_code == 7
    assert json.loads(Path(out + ".exit").read_text())["exit"] == 7


def test_quota_event_is_typed_quota(tmp_path, monkeypatch):
    _install_fake(monkeypatch, "quota")
    out = str(tmp_path / "out")
    outcome = run_worker(
        "coder", "grok", str(tmp_path / "b"), str(tmp_path), out, timeout=5)
    assert outcome.kind == "quota"
    assert load_worker_outcome(out).kind == "quota"


def test_blocked_result_field(tmp_path, monkeypatch):
    _install_fake(monkeypatch, "blocked")
    out = str(tmp_path / "out")
    outcome = run_worker(
        "coder", "grok", str(tmp_path / "b"), str(tmp_path), out, timeout=5)
    assert outcome.kind == "blocked"
    assert outcome.reason == "cannot"


def test_exit_file_reaps_live_pgid_before_reuse(tmp_path, monkeypatch):
    proc = subprocess.Popen(
        ["sleep", "30"], start_new_session=True, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pgid = os.getpgid(proc.pid)
    out = tmp_path / "out.txt"
    out.write_text("hello\n")
    (tmp_path / "out.txt.exit").write_text(
        json.dumps({"exit": 0, "kind": "done", "reason": "", "summary": "ok"}) + "\n")
    (tmp_path / "out.txt.pid").write_text(str(pgid))
    called = []

    def boom(*a, **k):
        called.append(1)
        raise AssertionError("must not launch")

    monkeypatch.setattr("ratchetloop.worker_run.subprocess.Popen", boom)
    outcome = run_worker("coder", "grok", str(tmp_path / "b"), str(tmp_path), str(out))
    assert outcome.kind == "done"
    assert called == []
    proc.wait(timeout=5)
    with pytest.raises(OSError):
        os.killpg(pgid, 0)


def test_log_result_recovers_without_exit_file(tmp_path, monkeypatch):
    out = tmp_path / "out.txt"
    out.write_text(json.dumps({
        "type": "result", "status": "done", "reason": "", "summary": "ok",
    }) + "\n")
    called = []

    def boom(*a, **k):
        called.append(1)
        raise AssertionError("must not launch")

    monkeypatch.setattr("ratchetloop.worker_run.subprocess.Popen", boom)
    outcome = run_worker("coder", "grok", str(tmp_path / "b"), str(tmp_path), str(out))
    assert outcome.kind == "done"
    assert called == []
    assert json.loads((tmp_path / "out.txt.exit").read_text())["kind"] == "done"


def test_exit_file_is_idempotent(tmp_path, monkeypatch):
    out = tmp_path / "out.txt"
    out.write_text("hello\n")
    (tmp_path / "out.txt.exit").write_text(
        json.dumps({"exit": 0, "kind": "done", "reason": "", "summary": "ok"}) + "\n")
    called = []

    def boom(*a, **k):
        called.append(1)
        raise AssertionError("must not launch")

    monkeypatch.setattr("ratchetloop.worker_run.subprocess.Popen", boom)
    outcome = run_worker("coder", "grok", str(tmp_path / "b"), str(tmp_path), str(out))
    assert outcome.kind == "done"
    assert called == []


def test_stale_pidfile_kills_live_pgid(tmp_path):
    proc = subprocess.Popen(
        ["sleep", "30"], start_new_session=True, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pgid = os.getpgid(proc.pid)
    pidfile = tmp_path / "out.pid"
    pidfile.write_text(str(pgid))
    _kill_stale(pidfile)
    proc.wait(timeout=5)
    with pytest.raises(OSError):
        os.killpg(pgid, 0)


def test_worker_env_blocks_git_push_allows_commit(tmp_path):
    repo = tmp_path / "repo"
    bare = tmp_path / "bare.git"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master"], cwd=repo, check=True)
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("a\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    env = worker_env(gh_dir=str(tmp_path / "gh"))
    commit = subprocess.run(
        ["git", "commit", "-m", "ok"], cwd=repo, env=env,
        capture_output=True, text=True,
    )
    assert commit.returncode == 0, commit.stderr
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=repo, check=True)
    push = subprocess.run(
        ["git", "push", "origin", "HEAD"], cwd=repo, env=env,
        capture_output=True, text=True,
    )
    assert push.returncode != 0
    err = (push.stderr + push.stdout).lower()
    assert "not allowed" in err or "protocol" in err or "disabled" in err


def _pipe_popen(captured, payload: bytes, stderr: bytes = b""):
    def fake_popen(argv, **kwargs):
        captured["env"] = kwargs.get("env")
        captured["start_new_session"] = kwargs.get("start_new_session")
        captured["stderr"] = kwargs.get("stderr")
        r, w = os.pipe()
        os.write(w, payload)
        os.close(w)
        er, ew = os.pipe()
        if stderr:
            os.write(ew, stderr)
        os.close(ew)

        class P:
            pid = os.getpid()
            returncode = 0
            stdout = os.fdopen(r, "rb", buffering=0)
            stderr = os.fdopen(er, "rb", buffering=0)

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

        return P()

    return fake_popen


@pytest.mark.parametrize("provider", ["grok", "codex", "claude", "copilot"])
def test_run_worker_env_containment_all_providers(tmp_path, monkeypatch, provider):
    captured: dict = {}
    brief = tmp_path / "b.md"
    brief.write_text("do it")
    out = str(tmp_path / "out")
    payload = (json.dumps({
        "type": "result", "status": "done", "reason": "", "summary": "ok",
        "result": "ok", "usage": {"input_tokens": 1, "output_tokens": 1},
    }) + "\n").encode()
    monkeypatch.setattr(
        "ratchetloop.worker_run.subprocess.Popen",
        _pipe_popen(captured, payload),
    )
    run_worker("coder", provider, str(brief), str(tmp_path), out, timeout=5)
    env = captured["env"]
    assert captured["start_new_session"] is True
    assert env["GIT_CONFIG_COUNT"] == "3"
    assert env["GIT_CONFIG_KEY_0"] == "protocol.allow"
    assert env["GIT_CONFIG_VALUE_0"] == "never"
    assert env["GIT_CONFIG_KEY_1"] == "credential.helper"
    assert env["GIT_CONFIG_VALUE_1"] == ""
    assert env["GIT_CONFIG_KEY_2"] == "remote.origin.pushurl"
    assert env["GIT_CONFIG_VALUE_2"] == "DISABLED"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
    gh = Path(env["GH_CONFIG_DIR"])
    assert gh.is_dir()
    assert list(gh.iterdir()) == []


def test_has_done_marker_removed():
    assert not hasattr(workers, "_has_done_marker")
    assert "done_marker" not in PROVIDERS["grok"]


def test_usage_on_the_result_line_is_captured(tmp_path, monkeypatch):
    # claude reports tokens and cost only on its result line; predecessor read usage events only,
    # so 0 of its 3 claude steps recorded tokens (2026-09-11).
    captured: dict = {}
    payload = (json.dumps({
        "type": "result", "result": json.dumps({"status": "done", "reason": "", "summary": "ok"}),
        "usage": {"input_tokens": 12, "output_tokens": 3}, "total_cost_usd": 0.02,
        "modelUsage": {"claude-sonnet-5": {}},
    }) + "\n").encode()
    monkeypatch.setattr("ratchetloop.worker_run.subprocess.Popen", _pipe_popen(captured, payload))
    brief = tmp_path / "b.md"
    brief.write_text("do it")
    out = run_worker("coder", "claude", str(brief), str(tmp_path), str(tmp_path / "out"),
                     timeout=5)
    assert out.kind == "done"
    usage = out.usage
    assert (usage["tokens_in"], usage["tokens_out"], usage["cost_usd"]) == (12, 3, 0.02)
    assert usage["usage_source"] == "measured"
    assert usage["model_reported"] == "claude-sonnet-5"
