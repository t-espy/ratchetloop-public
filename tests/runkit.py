"""Shared pieces for loop tests: a target repository, a task file, and scripted fake workers that act
on the worktree and emit worker_end events the way the real adapter does."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Callable

import yaml

from ratchetloop import workers
from ratchetloop.worker_io import WorkerOutcome

CALC = "def add(a, b):\n    return a + b\n"
MUL = CALC + "\n\ndef mul(a, b):\n    return a * b\n"
WRONG_MUL = CALC + "\n\ndef mul(a, b):\n    return a * b + 1\n"
CHECK = "python3 -c 'import calc; assert calc.mul(3, 4) == 12'"


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target"
    repo.mkdir()
    for args in (["init", "-q", "-b", "master"], ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "calc.py").write_text(CALC)
    subprocess.run(["git", "add", "calc.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def write_task(tmp_path: Path, repo: Path, **fields) -> Path:
    data = {"task_key": "add-mul", "repo": str(repo), "objective": "Add mul(a, b) to calc.py.",
            "acceptance_criteria": ["calc.mul(3, 4) == 12"], "checks": [CHECK],
            "allowed_paths": ["calc.py"], **fields}
    data = {k: v for k, v in data.items() if v is not None}
    path = tmp_path / f"{data['task_key']}.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def runs_root() -> Path:
    return Path(os.environ["RATCHETLOOP_RUNS_ROOT"])


def result_of(task_key: str = "add-mul") -> dict:
    runs = sorted((runs_root() / task_key).iterdir())
    return json.loads((runs[-1] / "result.json").read_text())


def events_of(task_key: str = "add-mul") -> list[dict]:
    run = sorted((runs_root() / task_key).iterdir())[-1]
    return [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines()]


def done(summary: str = "ok", **usage) -> WorkerOutcome:
    return WorkerOutcome(kind="done", reason="done", summary=summary, exit_code=0,
                         result={"status": "done", "reason": "done", "summary": summary},
                         usage={"usage_source": "measured" if usage else "unknown", **usage})


def verdict(reason: str, summary: str = "reviewed", findings=None, blocking: bool | None = None,
            **usage) -> WorkerOutcome:
    findings = findings or {"bug": 0, "suggestion": 0, "nit": 0}
    result = {"status": "done", "reason": reason, "summary": summary, "findings": findings}
    if blocking is not None:
        result["blocking"] = blocking
    return WorkerOutcome(kind="done", reason=reason, summary=summary, exit_code=0,
                         result=result,
                         usage={"usage_source": "measured" if usage else "unknown", **usage})


def outcome(kind: str, reason: str = "") -> WorkerOutcome:
    return WorkerOutcome(kind=kind, reason=reason or kind, exit_code=0 if kind == "blocked" else 1)


def write(rel: str, text: str) -> Callable[[Path], None]:
    def act(tree: Path) -> None:
        (tree / rel).parent.mkdir(parents=True, exist_ok=True)
        (tree / rel).write_text(text)
    return act


class FakeWorkers:
    """Scripted turns per role: each turn is (action(tree) or None, WorkerOutcome)."""

    def __init__(self, coder: list, reviewer: list):
        self.turns = {"coder": list(coder), "reviewer": list(reviewer)}
        self.calls: list[dict] = []

    def __call__(self, role, provider, brief_path, workdir, out_file, **kw):
        self.calls.append({"role": role, "provider": provider, "attempt": kw.get("attempt"),
                           "brief": Path(brief_path).read_text()})
        if not self.turns[role]:
            raise AssertionError(f"unexpected {role} launch")
        action, out = self.turns[role].pop(0)
        if action is not None:
            action(Path(workdir))
        sink = kw.get("sink")
        if sink is not None:
            usage = out.usage or {}
            sink.emit("worker_end", role=role, round=kw.get("attempt"), provider=provider,
                      kind=out.kind, reason=out.reason, model=None, effort=None, family=None,
                      model_reported=usage.get("model_reported"),
                      tokens_in=usage.get("tokens_in"), tokens_cached=None,
                      tokens_out=usage.get("tokens_out"), cost_usd=usage.get("cost_usd"),
                      ai_credits=None, usage_source=usage.get("usage_source", "unknown"))
        return out

    def roles(self) -> list[str]:
        return [c["role"] for c in self.calls]


def install(monkeypatch, coder: list, reviewer: list, *, clis: bool = True) -> FakeWorkers:
    fake = FakeWorkers(coder, reviewer)
    monkeypatch.setattr(workers, "run_worker", fake)
    if clis:
        monkeypatch.setattr("ratchetloop.run.shutil.which", lambda name: f"/usr/bin/{name}")
    return fake


def primary_state(repo: Path) -> tuple:
    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True).stdout
    return (git("rev-parse", "HEAD"), git("rev-parse", "--abbrev-ref", "HEAD"),
            git("status", "--porcelain=v1", "-uall"), git("diff"))
