#!/usr/bin/env python3
"""A stand-in `claude` binary for tests: speaks just enough stream-json.

Behaviour is steered by environment variables so one script covers every case:

    FAKE_CLAUDE_ARGV_LOG   append this call's argv (one JSON list per line)
    FAKE_CLAUDE_WRITE      a path to overwrite through a fake Write tool call
    FAKE_CLAUDE_COST       running total_cost_usd to report (default 0.01)
"""

import json
import os
import sys
import uuid

argv = sys.argv[1:]
if os.environ.get("FAKE_CLAUDE_ARGV_LOG"):
    with open(os.environ["FAKE_CLAUDE_ARGV_LOG"], "a") as log:
        log.write(json.dumps(argv) + "\n")

session = (argv[argv.index("--resume") + 1] if "--resume" in argv
           else argv[argv.index("--session-id") + 1])


def emit(event: dict) -> None:
    event.setdefault("session_id", session)
    event.setdefault("uuid", str(uuid.uuid4()))
    print(json.dumps(event), flush=True)


usage = {"input_tokens": 10, "cache_read_input_tokens": 100,
         "cache_creation_input_tokens": 50, "output_tokens": 20}
emit({"type": "system", "subtype": "init", "cwd": os.getcwd(),
      "tools": ["Read", "Write"], "model": "claude-haiku-5-5", "permissionMode": "dontAsk"})

target = os.environ.get("FAKE_CLAUDE_WRITE")
if target:
    emit({"type": "assistant", "parent_tool_use_id": None,
          "message": {"role": "assistant", "usage": usage, "content": [
              {"type": "tool_use", "id": "toolu_fake_1", "name": "Write",
               "input": {"file_path": target, "content": "x = 1\n"}}]}})
    with open(target, "w") as handle:
        handle.write("x = 1\n")
    emit({"type": "user", "parent_tool_use_id": None,
          "message": {"role": "user", "content": [
              {"type": "tool_result", "tool_use_id": "toolu_fake_1",
               "content": "File written", "is_error": False}]}})

envelope = {"status": "success", "summary": "fake claude did the work",
            "artifacts": [], "notes_for_next_agent": ""}
emit({"type": "assistant", "parent_tool_use_id": None,
      "message": {"role": "assistant", "usage": usage,
                  "content": [{"type": "text", "text": json.dumps(envelope)}]}})
emit({"type": "result", "subtype": "success", "is_error": False,
      "result": json.dumps(envelope), "num_turns": 1,
      "total_cost_usd": float(os.environ.get("FAKE_CLAUDE_COST", "0.01")),
      "usage": usage,
      "modelUsage": {"claude-haiku-5-5": {"contextWindow": 200000}}})
