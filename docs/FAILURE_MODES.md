# ratchetloop — Failure Modes

What has gone wrong in agent-delivery pipelines, ordered by damage, and what this design does
about each. Nothing here is hypothetical.

## 1. A run ACCEPTs work that was never really judged

A green result the caller trusts, on a change nobody vetted.

- A blocked reviewer returned `reason: APPROVE`; the kernel remapped `blocked` to done and ACCEPTed.
- A regex overrode the reviewer and forced APPROVE.
- Empty checks passed vacuously. A driver read pytest's last line instead of its exit code.

**Response:** the verdict is the reviewer object's `reason` field and nothing else (`CONTRACT.md` §4).
`blocked` is never a verdict. Checks pass only on exit codes. ACCEPT requires checks and review at
the same `head_sha`. Empty checks are refused at admit.

## 2. A worker escapes the worktree

Containment covered only some providers. A worker wrote into the primary checkout.

**Response:** every provider gets the same git containment (`protocol.allow=never`) and per-role
sandbox flags. After each coder step: HEAD unmoved, `.git` untouched, every changed path inside
`allowed_paths`. After each review: worktree hash unchanged. Any breach fails the run.

## 3. Work or data is destroyed

`trees --discard` could delete arbitrary directories. `git add -A` committed a venv symlink. A
STOP sentinel was swept into a commit.

**Response:** discard is provenance-gated. Staging is explicit paths from porcelain status, never
`-A`. Sentinels and setup artifacts live outside the worktree or in its `info/exclude`.

## 4. A worker hangs or outlives its run

A blocking read defeated the silence timer. Process-group kill was never confirmed. A systemd
oneshot killed detached children.

**Response:** non-blocking reads; stdout and stderr to separate files; process-group TERM then KILL
and confirm. `--detach` uses `setsid`. Liveness from log growth or newest worktree mtime.

## 5. The pipeline misreads a worker

Results parsed from narration. stderr parsed as events produced a false quota. Marker regexes
took the first match in the transcript.

**Response:** one final JSON object per worker. Quota signatures from the structured stream only.
Exit code never ignored.

## 6. Provider quota mid-run

Every CLI has run out of quota.

**Response:** quota is its own `kind`. The provider goes on cooldown. A coder quota ends the run
`quota_exhausted` so the caller can rerun later.

## 7. The pipeline itself dies

Resume that could not change the coder, and replay that duplicated records.

**Response:** events are on disk before each step; `result.json` in `finally`. The branch and
worktree are the resumable state; `--continue` picks them up.

## 8. Environment drift

The caller's environment leaked into target checks. PATH lacked provider CLIs. A brief on argv
exceeded `MAX_ARG_STRLEN`.

**Response:** workers and checks get an explicit environment. CLIs resolve to absolute paths at
admit. Briefs go by stdin or file.

## 9. Arguments and configuration

`pin.sh --help` created a directory named `--help`. Callers accepted task specs that admit refused.

**Response:** positional arguments that start with `-` are refused. One validator; `run --check`
is that validator.

## 10. Two good branches make a bad merge

Two ACCEPTed branches collided on merge.

**Response:** out of the pipeline's scope — it never merges — but `result.json` records `base_sha`,
and `review --base` re-judges a branch against a moved base.

## 11. Design review that never ends

Thousands of lines of review for a few hundred lines of code. Patching a reviewed design
paragraph by paragraph left stale text.

**Response:** `doc` tasks get the same two rounds as code. Only a document that is the next build
contract goes through the pipeline. On revise, the author rewrites the document whole.

## 12. Spend nobody can see

Launches with no recorded cost. A separate velocity service that grew larger than the deliveries
it described.

**Response:** every launch records `usage_source`; unknowns are counted, never zero-filled. Usage
and velocity land in `result.json`. Reporting is `stats`, a reader of run directories.
