# Calling ratchetloop

How a caller — a script or a person — uses the pipeline. Everything is a file in and a file
out; there is no API, database, or daemon. `docs/CONTRACT.md` is the authority for each shape.

## One change

1. Write a task file (`CONTRACT.md` §2): `task_key`, `repo`, `objective` or `brief_file`,
   `acceptance_criteria`, `checks`, and `setup` if the checks need an environment (link a venv or
   `node_modules`). A linked venv that holds an editable install of the project imports the
   original checkout's source, not the worktree's: set `PYTHONPATH` (e.g. `src`) in the task's `env`
   so the checks import the worktree.
2. Validate it with the pipeline's own code: `ratchetloop run TASK.yaml --check` exits 0, or 2 with
   the reason on stderr (D22). A caller never accepts a task the pipeline would refuse.
3. Run it: `ratchetloop run TASK.yaml [--policy WORKFLOW.md]`. The exit code is the disposition
   (§6: 0 ACCEPT, 3 HUMAN_REVIEW, 4 FAILED, 5 STOPPED, 2 refused). With `--detach` the command
   returns at once and prints `{"detached": pid, "run_dir": …, "log": …}`; the run goes on in its own
   session. A new session survives a closed terminal, not a cgroup kill: from a systemd unit, start
   it with `systemd-run --user --scope …` or give the unit `KillMode=process`.
4. Read the outcome from `<runs_root>/<task_key>/<run_id>/result.json` (§5), or with
   `ratchetloop status TASK_KEY`, which also shows a live run's last event. `result.json` is written
   on every exit path; never read a verdict from `events.jsonl` or from a worker's output.
5. A person merges `ratchetloop/<task_key>` if the disposition is ACCEPT. The pipeline never merges,
   pushes or deploys (D9).

After a disposition short of ACCEPT, or a death, `ratchetloop run TASK.yaml --continue` picks the task
up (§1.1). A fresh attempt is a new `task_key`.

## From an idea

Write an idea file (§9.1) and run `ratchetloop build IDEA.md [--gate plan] [--detach]`. The idea's
`result.json` is under `<runs_root>/<idea_key>/`; each stage's is under `<runs_root>/<idea_key>--<stage>/`.
`build --continue` resumes after a stop; `ratchetloop approve IDEA_KEY` releases a gate. A person
merges `ratchetloop/<idea_key>`.

## Stopping, spend, environment

- Launch the pinned runtime, never a checkout's venv: `deploy/ratchetloop …`, which runs
  `<opt>/current/.venv/bin/ratchetloop` (`<opt>` is `$RATCHETLOOP_OPT`, default `~/opt/ratchetloop`)
  with `PYTHONSAFEPATH=1` and without the caller's `PYTHONPATH`. A person pins, promotes and rolls
  back: `deploy/pin.sh SHA`, `deploy/promote.sh SHA --approved-by NAME`, `deploy/rollback.sh SHA`.

- Stop every run at its next step: create `<runs_root>/STOP`; one run: `<run dir>/STOP`; or send the
  process SIGTERM (§8).
- Spend and velocity: `ratchetloop stats [--since DATE] [--repo PATH] --json` reads the `result.json`
  files. A launch whose provider measures tokens but reports no cost (codex) has its tokens summed
  and is counted in `launches_without_cost`, because `cost_usd` leaves it out (D30); a launch with no
  usage at all is in `launches_with_unknown_usage`. Review records add spend, not tasks.
- `RATCHETLOOP_RUNS_ROOT` (default `~/var/ratchetloop/runs`), `RATCHETLOOP_WORKTREE_ROOT` and
  `RATCHETLOOP_LOCK_DIR` move the pipeline's files; the target repository needs a git identity
  (`user.name`, `user.email`), because the pipeline commits there.
