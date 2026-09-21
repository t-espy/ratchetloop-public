"""Worker briefs: the bundled role prompt, then the task brief, then the result contract the adapter
scores (`worker_io.result_fields`)."""
from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import Any

# The adapter scores a worker ONLY from its terminal result object; providers without a schema
# flag must be told so in the brief (predecessor item 79, 2026-09-08: a finished grok coder scored
# invalid_result because nothing had told it).
RESULT_CONTRACT = """\
## Result contract (the pipeline reads nothing else)

Your FINAL message must be exactly one JSON object and nothing else - no prose
before or after it, no code fence:

{"status": "done" | "blocked", "reason": "<one line>", "summary": "<what changed, file by file; the verbatim final line of the full test run; what you skipped and why>"}

Do not write SUMMARY.md or any other report file: the object is the report.
"""

# Reviewers are read-only. A brief telling a reviewer to write SUMMARY.md is what made a blocked
# reviewer look like a verdict in predecessor (item 90): the sandbox refused the write, the reviewer
# returned status=blocked with reason=APPROVE, and the run accepted.
REVIEWER_RESULT_CONTRACT = """\
## Result contract (the pipeline reads nothing else)

Your FINAL message must be exactly one JSON object and nothing else - no prose
before or after it, no code fence:

{"status": "done" | "blocked", "reason": "APPROVE" | "REQUEST_CHANGES", "summary": "<the whole review>", "findings": {"bug": 0, "suggestion": 0, "nit": 0}, "blocking": false}

Put the whole review in "summary" and the number of findings with each tag in "findings".
Set "blocking" to true only when a bug means the task's acceptance criteria are not met as
committed; otherwise false. Do not write SUMMARY.md or any other file.
"""

CARRIED_FOR_CODER = """\
## Residual findings carried from earlier phases
Fix any of these that the files this phase changes touch; leave the others as they are in
docs/REVIEW_NOTES.md.
"""
CARRIED_FOR_REVIEWER = """\
## Open residual findings from earlier phases
These are open. If this diff fixes one, say so on a line `resolved: <file:line>`; if this diff makes
one worse, re-raise it as a finding. A residual outside this phase's diff is
not grounds for REQUEST_CHANGES.
"""
FIX_CHECK = """\
For each round-1 finding write one line, `fixed: <n> — <what>` or `unfixed: <n> — <file:line> <why>`.
The verdict is REQUEST_CHANGES only when a line says `unfixed`. Anything new you notice goes under
one `notes:` line and never changes the verdict.
"""

# The verdict is a FIELD, never prose (predecessor item 79 run 4: a clean review with no VERDICT line
# was scored REQUEST_CHANGES).
REVIEWER_CONTRACT = """\
Reviewer: "reason" must be exactly APPROVE or REQUEST_CHANGES. That field IS the verdict; the
pipeline reads nothing else for it.
"""

PROMPTS = {("coder", "code"): "coder.md", ("reviewer", "code"): "reviewer.md",
           ("coder", "doc"): "doc-author.md", ("reviewer", "doc"): "doc-reviewer.md"}
MAX_DIFF_CHARS = 200_000


def role_prompt(role: str, kind: str = "code") -> str:
    name = PROMPTS.get((role, kind))
    if name is None:
        raise ValueError(f"no prompt for role {role!r} and kind {kind!r}; "
                         f"known: {sorted(PROMPTS)}")
    return files("ratchetloop").joinpath("prompts", name).read_text()


def assemble_brief(role: str, task_brief: str, dest: Path, *, kind: str = "code") -> None:
    parts = [role_prompt(role, kind), task_brief]
    if role == "reviewer":
        parts += [REVIEWER_RESULT_CONTRACT, REVIEWER_CONTRACT]
    else:
        parts.append(RESULT_CONTRACT)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n\n".join(parts))


def _objective(task: Any) -> str:
    return task.brief_file.read_text() if task.brief_file else task.objective


def _bullets(items: Any) -> str:
    return "\n".join(f"- {item}" for item in items)


def coder_task_brief(task: Any, tree: Path, *, round_: int, findings: str | None = None,
                     check_failures: str | None = None, previous: str | None = None) -> str:
    parts = [f"# Task {task.task_key}, round {round_}", f"WORKDIR: {tree}",
             "## Objective\n" + _objective(task),
             "## Acceptance criteria\n" + _bullets(task.acceptance_criteria)]
    if task.allowed_paths is not None:
        parts.append("## You may change only these paths\n" + _bullets(task.allowed_paths))
    if task.checks:
        parts.append("## Checks the pipeline runs after you finish\n"
                     + _bullets(f"`{c}`" for c in task.checks))
    if previous:
        parts.append("## Previous attempt\n" + previous)
    if getattr(task, "carried_findings", ()):
        parts.append(CARRIED_FOR_CODER + _bullets(task.carried_findings))
    if findings:
        parts.append("## Review findings to address\n" + findings)
    if check_failures:
        parts.append("## These checks failed on your change; make them pass\n" + check_failures)
    return "\n\n".join(parts)


def reviewer_task_brief(task: Any, tree: Path, *, round_: int, max_rounds: int, base_sha: str,
                        diff: str, checks: list[dict[str, Any]],
                        prior_findings: str | None = None) -> str:
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + (f"\n[diff truncated at {MAX_DIFF_CHARS} characters; "
                                        f"run `git diff {base_sha}..HEAD` for the rest]\n")
    parts = [f"# Review of task {task.task_key}, round {round_} of {max_rounds}",
             f"WORKDIR: {tree} (read-only; the change is committed on HEAD; base {base_sha})",
             "## Objective\n" + _objective(task),
             "## Acceptance criteria\n" + _bullets(task.acceptance_criteria),
             "## Checks the pipeline ran on this HEAD\n"
             + _bullets(f"`{r['cmd']}` exit {r['exit']}" for r in checks),
             "## Diff\n```diff\n" + diff + "\n```"]
    if getattr(task, "carried_findings", ()):
        parts.append(CARRIED_FOR_REVIEWER + _bullets(task.carried_findings))
    if prior_findings:
        parts.append("## Round-1 findings (fix-check: verify only that these were addressed)\n"
                     + prior_findings + "\n\n" + FIX_CHECK)
    return "\n\n".join(parts)
