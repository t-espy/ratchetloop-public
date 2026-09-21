"""Every assembled brief ends with the result contract the adapter scores (predecessor item 79,
2026-09-08: a finished grok coder scored invalid_result because nothing had told it)."""
import json

import pytest

from ratchetloop.worker_brief import (
    REVIEWER_CONTRACT, REVIEWER_RESULT_CONTRACT, RESULT_CONTRACT, assemble_brief, role_prompt,
)
from ratchetloop.worker_io import result_fields


def test_coder_brief_is_prompt_then_task_then_contract(tmp_path):
    dest = tmp_path / "in" / "brief.md"
    assemble_brief("coder", "# task\ndo the thing", dest)
    text = dest.read_text()
    assert text.startswith(role_prompt("coder"))
    assert "# task\ndo the thing" in text
    assert text.endswith(RESULT_CONTRACT)
    for key in ('"status"', '"reason"', '"summary"', '"done"', '"blocked"'):
        assert key in RESULT_CONTRACT


def test_reviewer_brief_carries_the_verdict_field_and_findings(tmp_path):
    dest = tmp_path / "brief.md"
    assemble_brief("reviewer", "review it", dest)
    text = dest.read_text()
    assert text.startswith(role_prompt("reviewer"))
    assert REVIEWER_RESULT_CONTRACT in text and text.endswith(REVIEWER_CONTRACT)
    assert '"findings"' in REVIEWER_RESULT_CONTRACT


def test_no_contract_asks_for_a_report_file():
    # A "write SUMMARY.md" instruction to a read-only reviewer produced predecessor item 90.
    for contract in (RESULT_CONTRACT, REVIEWER_RESULT_CONTRACT):
        assert "Do not write SUMMARY.md" in contract


def test_unknown_role_is_refused(tmp_path):
    with pytest.raises(ValueError, match="planner"):
        assemble_brief("planner", "x", tmp_path / "b.md")


def test_grok_envelope_with_object_as_final_message_scores_done():
    final = {"status": "done", "reason": "brief implemented", "summary": "518 passed"}
    envelope = {"type": "result", "result": json.dumps(final)}
    assert result_fields(envelope) == final


def test_grok_envelope_with_prose_final_message_is_not_a_result():
    envelope = {"type": "result", "result": "All done, 518 passed.\n\nCODER_DONE"}
    assert result_fields(envelope) is None


def test_reviewer_findings_kept_when_well_formed_and_nulled_when_not():
    ok = {"status": "done", "reason": "APPROVE", "summary": "s",
          "findings": {"bug": 0, "suggestion": 1, "nit": 2}}
    assert result_fields(ok)["findings"] == {"bug": 0, "suggestion": 1, "nit": 2}
    bad = result_fields({**ok, "findings": {"bug": "one", "suggestion": 0, "nit": 0}})
    assert bad["reason"] == "APPROVE" and bad["findings"] is None
    assert "findings" not in result_fields({"status": "done", "reason": "x", "summary": "s"})
