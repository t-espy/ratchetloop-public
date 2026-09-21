# ratchetloop — Decisions

Closed product rules. An open question is a gate only for the change that needs its answer.

| # | Decision | Resolution |
| --- | --- | --- |
| D2 | Who starts runs | The caller. No daemon, queue, or poller. |
| D3 | Execution record | Files, not a database. One run directory per run. |
| D5 | Worker transport | Headless CLIs with structured output. |
| D6 | Review independence | Reviewer is a different **model family** (not merely a different CLI), read-only, cannot promote. |
| D7 | Review rounds | Two per task at most. Round 2 is a fix-check of round-1 findings. |
| D8 | Where verdicts come from | The structured result's field only. No regex, marker, or override. |
| D9 | Merge and push | Never by the pipeline. A person merges. |
| D10 | Self-hosting | No. The pipeline is built directly, not by itself. |
| D12 | Source file size | 300-line target, 330 enforced by a test, 500 hard limit. |
| D14 | Red checks | One fix launch per run without spending a review. Still red → FAILED `checks_failed`. |
| D15 | Re-running a task key | `run` refuses an existing task branch; `--continue` picks it up. |
| D16 | Concurrency | Lock only around git mutations. Separate worktrees of one repo may run in parallel. |
| D18 | Run directories | `~/var/ratchetloop/runs`; `RATCHETLOOP_RUNS_ROOT` overrides. |
| D19 | Coder quota mid-run | The run ends `quota_exhausted`. No automatic coder switch. |
| D21 | Policy file | First fenced yaml block of `WORKFLOW.md`; `--policy`, else `RATCHETLOOP_POLICY`, else `~/.config/ratchetloop/WORKFLOW.md`. |
| D22 | Task validation | Grep-only checks are refused. `run --check` uses the same validator. |
| D23 | Plans and designs | A `doc` task kind through the same loop. |
| D24 | Usage and velocity | Captured in `result.json` from the first run. `stats` reads those files. |
| D28 | From an idea | `build idea.md`: one design-and-plan stage, then one code stage per phase. |
| D30 | Budget with unknown usage | `cost_budget_usd` counts measured spend; unknown-cost launches are bounded by `wall_budget_s`. |
