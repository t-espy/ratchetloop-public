# ratchetloop

Ratchetloop is a local-first, auditable software-delivery pipeline for AI coding agents.

Give it a bounded software task or an idea. A coding agent works in an isolated Git
worktree, deterministic checks validate the change, an independent model family reviews
the diff, and Ratchetloop produces a branch plus an evidence-rich run record. It never
merges, pushes, or deploys the target repository.

```text
task → isolated coder → checks → scoped commit → independent review → result.json → human merge
```

Validated providers: **Grok**, **Codex**, and **Claude**.

[![CI](https://github.com/t-espy/ratchetloop-public/actions/workflows/ci.yml/badge.svg)](https://github.com/t-espy/ratchetloop-public/actions/workflows/ci.yml)

## Used in practice

Ratchetloop has been used to produce and independently review the implementations in
[leetcode-python](https://github.com/t-espy/leetcode-python), a public algorithm
interview work sample with deterministic oracle testing and CI, along with multiple ongoing private projects.

## Quick start

```bash
/usr/bin/python3 -m venv venv
venv/bin/pip install -e '.[dev]'
venv/bin/python -m pytest -q
venv/bin/ratchetloop run examples/task.yaml --check --policy WORKFLOW.md
```

`--check` validates the task file. A real run needs a git repository at `repo:` and
the named provider CLIs on `PATH`. After ACCEPT, a person merges `ratchetloop/<task_key>`.

```bash
venv/bin/ratchetloop run examples/task.yaml --policy WORKFLOW.md
venv/bin/ratchetloop status add-greet
venv/bin/ratchetloop stats --json
```

From an idea:

```bash
venv/bin/ratchetloop build examples/idea.md --policy WORKFLOW.md
```

## Pin (optional)

Callers can run a pinned runtime instead of a checkout venv:

```bash
deploy/pin.sh SHA
deploy/promote.sh SHA --approved-by NAME
deploy/ratchetloop run examples/task.yaml --policy WORKFLOW.md
```

`$RATCHETLOOP_OPT` (default `~/opt/ratchetloop`) is the pin root.

## Docs

| File | What it is |
| --- | --- |
| [docs/DESIGN.md](docs/DESIGN.md) | Product and isolation rules |
| [docs/CONTRACT.md](docs/CONTRACT.md) | Task file, run record, dispositions |
| [docs/CALLERS.md](docs/CALLERS.md) | How to invoke the CLI |
| [docs/FAILURE_MODES.md](docs/FAILURE_MODES.md) | Failures the design answers |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Closed product rules |
| [WORKFLOW.md](WORKFLOW.md) | Example model-ladder policy |

## Layout

```text
src/ratchetloop/   pipeline
tests/             fake-provider suite
deploy/            pin, promote, rollback, launcher
examples/          sample task and idea
docs/              design and contract
```

This tree is a curated public snapshot of a local pipeline. It does not claim to be the best possible implementation of anything. It fits my workflow, and has proven itself valuable. Fork it, flame it, extend it as you like.

## License

MIT. See [LICENSE](LICENSE).
