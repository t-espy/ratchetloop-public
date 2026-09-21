# ratchetloop — Contract

**Status:** Current public contract (snapshot 2026-09-21)
**Scope:** the normative interface — what a caller writes, what the pipeline writes, and what each
worker must return. Anything not here is internal and may change.

## 1. Command line

```text
ratchetloop run TASK.yaml [--coder P] [--reviewer P] [--policy FILE] [--check] [--continue] [--detach]
ratchetloop trees [--discard PATH --yes]
ratchetloop review TASK.yaml --branch B [--reviewer P] [--base SHA] [--policy FILE]
ratchetloop status TASK_KEY [--run RUN_ID]
ratchetloop stats [--since DATE] [--repo PATH] [--json]
ratchetloop build IDEA.md [--gate plan] [--continue] [--policy FILE] [--coder P] [--reviewer P] [--detach]
ratchetloop approve IDEA_KEY
```

- `run` executes one task to a disposition. `--continue` picks the task up after a run that
  ended short of ACCEPT or died (§1.1, D15). `--detach` goes through admit in the foreground (a refusal
  still exits 2), then re-executes the same command in a new session, so the run outlives the caller;
  it logs to `<runs_root>/.detached/` and prints the child's pid, its log and, once it exists, its run
  directory; a child that ends before its run begins returns its exit code, with its log's tail on
  stderr. `build --detach` goes through `build`'s own refusals first, likewise. A new session survives
  a closed terminal, not a cgroup kill: from a systemd unit use `systemd-run --user --scope`. `--policy` names the policy file (§7). `--check` runs admit
  — task, policy, model families, provider CLIs, repository — and exits 0 without a run (D22).
- `trees` lists preserved worktrees; `--discard PATH --yes` force-removes one that carries the
  preserve marker, the only forced removal outside an ACCEPT.
- `review` runs one review round on an existing branch against a moved base without a rebase.
  The base defaults to the
  branch's merge-base with the task's `base_branch` and must be an ancestor of the branch. The head is
  checked out detached — no branch is locked, nothing is committed — `setup` and the task's checks run
  there first (red: FAILED `checks_failed`, no review), then a reviewer that shares no model family
  with any coder recorded for the task reads base..HEAD: APPROVE ends ACCEPT, REQUEST_CHANGES ends
  HUMAN_REVIEW. Setup and the checks must leave the head where it was (else FAILED
  `scope_violation`, no review). The record, `kind: review`, is under
  `<runs_root>/<task_key>.review/`, apart from the task's own runs.
- `status` prints `result.json` for the latest (or `--run`) run of a task or idea key, or, for a run
  without one, its last event and whether its process lives; exit 2 when the key has no run.
- `stats` aggregates usage and velocity from `result.json` files. It only reads. Runs, acceptances
  and velocity count task runs (`code`, `doc`); spend — tokens, cost, launches, with
  `launches_without_cost` for launches whose provider reports no cost and
  `launches_with_unknown_usage` for those with no usage — also counts review records; an idea is
  reported beside them, never added, because its usage is its stages'. Breakdowns: per provider and model family (by launch), per kind and repository (by
  run); cost and tokens per ACCEPTed task, findings per review round, tests per 100 lines added.
- `build` takes an idea from design and plan through every code phase (§9); `approve` releases an
  idea paused at `--gate plan`.
- Any argument beginning with `-` in a positional slot is refused (`FAILURE_MODES.md` §9).

### 1.1 `--continue`

Admit refuses it when the task branch does not exist, when the task's latest run that recorded a
base commit ended ACCEPT (a
further change is a new `task_key`), or when the task's worktree exists but is not this task's (its
identity names another task, branch or base commit; it is not registered; another branch is checked
out). Otherwise the run:

- starts from the base commit the latest run recorded, not today's `base_branch`;
- reuses the worktree as it was left, without re-running `setup`, or, when there is none, makes one
  on the branch and runs `setup` there;
- adopts uncommitted changes a dead or stopped run left: everything dirty except the pipeline's own
  leftovers, unchanged since `.ratchetloop/residue.json` recorded them with their content, is the
  round-1 change, scoped and committed with it, and the coder is told so. When that run ended after
  its scope check (killed during the checks, or stopped before them), only the paths the check named
  are adopted; anything else is output of the interrupted checks;
- gives the coder the branch's commits and the latest review's findings when it asked for changes;
- takes any coder (`--coder`), but no reviewer may share a model family with any coder of the task,
  earlier runs included (D6).

A coder that then changes nothing is `no_change` only if the branch holds no commit; otherwise the
run goes on to the checks and a review.

## 2. Task file

YAML, one task per file. Unknown keys are refused.

| Key | Required | Meaning |
| --- | --- | --- |
| `task_key` | yes | `[a-z0-9][a-z0-9-]{0,62}`; names the branch `ratchetloop/<task_key>` and the run folder |
| `kind` | no | `code` (default) or `doc` — a plan or design that is the next build contract (`DESIGN.md` §1.1) |
| `repo` | yes | Path to the target repository; no default |
| `objective` | one of | The change, in prose. The coder's brief is built from it |
| `brief_file` | one of | A markdown brief, instead of `objective` |
| `acceptance_criteria` | yes | Non-empty list of strings; shown to coder and reviewer |
| `checks` | `code`: yes | Non-empty list of shell commands; all must exit 0. `doc` tasks may omit it; the built-in doc checks (§2.1) always run |
| `base_branch` | no | Default `master` |
| `checks_cwd` | no | Relative to the worktree; default the worktree root |
| `setup` | no | Commands run in the worktree before the coder (e.g. link a venv); D20 |
| `allowed_paths` | `doc`: yes | Path prefixes the change may touch; anything else fails the run |
| `coder` / `reviewer` | no | Provider override: `grok`, `codex`, `claude`, `copilot`. The reviewer's model family must differ from the coder's (§7) |
| `tier` | no | `light`, `standard`, `heavy` — the model-ladder starting rung |
| `max_review_rounds` | no | 1 or 2; default 2 |
| `carried_findings` | no | Open residual findings from earlier stages of an idea, one line each (§9.5); the coder is told to fix those its phase touches, the reviewer that they are open but not grounds for REQUEST_CHANGES outside its diff |
| `env` | no | Variables for `setup` and `checks`, `NAME: value`. Otherwise they see only `HOME`, `USER`, `LOGNAME`, `LANG`, `LC_*`, `TERM`, `TMPDIR`, `SHELL`, `TZ`, and a `PATH` without ratchetloop's own virtualenv. Workers and checks also run with no Python bytecode (`PYTHONDONTWRITEBYTECODE=1`) and no shared .NET build servers (`DOTNET_CLI_USE_MSBUILD_SERVER=0`, `MSBUILDDISABLENODEREUSE=1`, `UseSharedCompilation=false`; codex launches pin `shell_environment_policy.inherit=all` so its commands keep them); `env` may override them for `setup` and `checks`, never for workers |
| `wall_budget_s` | no | Default 7200 |
| `cost_budget_usd` | no | Default 25. Measured spend only: a launch whose provider reports no cost (codex) is bounded by `wall_budget_s` alone (D30) |

Copilot integration is implemented but not included among the validated providers in this public snapshot.

Admit refuses: a `code` task with an empty `checks` list; a `doc` task without `allowed_paths`; a
check that only greps source (D22); a `repo` that is not a git
repository or has no `base_branch`; an existing task branch without `--continue`; a missing provider
CLI; a run of the same task that is still alive (its `lock`, §3). A dead one is marked abandoned first.

### 2.1 Built-in doc checks

For `kind: doc`, before any listed checks: every changed file is inside `allowed_paths`; at least one
file changed; every relative markdown link in a changed file resolves in the worktree (D25). These make
a doc run fail on a missing or misplaced document, not on its content — content is the reviewer's job.

## 3. Run directory

```text
<runs_root>/<task_key>/<run_id>/        run_id = UTC 20260911T201500Z
  task.yaml              copy as admitted
  lock                   while running: pid, the kernel's start time for it, unix time (D33)
  events.jsonl           append-only, one event per line
  brief.coder.1.md       exact text sent to the worker
  coder.1.log            worker structured stream (stdout)
  coder.1.err            worker stderr, never parsed for events
  checks.1.log
  setup.pid, checks.pid  process group of the setup command or check while it runs
  diff.1.patch           base..HEAD after commit
  brief.reviewer.1.md
  reviewer.1.log / .err
  result.json            written on every exit path
```

`<runs_root>` defaults to `~/var/ratchetloop/runs` (D18). Worktrees live under
`~/var/ratchetloop/worktrees/<repo digest>/<task_key>` (`RATCHETLOOP_WORKTREE_ROOT` overrides). Nothing
is written inside a worktree except the coder's change, `setup` outputs (never staged), and
`.ratchetloop/` (identity, preserve marker, and `residue.json` — the untracked paths the pipeline
itself made, read by `--continue` — excluded from git).

Admit takes an exclusive flock on `<runs_root>/.locks/<task_key>.lock` and the run holds it to the
end, so two runs of one task never both get past admit; the kernel drops it when its holder dies. A
later run of the task that finds a `lock` whose process is dead marks that run abandoned: it reaps
the process groups recorded in the run directory's `*.pid` files — skipping launches that wrote their
`.exit` file, and groups with no member working inside the task's worktree — appends a FAILED
`abandoned` disposition naming the last event on disk, and writes its `result.json`. A run that had
already written its `result.json` keeps it; only its `lock` is removed.

### 3.1 Events

Each line is one JSON object with at least `t` (ISO-8601 UTC), `run_id`, and `event`. The line is
flushed and fsynced before the step it describes hands control on.

| `event` | Extra fields |
| --- | --- |
| `admit` | `task_key`, `repo`, `base_sha`, `branch`, `coder`, `coder_family`, `reviewer`, `continued_from` (the run `--continue` picked up, else null) |
| `worktree` | `path`, `reused` |
| `worker_start` | `role`, `round`, `provider`, `model`, `effort`, `family`, `out_file` (its process group is in `<out_file>.pid`) |
| `worker_end` | `role`, `round`, `kind` (§4), `reason`, `exit`, `wall_s`, `model`, `effort`, `family` (as chosen), `model_reported` (as the stream reports it), `findings` (reviewer), `reused`, `tokens_in` (all input processed, cached included), `tokens_cached`, `tokens_out`, `cost_usd`, `ai_credits`, `usage_source` |
| `scope` | `changed`, `outside_allowed`, `head_moved`, `git_dir_touched` |
| `checks` | `round`, `head_sha`, `passed`, `wall_s`, per-command `exit` |
| `setup` | `passed`, `wall_s`, per-command `exit`; `skipped` when a reused worktree already had it |
| `commit` | `round`, `sha`, `paths`, `lines_added`, `lines_removed` |
| `review` | `round`, `reviewer`, `verdict` (null when the review is rejected: tree changed, blocked, coder's family, no valid verdict), `findings` (§4), `summary` (the review; `--continue` hands the latest REQUEST_CHANGES one to the coder), `head_sha`, `tree_unchanged` |
| `worktree_end` | `path`, `disposition` (`removed` or `preserved`), `reason` |
| `disposition` | `disposition`, `reason`, `detail` (for `abandoned`, appended by the later run) |
| `error` | `where`, `message` |

## 4. Worker results

A worker's final message must be exactly one JSON object. Both contracts are appended to every brief;
providers with a schema flag also get it as a schema.

```json
{"status": "done" | "blocked", "reason": "<one line>", "summary": "<what changed>"}
```

Reviewer:

```json
{"status": "done" | "blocked", "reason": "APPROVE" | "REQUEST_CHANGES", "summary": "<findings>",
 "findings": {"bug": 0, "suggestion": 0, "nit": 0}}
```

`findings` counts are velocity data, never control flow: the verdict is `reason` alone, and missing
or malformed counts are recorded as `null` rather than invalidating the review.

`usage_source` on every launch is `measured` (the CLI reported the numbers), `estimated`, or `unknown`
(the CLI reported nothing usable). Unknown values stay `null`; they are never recorded as zero. copilot bills in
AI credits, not dollars: its launches fill `ai_credits` and leave `cost_usd` `null` unless a price
table converts them. Its per-launch budget is `--max-ai-credits`, passed when the caller sets one (at least 30).

The adapter maps every launch to one `kind`:

| `kind` | When |
| --- | --- |
| `done` | Valid object, `status: done`, exit 0 |
| `blocked` | Valid object, `status: blocked` — for a reviewer, never a verdict |
| `invalid_result` | No parseable final object, or wrong fields |
| `error` | Non-zero exit, even with a valid object |
| `quota` | A provider rate-limit signature in the structured stream (never stderr) |
| `silence` | No event for the silence limit; process group killed and confirmed dead |
| `timeout` | Wall budget exceeded; process group killed and confirmed dead |
| `cleanup_failed` | The process group would not die — after one of the kills above, or after a clean exit, when whatever the worker left in its group (a build server, a test daemon) is reaped |

Briefs reach the worker by stdin or file, never argv. copilot (1.0.83) accepts a prompt only as
`-p <text>`, so its argv carries one fixed launcher line naming the brief file; a copy of the brief
sits alone in a fresh `<out_file>.in/` directory, the one path granted with `--add-dir`
besides the worktree, so the run record is never exposed (D27).

## 5. `result.json`

```json
{
  "task_key": "add-mul", "run_id": "20260911T201500Z", "kind": "code", "attempt": 1,
  "disposition": "ACCEPT", "reason": "approved",
  "repo": "/path/to/repo",
  "branch": "ratchetloop/add-mul", "base_sha": "…", "head_sha": "…",
  "coder": {"provider": "grok", "model": "grok-4.6", "effort": "medium"},
  "reviewer": {"provider": "codex", "model": "gpt-5.6-terra", "effort": "medium"},
  "checks": {"passed": true, "head_sha": "…"},
  "review_summary": "<last review summary>",
  "worktree": null,
  "usage": {
    "launches": [
      {"role": "coder", "round": 1, "provider": "grok", "model": "grok-4.6", "effort": "medium",
       "wall_s": 312, "tokens_in": 101930, "tokens_out": 22356, "cost_usd": 0.15,
       "usage_source": "measured"}
    ],
    "tokens_in": 101930, "tokens_out": 22356, "cost_usd": 0.15, "launches_with_unknown_usage": 0
  },
  "velocity": {
    "lines_added": 9, "lines_removed": 0, "files_changed": 2,
    "tests_added": 1, "tests_added_method": "heuristic",
    "review_rounds": 1, "findings": {"bug": 0, "suggestion": 1, "nit": 0},
    "checks_wall_s": 4, "wall_s": 540
  },
  "started": "…", "ended": "…"
}
```

`worktree` is non-null only when the worktree was preserved (dirty, or disposition not ACCEPT).

### 5.1 How velocity is computed

- `attempt` = 1 + the number of earlier run directories for the same `task_key`.
- `lines_added`, `lines_removed`, `files_changed` = `git diff --numstat base_sha..head_sha`.
- `tests_added` = added lines matching a per-language test pattern (Python `def test_`, JS/TS
  `it(` / `test(`, C# `[Fact]` / `[Theory]` / `[Test]` / `[TestMethod]` — one per test method, not per
  data row), a line-matching heuristic; `tests_added_method` says so. `null` for
  `doc` tasks and unknown languages (D26).
- `findings` = the last review's counts; per-round counts are in `events.jsonl`.
- Usage totals sum only measured and estimated launches; `launches_with_unknown_usage` says how much
  is missing.

## 6. Dispositions and exit codes

| Disposition | Exit | Reasons |
| --- | --- | --- |
| ACCEPT | 0 | `approved` — checks green at the reviewed HEAD and the reviewer's verdict is APPROVE |
| HUMAN_REVIEW | 3 | `review_rounds_exhausted`, `coder_blocked` |
| FAILED | 4 | `checks_failed`, `scope_violation`, `reviewer_wrote`, `reviewer_blocked`, `invalid_result`, `worker_error`, `quota_exhausted`, `budget_wall`, `budget_cost`, `setup_failed`, `no_change`, `crash`, `abandoned` |
| STOPPED | 5 | `stop_sentinel`, `killed` |
| (refused at admit) | 2 | bad task file, bad arguments, bad policy, missing CLI; no run directory |

Exit 1 is left to the interpreter for an uncaught crash; the pipeline still attempts `result.json`
with `reason: crash`. No other path exits 1.

`abandoned` is never a live run's own exit: a later run of the task writes it for a run whose
pipeline died (§3). `killed` is a SIGTERM or SIGINT to the pipeline (§8).

ACCEPT requires the checks event and the review event to name the same `head_sha`. For a `doc` task,
ACCEPT means reviewed, not proven.

`review_rounds_exhausted` with green checks ends a stand-alone task run as HUMAN_REVIEW; inside an
idea build it is carried (§9.5) unless the idea says `stop` or the reviewer flagged the last finding
`blocking`. The reviewer's result carries `blocking` (boolean, nullable; §4): true only when a bug
leaves the task's acceptance criteria unmet as committed.

## 7. Policy file

The first fenced `yaml` block of a
`WORKFLOW.md`, located by `--policy` or `ratchetloop`'s config directory (D21). Keys: `ladders` per
provider, `tier_start`, `default_tier` per role, `review_light_max_lines`, `review_heavy_min_lines`.
Each rung adds a `family` (`anthropic`, `openai`, `google`, `microsoft`, `moonshot`, `xai`); grok,
codex and claude rungs default to their maker, copilot rungs must state it and name a model from
`copilot help config`. Reviewer selection skips any rung whose family matches the coder's. Copilot's
`--model auto` is refused, because its family cannot be known.

The family check is repeated on the model the worker's stream reports it actually used. A review whose
actual family matches the coder's is `invalid_result`, whatever was requested, and so is a copilot review
that reports no model. That covers copilot's `continueOnAutoMode` (switch models on a rate limit):
nothing reads the setting; a switch to the coder's family, or one that leaves no model reported, fails the
review.
A malformed block refuses the run. Each earlier run of the same task in which the same role failed (its
worker ended blocked, invalid or with a protocol error, or, for the coder, the checks failed)
climbs one rung, counted from those runs' `events.jsonl`; quota failures do not climb.

## 8. Stop

`<runs_root>/STOP` halts every run at the next step boundary — before each launch, before the
checks, before the commit; `<run dir>/STOP` halts that run. Neither lives inside a worktree. The run
ends STOPPED `stop_sentinel` with its worktree preserved; `--continue`, once the sentinel is gone,
adopts what it left.

SIGTERM or SIGINT to `ratchetloop run` reaps the running launch's or check's process group and ends
the run STOPPED `killed`, `result.json` written. SIGKILL cannot be caught: the next run of the task
marks the run abandoned and reaps what it left running (§3).

## 9. Idea mode

### 9.1 Idea file

Markdown with YAML front matter. Unknown keys are refused.

```markdown
---
idea_key: calc-cli                          # as task_key, but may not contain "--"
repo: /path/to/calc-cli                     # must be a git repo with at least one commit
base_branch: master
checks: ["venv/bin/python -m pytest -q"]    # optional; run after every code phase
setup: ["ln -s /path/to/venv venv"]         # optional; as in task files
max_phases: 6                               # default 6
cost_budget_usd: 50                         # whole idea; measured spend only (D30)
wall_budget_s: 21600                        # whole idea
on_rounds_exhausted: carry                  # or stop; §9.5, default carry
---
One or more paragraphs: what should be built, for whom, and what done looks like.
```

### 9.2 Stages

1. **Plan** — a `doc` task, `task_key` `<idea_key>--plan`, `allowed_paths: [docs/]`, on its own branch
   `ratchetloop/<idea_key>--plan` from `base_branch`. The author writes `docs/DESIGN.md` and
   `docs/PLAN.md`. `PLAN.md` must contain exactly one fenced `yaml` block:

   ```yaml
   phases:
     - key: bootstrap                  # [a-z0-9-]+, unique
       objective: "…"
       acceptance_criteria: ["…"]
       checks: ["…"]                   # this phase's own proof
       allowed_paths: ["src/", "tests/"]   # optional
   ```

   The plan stage's check — `python -m ratchetloop.idea check-plan docs/PLAN.md --idea …`, the same
   code the builder uses — adds, to §2.1: `docs/DESIGN.md` exists; the block parses; it has 1 to
   `max_phases` phases with unique keys (not `plan`), each with its own `checks`; each phase, merged
   with the idea's `repo`, `setup` and `checks`, passes the task-file validator (`run --check`). A plan
   the pipeline cannot execute fails its checks, not its review.
2. **Gate** — only with `--gate plan`: after the plan is ACCEPTed the idea stops PAUSED. A person may
   edit `docs/PLAN.md` on the plan stage's branch; `approve IDEA_KEY` records the approval
   (`approved.json`) in the idea's latest run directory; `build --continue` proceeds and re-reads and
   re-validates the phases at that branch's head. A gate once asked for holds on every later build
   of the idea, with or without `--gate`, until it is approved.
3. **Phases** — one `code` task per phase, in order, `task_key` `<idea_key>--<phase key>`, each on its
   own branch `ratchetloop/<task_key>` started from the previous stage's branch (D34). The coder sees
   the phase's objective and criteria plus `docs/DESIGN.md` and `docs/PLAN.md` (D31); the reviewer sees
   that phase's own commits (its base..HEAD) and the plan. The idea's `checks` run after the phase's
   own.

After each ACCEPTed stage the idea's branch `ratchetloop/<idea_key>` is fast-forwarded to that stage's
head — never rewound, never while checked out in any worktree: it is the one branch a person merges
(D28), holding every stage's commits in order.

Each stage is an ordinary task run with its own run directory (§3) and `result.json` (§5).

### 9.3 Idea result

`<runs_root>/<idea_key>/<run_id>/` holds `idea.md` as admitted, `events.jsonl` (one `stage_start` and
one `stage_end` per stage, plus `gate`), and `result.json`:

```json
{
  "idea_key": "calc-cli", "run_id": "…", "kind": "idea",
  "disposition": "FAILED", "reason": "checks_failed", "stage": "calc-cli--divide",
  "branch": "ratchetloop/calc-cli", "base_sha": "…", "head_sha": "…",
  "stages": [
    {"task_key": "calc-cli--plan", "run_id": "…", "disposition": "ACCEPT"},
    {"task_key": "calc-cli--bootstrap", "run_id": "…", "disposition": "ACCEPT"},
    {"task_key": "calc-cli--divide", "run_id": "…", "disposition": "FAILED"}
  ],
  "usage": {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "ai_credits": 0,
            "launches_with_unknown_usage": 0},
  "velocity": {"phases_planned": 3, "phases_accepted": 1, "lines_added": 0, "tests_added": 0,
               "review_rounds": 0, "wall_s": 0}
}
```

The idea's disposition is ACCEPT only if every stage ACCEPTed; otherwise it is the first non-ACCEPT
stage's disposition and reason, with `stage` naming it. Usage and velocity are sums over the stages —
stages skipped on `--continue` included, so the totals are the idea's; each stage's own numbers stay
in its own `result.json`, and each `stages` entry says whether it was `skipped`. An idea can also end
FAILED `refused` (a stage's task was refused at admit), FAILED `idea_branch` (the idea's branch was
moved outside the pipeline, or is checked out), PAUSED `gate`, or FAILED `abandoned` (a build that
died, recorded by the next build of the idea; its stages keep their own records).

| Idea disposition | Exit |
| --- | --- |
| as §6 | as §6 |
| PAUSED (waiting at `--gate plan`) | 6 |

### 9.4 Re-running and budgets

`build` refuses an idea whose branches exist; `build --continue` skips every stage whose latest run
ended ACCEPT — or was carried (§9.5) — and whose branch still points at that head — the plan stage's may have moved on by a
person's edit at the gate; a code stage's that moved on holds unreviewed commits and is refused, so
they never reach the idea's branch — and starts at the first that did not, with `run --continue`
(§1.1) when that stage's branch exists. A SIGTERM or SIGINT stops the idea STOPPED `killed` at the
stage it reached, even one that lands as a stage writes its result. Nothing else carries state between stages.
One build of an idea runs at a time (its flock, as for tasks). The idea's `cost_budget_usd` and
`wall_budget_s` count this build's measured spend and time; each stage runs with what is left of
them; launches with unknown usage are bounded by `wall_budget_s` (D30). Either budget ends the idea at the next stage boundary with FAILED
`budget_cost` or `budget_wall`.

### 9.5 Carried residuals

A code stage whose reviewer still asks for changes after its last round, with the checks green at
the reviewed head and no `blocking` finding, does not end the idea: the build records the residual
findings and starts the next phase from that stage's branch (D35). The stage's own `result.json`
keeps its HUMAN_REVIEW `review_rounds_exhausted`; `carried.json` beside it names the head the idea
went on from, and the idea's `stages` entry carries `"carried": true` and the `residuals`.

Residuals are data parsed from the review summary — a `bug — file:line: text` finding keeps its
tag, file and line; a `finding N remains` or `unfixed:` statement is an unfixed bug; a `notes:`
observation is a nit (kept even when the round ACCEPTs); `fixed:` and `resolved:` statements are
dropped; anything else is kept whole as a `note` — written cumulatively to
`<idea run dir>/residuals.json` and appended to `docs/REVIEW_NOTES.md` on the stage branch by the
pipeline's own commit, `ratchetloop(<task_key>): residual review notes`, so they travel with the code
to the idea's branch. Every later phase's task gets them as `carried_findings` (§2): its coder is told
to fix those its phase touches, its reviewer that they are open, may be closed with a `resolved:
<file:line>` line or re-raised, and are not grounds for REQUEST_CHANGES outside the phase's diff. A
later review that says `fixed:` or `resolved:` naming a residual's file closes it (`resolved_by`).

Round 2 of any review is a fix-check: the brief asks for one `fixed: <n>` or `unfixed: <n>` line per
round-1 finding, the verdict follows those lines, and new observations go under `notes:` without
changing it.

The idea's `result.json` lists `residuals` (each with `stage`, `round`, `tag`, `file`, `line`,
`text`, `status`, `resolved_by`) and `velocity.phases_carried`; `build` prints
`<disposition> with N residual findings, M open` after its result line; `status IDEA_KEY` adds
`carried_stages` and `open_residuals`; `stats` counts carried phases and residuals per idea. The
merge is where a person reads them. Hard stops stay hard: red checks, a blocked coder, a
`blocking` finding, or `on_rounds_exhausted: stop` end the idea as before.
