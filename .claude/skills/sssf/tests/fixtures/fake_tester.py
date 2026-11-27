#!/usr/bin/env python3
"""A stand-in `claude` that plays test_planner and tester for the testing workflow.

It reads which task it was sent from the rendered user.md, does the file work a
real agent would, and answers with the Report JSON. FAKE_TESTER_SCENARIO steers it:

    green         writes correct tests
    broken_test   writes one wrong expectation; triage repairs the TEST
    bug           writes correct tests against buggy code; triage reports the bug
    touch_source  "fixes" things by editing the file under test (must be refused)
    weaken        triage reports nothing and changes nothing (must fail the gate)
    stray         also writes a test file outside the tests folder (must be refused)

The file under test is always src/calc.py; the tests go in <folder>/test_calc.py,
where <folder> is the tests folder the prompt names.
"""

import json
import os
import re
import sys
import uuid
from pathlib import Path

argv = sys.argv[1:]
session = (argv[argv.index("--resume") + 1] if "--resume" in argv
           else argv[argv.index("--session-id") + 1])
prompt = argv[-1]
scenario = os.environ.get("FAKE_TESTER_SCENARIO", "green")

GOOD = '''import pytest

from src.calc import add, divide


def test_add_two_numbers():
    assert add(2, 2) == {expected}


def test_divide_by_zero_raises():
    with pytest.raises(ZeroDivisionError):
        divide(1, 0)
'''


def emit(event: dict) -> None:
    event.setdefault("session_id", session)
    event.setdefault("uuid", str(uuid.uuid4()))
    print(json.dumps(event), flush=True)


match = re.search(r"Put every test file in `([^`]+)/`", prompt)
FOLDER = match.group(1) if match else "tests"
TEST_FILE = f"{FOLDER}/test_calc.py"


def handoff_dir() -> Path:
    match = re.search(r"### context_handoff_dir\s*\n\s*\n(.+)", prompt)
    return Path(match.group(1).strip())


def plan() -> dict:
    (handoff_dir() / "test_plan.md").write_text(
        "# Test plan for src/calc.py\n\n## What the file does\nadd and divide.\n\n"
        "## Cases\n| test_add_two_numbers | basic |\n| test_divide_by_zero_raises | error |\n",
        encoding="utf-8")
    return {"status": "success", "summary": "two cases for src/calc.py",
            "artifacts": [str(handoff_dir() / "test_plan.md")],
            "target": "src/calc.py", "purpose": "arithmetic helpers", "framework": "pytest",
            "test_files": [TEST_FILE],
            "cases": [{"name": "test_add_two_numbers", "kind": "basic", "checks": "add(2, 2) == 4"},
                      {"name": "test_divide_by_zero_raises", "kind": "error",
                       "checks": "divide(1, 0) raises ZeroDivisionError"}],
            "notes_for_next_agent": ""}


def tester(base: dict, **fields) -> dict:
    return {**{"status": "success", "summary": "tester did its job", "artifacts": [],
               "test_files": [TEST_FILE], "changed_files": [],
               "cases": ["test_add_two_numbers", "test_divide_by_zero_raises"],
               "fixed": [], "suspected_bugs": [], "commit_message": "Add tests for src/calc.py",
               "notes_for_next_agent": ""}, **base, **fields}


def write() -> dict:
    Path(FOLDER).mkdir(parents=True, exist_ok=True)
    expected = 5 if scenario == "broken_test" else 4
    Path(TEST_FILE).write_text(GOOD.format(expected=expected), encoding="utf-8")
    if scenario == "stray":
        Path("test_stray.py").write_text("def test_stray():\n    pass\n", encoding="utf-8")
    if scenario == "touch_source":
        Path("src/calc.py").write_text("def add(a, b):\n    return 5\n", encoding="utf-8")
    return tester({}, changed_files=[TEST_FILE])


def triage() -> dict:
    if scenario == "broken_test":
        Path(TEST_FILE).write_text(GOOD.format(expected=4), encoding="utf-8")
        return tester({}, changed_files=[TEST_FILE],
                      fixed=["test_add_two_numbers expected 5 for add(2, 2); it is 4"])
    if scenario == "bug":
        return tester({}, suspected_bugs=[{
            "test": f"{TEST_FILE}::test_divide_by_zero_raises",
            "observed": "divide(1, 0) returns 0",
            "expected": "raises ZeroDivisionError, as the docstring says",
            "evidence": "src/calc.py:6"}])
    return tester({})                           # weaken: did nothing, said nothing


if "# Test Plan Task" in prompt:
    answer = plan()
elif "# Test Task" in prompt:
    previous = prompt.split("### previous_envelope", 1)[1].split("### context_handoff_dir", 1)[0]
    answer = triage() if '"passed": false' in previous else write()
else:                                           # a gate correction: change nothing, repeat
    answer = tester({})

text = json.dumps(answer)
usage = {"input_tokens": 10, "output_tokens": 20}
emit({"type": "system", "subtype": "init", "cwd": os.getcwd(), "tools": ["Read", "Write"],
      "model": "claude-haiku-5-5", "permissionMode": "dontAsk"})
emit({"type": "assistant", "parent_tool_use_id": None,
      "message": {"role": "assistant", "usage": usage, "content": [{"type": "text", "text": text}]}})
emit({"type": "result", "subtype": "success", "is_error": False, "result": text, "num_turns": 1,
      "total_cost_usd": 0.01, "usage": usage,
      "modelUsage": {"claude-haiku-5-5": {"contextWindow": 200000}}})
