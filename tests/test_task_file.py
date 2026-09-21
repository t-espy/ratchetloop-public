"""Task files are refused before any agent runs unless every key is one the pipeline can honour."""
from pathlib import Path

import pytest

from ratchetloop.task import TaskError, grep_only_checks, load_task, validate_task

BASE = {
    "task_key": "add-mul", "repo": "/tmp/repo", "objective": "Add mul(a, b).",
    "acceptance_criteria": ["mul exists"], "checks": ["venv/bin/python -m pytest -q"],
}


def test_minimal_code_task_gets_defaults():
    task = validate_task(BASE)
    assert (task.kind, task.base_branch, task.checks_cwd, task.max_review_rounds) == (
        "code", "master", ".", 2)
    assert task.repo == Path("/tmp/repo")
    assert (task.wall_budget_s, task.cost_budget_usd) == (7200.0, 25.0)
    assert task.allowed_paths is None and task.setup == ()


def test_doc_task_needs_allowed_paths_not_checks():
    raw = {**BASE, "kind": "doc", "allowed_paths": ["docs/"]}
    del raw["checks"]
    task = validate_task(raw)
    assert task.checks == () and task.allowed_paths == ("docs/",)
    del raw["allowed_paths"]
    with pytest.raises(TaskError, match="allowed_paths"):
        validate_task(raw)


@pytest.mark.parametrize("change,match", [
    ({"foundational": True}, "unknown keys"),  # predecessor keys are not ratchetloop keys
    ({"lab_item": 49}, "unknown keys"),
    ({"task_key": "Add_Mul"}, "task_key"),
    ({"task_key": "-x"}, "task_key"),
    ({"task_key": "a" * 64}, "task_key"),
    ({"kind": "plan"}, "kind"),
    ({"repo": ""}, "repo"),
    ({"repo": "--help"}, "looks like a flag"),
    ({"brief_file": "b.md"}, "exactly one"),
    ({"acceptance_criteria": []}, "must not be empty"),
    ({"acceptance_criteria": ["ok", ""]}, "non-empty strings"),
    ({"checks": []}, "must not be empty"),
    ({"checks": ["grep -q def\\ mul src/calc/__init__.py"]}, "grep source"),
    ({"allowed_paths": ["/etc"]}, "inside the repository"),
    ({"allowed_paths": ["src/../../x"]}, "inside the repository"),
    ({"base_branch": "--help"}, "looks like a flag"),
    ({"checks_cwd": "../other"}, "inside the repository"),
    ({"coder": "gemini"}, "coder"),
    ({"coder": "grok", "reviewer": "grok"}, "model family"),
    ({"tier": "cheap"}, "tier"),
    ({"max_review_rounds": 3}, "1 or 2"),
    ({"max_review_rounds": True}, "1 or 2"),
    ({"wall_budget_s": -1}, "non-negative"),
    ({"cost_budget_usd": "cheap"}, "non-negative"),
])
def test_bad_task_is_refused(change, match):
    with pytest.raises(TaskError, match=match):
        validate_task({**BASE, **change})


def test_missing_task_key_and_non_mapping_are_refused():
    raw = dict(BASE)
    del raw["task_key"]
    with pytest.raises(TaskError, match="task_key"):
        validate_task(raw)
    with pytest.raises(TaskError, match="mapping"):
        validate_task(["not", "a", "mapping"])


def test_env_reaches_the_task_as_strings_and_bad_entries_are_refused():
    task = validate_task({**BASE, "env": {"DB_DSN": "dbname=test", "WORKERS": 2}})
    assert task.env == {"DB_DSN": "dbname=test", "WORKERS": "2"}
    assert validate_task(BASE).env == {}
    for bad, match in (({"1BAD": "x"}, "valid variable name"), ({"OK": ["x"]}, "string or a number"),
                       ({"OK": True}, "string or a number"), (["A=1"], "mapping")):
        with pytest.raises(TaskError, match=match):
            validate_task({**BASE, "env": bad})


def test_objective_prose_may_start_with_a_dash():
    # Phase 1 review nit 9: the flag guard is for paths and refs, not for prose.
    assert validate_task({**BASE, "objective": "- [ ] add mul"}).objective == "- [ ] add mul"


def test_copilot_may_review_copilot_because_family_is_checked_when_chosen():
    task = validate_task({**BASE, "coder": "copilot", "reviewer": "copilot"})
    assert (task.coder, task.reviewer) == ("copilot", "copilot")


def test_grep_only_detection_ignores_behavioural_commands():
    assert grep_only_checks(["python -m pytest -q", "cd x && grep -q ok out.txt"]) == []
    assert grep_only_checks(["LC_ALL=C grep -n foo src/a.py"]) == ["LC_ALL=C grep -n foo src/a.py"]


def test_load_task_resolves_relative_paths_against_the_task_file(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks" / "brief.md").write_text("do it")
    path = tmp_path / "tasks" / "t.yaml"
    path.write_text("task_key: t\nrepo: ../repo\nbrief_file: brief.md\n"
                    "acceptance_criteria: [ok]\nchecks: [make test]\n")
    task = load_task(path)
    assert task.repo == repo.resolve()
    assert task.brief_file == (tmp_path / "tasks" / "brief.md").resolve()


def test_load_task_refuses_missing_brief_non_git_repo_and_bad_yaml(tmp_path):
    (tmp_path / "repo").mkdir()
    path = tmp_path / "t.yaml"
    path.write_text("task_key: t\nrepo: repo\nobjective: x\nacceptance_criteria: [ok]\n"
                    "checks: [make test]\n")
    with pytest.raises(TaskError, match="not a git repository"):
        load_task(path)
    (tmp_path / "repo" / ".git").mkdir()
    path.write_text(path.read_text().replace("objective: x", "brief_file: nope.md"))
    with pytest.raises(TaskError, match="not found"):
        load_task(path)
    path.write_text("task_key: [\n")
    with pytest.raises(TaskError, match="invalid YAML"):
        load_task(path)
    with pytest.raises(TaskError, match="cannot read"):
        load_task(tmp_path / "missing.yaml")
