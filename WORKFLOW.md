# ratchetloop policy — example

Copy this file to `~/.config/ratchetloop/WORKFLOW.md`, pass `--policy PATH`, or set
`RATCHETLOOP_POLICY`. ratchetloop reads the FIRST fenced yaml block below. A missing
file named by `--policy` or the variable, an unknown key, or a malformed ladder
refuses the run. With no policy file at all, every CLI runs its own default model.

```yaml
models:
  ladders:
    codex:
      - {model: gpt-5.6-luna, effort: medium}
      - {model: gpt-5.6-terra, effort: medium}
      - {model: gpt-5.6-sol, effort: high}
    grok:
      - {model: grok-4.6, effort: low}
      - {model: grok-4.6, effort: medium}
      - {model: grok-4.6, effort: high}
    claude:
      - {model: sonnet, effort: medium}
      - {model: sonnet, effort: high}
      - {model: opus, effort: high}
  tier_start: {light: 0, standard: 1, heavy: 2}
  default_tier: {coder: standard, reviewer: standard}
  review_light_max_lines: 80
  review_heavy_min_lines: 750
```

## Keys

- `ladders` — per provider (`grok`, `codex`, `claude`), a list of rungs, cheapest first.
  Each rung is `{model, effort?, family?}`. `family` defaults to the maker for grok (xai),
  codex (openai) and claude (anthropic). Reviewer independence compares families, not CLIs
  (`docs/DECISIONS.md` D6).
- `tier_start` — the rung each tier starts on (`light`, `standard`, `heavy`).
- `default_tier` — the tier per role when the task sets no `tier`.
- `review_light_max_lines` / `review_heavy_min_lines` — a review of at most / at least
  this many changed lines is light / heavy.
- Each earlier run of the same task in which the same role failed (blocked, invalid
  result, protocol error, or failed checks after a coder) climbs one rung. Quota
  failures do not climb.
