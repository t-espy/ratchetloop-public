"""One CLI-worker adapter: pinned argv per provider, structured events, typed outcomes, and one run
event when a launch starts and one when it ends (CONTRACT.md §3.1, §4). transport=cli; ACP is not
required (DECISIONS.md D5). Carried from predecessor without its lab reporting or database recording."""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

from .events import EventSink
from .signals import RunKilled, killed_why
from .worker_io import (
    RESULT_SCHEMA_JSON, REVIEWER_RESULT_SCHEMA_JSON, WorkerOutcome, atomic_write,
    claude_events, codex_events, copilot_events, grok_events, load_worker_outcome,
    outcome_payload, write_exit,
)
from .worker_proc import _kill_stale, worker_env
from .worker_run import outcome_from_log, run_cli

__all__ = ["PROVIDERS", "run_worker", "outcome_payload", "worker_env", "_kill_stale"]

# Verified against grok --help (1.0.13, re-checked 1.0.25): --json-schema implies --output-format
# json, --include-partial-messages only affects streaming-messages-json. Pin streaming + in-process
# schema check (no --json-schema).
GROK_DENY_RULES = ("Bash(git push*)", "Bash(gh *)")
GROK_SANDBOX = "workspace"
ROLES = ("coder", "reviewer")
# claude has no OS sandbox, so a coder with Bash could write anywhere; it gets file tools only.
# --disallowedTools is the boundary; --allowedTools only grants.
ROLE_TOOLS = {"coder": "Read,Glob,Grep,Write,Edit", "reviewer": "Read,Glob,Grep"}
ROLE_DENY = {
    "coder": "Bash,NotebookEdit,WebFetch,WebSearch,Agent,Task",
    "reviewer": "Bash,Write,Edit,NotebookEdit,WebFetch,WebSearch,Agent,Task",
}
# copilot 1.0.83 takes a prompt only as `-p <text>`, so argv carries this line and the brief stays
# in its file (D27). Its shell runs unsandboxed unless the experimental MXC sandbox is on
# (`copilot help sandbox`), so shell stays denied, as for claude. Its built-in GitHub MCP server
# would give the agent GitHub API tools around git containment, so built-in MCPs are disabled.
COPILOT_LAUNCHER = ("Read the file {brief} and follow it exactly. It is your complete task, "
                    "your rules, and the required format of your final message.")


def _role(role: str) -> str:
    if role not in ROLES:
        raise ValueError(f"unknown worker role {role!r}; roles are {list(ROLES)}")
    return role


def model_flags(provider: str, model: str | None, effort: str | None) -> list[str]:
    """Flags for a chosen model/effort. Verified against each CLI's --help
    2026-09-11: grok -m/--reasoning-effort, codex -m and -c
    model_reasoning_effort (TOML), claude and copilot --model/--effort."""
    flags: list[str] = []
    if model:
        flags += ["--model", model] if provider in ("claude", "copilot") else ["-m", model]
    if effort and provider == "grok":
        flags += ["--reasoning-effort", effort]
    elif effort and provider == "codex":
        flags += ["-c", f'model_reasoning_effort="{effort}"']
    elif effort and provider in ("claude", "copilot"):
        flags += ["--effort", effort]
    return flags


def grok_argv(brief: str, workdir: str, out_file: str, role: str = "coder", *,
              model: str | None = None, effort: str | None = None) -> list[str]:
    # grok --help / user-guide/18-sandbox.md: profiles off|workspace|read-only|strict.
    sandbox = "read-only" if _role(role) == "reviewer" else GROK_SANDBOX
    cmd = [
        "grok", "--prompt-file", brief,
        "--permission-mode", "bypassPermissions",
        "--cwd", workdir,
        "--sandbox", sandbox,
        "--output-format", "streaming-messages-json",
        "--include-partial-messages",
    ]
    for rule in GROK_DENY_RULES:
        cmd += ["--deny", rule]
    if role == "reviewer":
        cmd += ["--deny", "Edit"]  # user-guide/22-permissions-and-safety.md
    return cmd + model_flags("grok", model, effort)


def schema_path(out_file: str) -> Path:
    return Path(str(out_file) + ".schema.json")


def codex_argv(brief: str, workdir: str, out_file: str, role: str = "coder", *,
               model: str | None = None, effort: str | None = None) -> list[str]:
    # The brief goes in on STDIN (codex exec: "[PROMPT] ... instructions are read
    # from stdin"), never as an argv element: a 2.2 MB review brief raised
    # "Argument list too long" in predecessor (item 79 run 2). PROVIDERS["codex"]["stdin_brief"].
    reviewer = _role(role) == "reviewer"
    schema = schema_path(out_file)
    atomic_write(schema, (REVIEWER_RESULT_SCHEMA_JSON if reviewer else RESULT_SCHEMA_JSON) + "\n")
    # `codex exec --help`: -s read-only | workspace-write | danger-full-access. The shell policy is
    # pinned so a config.toml cannot strip worker_env's variables from the commands codex runs — its
    # reviewer's `dotnet test` is what left the shared build server (Donchian build, 2026-09-12).
    return [
        "codex", "exec", "-C", workdir, "-s", "read-only" if reviewer else "workspace-write",
        "-c", "shell_environment_policy.inherit=all",
        "--json", "--output-schema", str(schema), "-o",
        str(out_file) + ".last",
    ] + model_flags("codex", model, effort)


def claude_argv(brief: str, workdir: str, out_file: str, role: str = "coder", *,
                model: str | None = None, effort: str | None = None) -> list[str]:
    # claude 2.1.265: -p --output-format stream-json dies without --verbose
    # (help only: "Override verbose mode setting from config").
    # Brief on stdin (PROVIDERS["claude"]["stdin_brief"]): `claude --help`
    # takes a positional prompt OR --print with --input-format text (default)
    # from stdin. Putting the body on argv blew MAX_ARG_STRLEN (predecessor item 79 run 2).
    return [
        "claude", "-p", "--output-format", "stream-json", "--verbose",
        "--allowedTools", ROLE_TOOLS[_role(role)],
        "--disallowedTools", ROLE_DENY[role],
        "--permission-mode", "acceptEdits",
    ] + model_flags("claude", model, effort)


COPILOT_MIN_CREDITS = 30  # `copilot help limits`: the smallest --max-ai-credits it accepts


def copilot_inbox(out_file: str) -> Path:
    return Path(str(out_file) + ".in").resolve()


def copilot_argv(brief: str, workdir: str, out_file: str, role: str = "coder", *,
                 model: str | None = None, effort: str | None = None,
                 max_ai_credits: int | None = None) -> list[str]:
    write = "--allow-tool=write" if _role(role) == "coder" else "--deny-tool=write"
    # The brief is copied into a fresh directory of its own, the only path granted besides the
    # worktree: granting the brief's own directory could expose events.jsonl (review finding 3).
    inbox = copilot_inbox(out_file)
    shutil.rmtree(inbox, ignore_errors=True)
    inbox.mkdir(parents=True)
    copy = inbox / "brief.md"
    shutil.copyfile(brief, copy)
    cmd = [
        "copilot", "-p", COPILOT_LAUNCHER.format(brief=copy), "-C", workdir,
        "--output-format", "json", "--stream", "on",
        "--usage-output-file", str(out_file) + ".usage",
        "--add-dir", workdir, "--add-dir", str(inbox),
        "--no-ask-user", "--disable-builtin-mcps", "--deny-tool=shell", write,
    ]
    if max_ai_credits is not None:
        if (isinstance(max_ai_credits, bool) or not isinstance(max_ai_credits, int)
                or max_ai_credits < COPILOT_MIN_CREDITS):
            raise ValueError(f"max_ai_credits must be an int >= {COPILOT_MIN_CREDITS}")
        cmd += ["--max-ai-credits", str(max_ai_credits)]
    return cmd + model_flags("copilot", model, effort)


PROVIDERS: dict[str, dict[str, Any]] = {
    "grok": {"transport": "cli", "mode": "structured", "argv": grok_argv,
             "events": grok_events},
    "codex": {"transport": "cli", "mode": "structured", "argv": codex_argv,
              "events": codex_events, "stdin_brief": True},
    "claude": {"transport": "cli", "mode": "structured", "argv": claude_argv,
               "events": claude_events, "stdin_brief": True},
    "copilot": {"transport": "cli", "mode": "structured", "argv": copilot_argv,
                "events": copilot_events},
}


def _chosen(model_meta: dict[str, Any] | None) -> dict[str, Any]:
    meta = model_meta or {}
    return {k: meta.get(k) for k in ("model", "effort", "family")}


def _emit_end(sink: EventSink | None, role: str, attempt: int, provider: str,
              model_meta: dict[str, Any] | None, outcome: WorkerOutcome,
              *, reused: bool = False) -> None:
    if sink is None:
        return
    usage = outcome.usage or {}
    fields: dict[str, Any] = {
        "role": role, "round": attempt, "provider": provider, "kind": outcome.kind,
        "reason": outcome.reason, "exit": outcome.exit_code, "wall_s": usage.get("wall_s"),
        **_chosen(model_meta), "model_reported": usage.get("model_reported"),
        "tokens_in": usage.get("tokens_in"), "tokens_cached": usage.get("tokens_cached"),
        "tokens_out": usage.get("tokens_out"),
        "cost_usd": usage.get("cost_usd"), "ai_credits": usage.get("ai_credits"),
        "usage_source": usage.get("usage_source") or "unknown",
    }
    if role == "reviewer":
        fields["findings"] = (outcome.result or {}).get("findings")
    if reused:
        fields["reused"] = True
    sink.emit("worker_end", **fields)


def run_worker(
    role: str, provider: str, brief_path: str, workdir: str, out_file: str,
    *, timeout: float = 2400, attempt: int = 1, wall_budget_s: float = 7200,
    model_meta: dict[str, Any] | None = None, sink: EventSink | None = None,
    max_ai_credits: int | None = None,
) -> WorkerOutcome:
    """Launch one provider CLI and return its typed outcome. A launch that already finished (an
    `.exit` file, or a log holding a result) is reused, not re-run, after reaping any stale
    process group. With a sink, emits worker_start before the launch and worker_end after it."""
    spec = PROVIDERS.get(provider)
    if spec is None:
        raise SystemExit(f"unknown worker provider {provider!r}")
    if spec.get("transport") != "cli":
        raise SystemExit(f"unsupported transport {spec.get('transport')!r}")
    _role(role)
    if not _kill_stale(Path(str(out_file) + ".pid")):
        outcome = WorkerOutcome(kind="cleanup_failed", reason="cleanup_failed")
        _emit_end(sink, role, attempt, provider, model_meta, outcome)
        return outcome
    reused = load_worker_outcome(out_file)
    if reused is None:
        recovered = outcome_from_log(provider, out_file, spec["events"])
        if recovered is not None:
            write_exit(out_file, recovered)
            reused = recovered
    if reused is not None:
        if not reused.text:
            p = Path(out_file)
            reused.text = p.read_text() if p.is_file() else ""
        _emit_end(sink, role, attempt, provider, model_meta, reused, reused=True)
        return reused
    started_path = Path(str(out_file) + ".started")
    started_path.parent.mkdir(parents=True, exist_ok=True)
    started_path.write_text(str(time.time()))
    flags = {k: v for k, v in _chosen(model_meta).items() if k != "family" and v}
    if provider == "copilot" and max_ai_credits is not None:
        flags["max_ai_credits"] = max_ai_credits  # only copilot bills in AI credits
    argv = spec["argv"](brief_path, workdir, out_file, role, **flags)
    if sink is not None:
        sink.emit("worker_start", role=role, round=attempt, provider=provider,
                  **_chosen(model_meta), out_file=str(out_file))
    outcome = run_cli(
        provider, argv, workdir, out_file, timeout, spec["events"], wall_budget_s,
        stdin_path=brief_path if spec.get("stdin_brief") else None,
    )
    if killed_why():
        raise RunKilled(killed_why())
    # kind feeds model_choice's climb; model_choice records which rung was picked.
    outcome.usage = {**outcome.usage, "kind": outcome.usage.get("kind") or outcome.kind,
                     **({"model_choice": dict(model_meta)} if model_meta else {})}
    _emit_end(sink, role, attempt, provider, model_meta, outcome)
    return outcome
