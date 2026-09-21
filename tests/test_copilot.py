"""copilot launches: the brief sits alone in a fresh directory, and an AI-credit budget is passed
only when the caller sets one (Phase 1 review, findings 2 and 3)."""
import sys
from pathlib import Path

import pytest

from ratchetloop.workers import PROVIDERS, copilot_argv, copilot_inbox, run_worker

FAKE = Path(__file__).resolve().parent / "fake_struct_worker.py"


def _setup(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "events.jsonl").write_text('{"event":"admit"}\n')
    brief = run / "brief.coder.1.md"
    brief.write_text("do the thing")
    tree = tmp_path / "tree"
    tree.mkdir()
    return str(brief), str(tree), str(run / "coder.1.log"), run


def test_the_run_directory_is_never_granted(tmp_path):
    brief, tree, out, run = _setup(tmp_path)
    argv = copilot_argv(brief, tree, out)
    inbox = copilot_inbox(out)
    dirs = [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
    assert dirs == [tree, str(inbox)]
    assert str(run.resolve()) not in dirs
    assert sorted(p.name for p in inbox.iterdir()) == ["brief.md"]
    assert (inbox / "brief.md").read_text() == "do the thing"


def test_each_launch_gets_a_fresh_inbox(tmp_path):
    brief, tree, out, _ = _setup(tmp_path)
    copilot_argv(brief, tree, out)
    (copilot_inbox(out) / "left-behind.txt").write_text("x")
    copilot_argv(brief, tree, out, role="reviewer")
    assert sorted(p.name for p in copilot_inbox(out).iterdir()) == ["brief.md"]


def test_credit_budget_is_passed_only_when_set_and_never_below_30(tmp_path):
    brief, tree, out, _ = _setup(tmp_path)
    assert "--max-ai-credits" not in copilot_argv(brief, tree, out)
    argv = copilot_argv(brief, tree, out, max_ai_credits=40)
    assert argv[argv.index("--max-ai-credits") + 1] == "40"
    for bad in (29, True, 40.5):
        with pytest.raises(ValueError, match="30"):
            copilot_argv(brief, tree, out, max_ai_credits=bad)


def test_run_worker_passes_credits_to_copilot_only(tmp_path, monkeypatch):
    seen: dict = {}
    for provider in ("copilot", "grok"):
        def argv(brief, workdir, out_file, role="coder", _p=provider, **kw):
            seen[_p] = kw
            return [sys.executable, str(FAKE)]
        monkeypatch.setitem(PROVIDERS, provider, {**PROVIDERS[provider], "argv": argv})
    brief = tmp_path / "b.md"
    brief.write_text("x")
    for provider in ("copilot", "grok"):
        out = run_worker("coder", provider, str(brief), str(tmp_path),
                         str(tmp_path / f"{provider}.log"), timeout=10, max_ai_credits=50)
        assert out.kind == "done"
    assert seen == {"copilot": {"max_ai_credits": 50}, "grok": {}}
