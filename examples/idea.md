---
idea_key: greet-cli
repo: /path/to/git/repo
base_branch: main
checks: ["python -m pytest -q"]
max_phases: 3
cost_budget_usd: 15
wall_budget_s: 7200
---
A tiny command-line greeter. `python -m greet NAME` prints `hello, NAME` and
exits 0. Missing or extra arguments exit 2 with a one-line usage string.
Python 3.12 stdlib only. Tests cover the happy path and the usage path.

Done looks like: `python -m greet ada` prints `hello, ada`, and `pytest -q`
is green.
