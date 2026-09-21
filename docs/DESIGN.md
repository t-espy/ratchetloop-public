# ratchetloop — Design

A command-line program. A task file, or a paragraph describing what to build, goes in; a git
branch, a disposition, and a run directory come out. No database, no daemon, no workflow engine
library.

## Product

A person or program has a bounded change for a git repository — code, or the plan or design the
next code change will follow — and wants it made by an agent, checked by commands that exit 0 or
not, and judged by a second agent from a different model family.

```text
task → isolated coder → checks → scoped commit → independent review → result.json → human merge
```

The pipeline never merges, pushes, or deploys the target repository. A person does.

## Loop

1. Admit the task file, policy, providers, and repository.
2. Create a git worktree on `ratchetloop/<task_key>`.
3. A coding agent edits that worktree. Git remotes are disabled for the worker.
4. Scope: HEAD unmoved, `.git` untouched, every changed path inside `allowed_paths`.
5. Checks: the task's shell commands, explicit environment, all must exit 0. One fix launch on red.
6. Commit exactly the worker's paths.
7. A read-only reviewer from a different **model family** returns APPROVE or REQUEST_CHANGES. Two rounds at most.
8. Write `result.json` on every exit path.

`kind: doc` uses the same loop for a design or plan that is the next build contract.

`ratchetloop build IDEA.md` writes the design and plan, then one code stage per planned phase.

## Isolation

Every provider gets the same git containment (`protocol.allow=never`) and per-role sandbox flags.
After a coder step the pipeline checks the tree. After a review the worktree hash must be unchanged.
Verdicts come from the worker's final JSON object only.

## Out of scope

No queue, web UI, merge bot, or package index. No automatic re-planning after a failed phase.
The pipeline is not used to build itself.

See `CONTRACT.md` for the normative shapes, `FAILURE_MODES.md` for why the checks exist, and
`DECISIONS.md` for closed product rules.
