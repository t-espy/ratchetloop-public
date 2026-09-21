"""Usage comes from structured output only; reported zeros stay, missing numbers stay None.
The fixtures are the real result shapes from the Phase 0 review and the Phase 1 spike (2026-09-11)."""
from ratchetloop.worker_usage import UNKNOWN, parse_usage, usage_from_obj

GROK_RESULT = {
    "type": "result", "result": "{}",
    "usage": {"input_tokens": 9107, "output_tokens": 229, "cache_read_input_tokens": 48512,
              "cache_creation_input_tokens": 0},
    "total_cost_usd": 0.01490696,
    "modelUsage": {"grok-4.6-build": {"inputTokens": 9107, "outputTokens": 229,
                                      "costUSD": 0.01490696}},
}
CLAUDE_RESULT = {
    "type": "result", "result": "{}",
    "usage": {"input_tokens": 6, "cache_creation_input_tokens": 13011,
              "cache_read_input_tokens": 65338, "output_tokens": 348},
    "total_cost_usd": 0.0699936,
    "modelUsage": {
        "claude-haiku-4-5-20251001": {"inputTokens": 1315, "outputTokens": 15, "costUSD": 0.00139},
        "claude-sonnet-5": {"inputTokens": 6, "outputTokens": 348, "costUSD": 0.0686036},
    },
}
CODEX_TURN = ('{"type":"turn.completed","usage":{"input_tokens":58709,'
              '"cached_input_tokens":42240,"cache_write_input_tokens":0,'
              '"output_tokens":491,"reasoning_output_tokens":168}}')


def test_grok_result_counts_cache_reads_as_input():
    assert usage_from_obj(GROK_RESULT) == {
        "tokens_in": 9107 + 48512, "tokens_cached": 48512, "tokens_out": 229,
        "cost_usd": 0.01490696, "usage_source": "measured", "model_reported": "grok-4.6-build",
    }


def test_claude_result_sums_cache_and_names_the_model_that_did_the_work():
    got = usage_from_obj(CLAUDE_RESULT)
    assert (got["tokens_in"], got["tokens_cached"], got["tokens_out"]) == (78355, 65338, 348)
    assert got["cost_usd"] == 0.0699936
    assert got["model_reported"] == "claude-sonnet-5"  # not the side-task haiku listed first


def test_codex_input_already_includes_cached_reads():
    got = parse_usage('{"type":"thread.started"}\n' + CODEX_TURN + "\nnot json\n")
    assert (got["tokens_in"], got["tokens_cached"], got["tokens_out"]) == (58709, 42240, 491)
    assert got["cost_usd"] is None  # codex reports tokens, not cost


def test_last_report_wins_across_a_jsonl_stdout():
    stdout = ('{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2}}\n'
              '{"type":"turn.completed","usage":{"input_tokens":30,"output_tokens":4}}\n')
    assert (parse_usage(stdout)["tokens_in"], parse_usage(stdout)["tokens_out"]) == (30, 4)


def test_reported_zero_is_kept_and_booleans_are_not_counts():
    got = usage_from_obj({"usage": {"input_tokens": 0, "output_tokens": 0}})
    assert (got["tokens_in"], got["tokens_cached"]) == (0, None)
    assert usage_from_obj({"usage": {"input_tokens": True}}) is None


def test_nothing_reported_stays_unknown():
    assert parse_usage("") == UNKNOWN
    assert parse_usage("", {"type": "result", "result": "ok"}) == UNKNOWN
    assert usage_from_obj({"type": "progress"}) is None
    assert UNKNOWN["tokens_in"] is None
