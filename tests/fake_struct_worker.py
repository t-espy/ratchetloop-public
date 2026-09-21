"""Emit canonical JSONL worker events for structured-I/O tests."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = argv[0] if argv else "result"
    if mode == "all_kinds":
        for ev in (
            {"type": "progress", "text": "working"},
            {"type": "tool", "name": "Read"},
            {"type": "usage", "input_tokens": 1, "output_tokens": 2},
            {"type": "result", "status": "done", "reason": "", "summary": "ok"},
        ):
            print(json.dumps(ev), flush=True)
        return 0
    if mode == "silence":
        print(json.dumps({"type": "progress", "text": "start"}), flush=True)
        time.sleep(float(argv[1]) if len(argv) > 1 else 30)
        return 0
    if mode == "leave_child":  # like a build server: outlives the worker, in the worker's group
        import subprocess
        child = subprocess.Popen(["sleep", "30"], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        Path(argv[1]).write_text(str(child.pid))
        print(json.dumps({"type": "result", "status": "done", "reason": "", "summary": "ok"}),
              flush=True)
        return 0
    if mode == "bad_result":
        print(json.dumps({"type": "result", "status": "nope"}), flush=True)
        return 0
    if mode == "error":
        print(json.dumps({"type": "error", "code": "boom", "text": "boom"}), flush=True)
        return 1
    if mode == "quota":
        print(json.dumps({"type": "error", "code": 429, "text": "limited"}), flush=True)
        return 1
    if mode == "exit":
        print(json.dumps(
            {"type": "result", "status": "done", "reason": "", "summary": "ok"}
        ), flush=True)
        return int(argv[1]) if len(argv) > 1 else 0
    if mode == "blocked":
        print(json.dumps({
            "type": "result", "status": "blocked",
            "reason": "cannot", "summary": "stopped",
        }), flush=True)
        return 0
    if mode == "none":
        print("plain text, not an event", flush=True)
        return 0
    if mode == "partial":
        sys.stdout.buffer.write(b"{")
        sys.stdout.buffer.flush()
        time.sleep(float(argv[1]) if len(argv) > 1 else 30)
        return 0
    if mode == "huge":
        sys.stdout.buffer.write(b"x" * (1024 * 1024 + 1))
        sys.stdout.buffer.flush()
        time.sleep(float(argv[1]) if len(argv) > 1 else 5)
        return 0
    if mode == "stderr_quota":
        sys.stderr.write(json.dumps({"type": "error", "code": 429}) + "\n")
        sys.stderr.flush()
        print(json.dumps({
            "type": "result", "status": "done", "reason": "", "summary": "ok",
        }), flush=True)
        return 0
    if mode == "prose_result":
        print(json.dumps({
            "type": "assistant",
            "content": json.dumps({
                "status": "done", "reason": "", "summary": "ok",
            }),
        }), flush=True)
        return 0
    if mode == "error_then_result":
        print(json.dumps({"type": "error", "code": "boom", "text": "boom"}), flush=True)
        print(json.dumps({
            "type": "result", "status": "done", "reason": "", "summary": "ok",
        }), flush=True)
        return 0
    if mode == "result_then_reconnect":
        print(json.dumps({
            "type": "result", "status": "done", "reason": "", "summary": "ok",
        }), flush=True)
        print(json.dumps({
            "type": "error",
            "message": "Reconnecting... (request timed out)",
        }), flush=True)
        return 0
    if mode == "schema_then_reconnect_then_result":
        print(json.dumps({"type": "error", "message": "invalid_json_schema"}), flush=True)
        print(json.dumps({
            "type": "error",
            "message": "Reconnecting... (request timed out)",
        }), flush=True)
        print(json.dumps({
            "type": "result", "status": "done", "reason": "", "summary": "ok",
        }), flush=True)
        return 0
    if mode == "honors_o":
        dest = argv[argv.index("-o") + 1] if "-o" in argv else None
        print(json.dumps({
            "type": "result", "status": "done", "reason": "", "summary": "ok",
        }), flush=True)
        if dest:
            Path(dest).write_text("last-message-sidecar\n")
        return 0
    print(json.dumps(
        {"type": "result", "status": "done", "reason": "", "summary": "ok"}
    ), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
