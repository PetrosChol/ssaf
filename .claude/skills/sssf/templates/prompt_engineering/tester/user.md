# Test Task

## Variables

### prompt

{{prompt}}

### previous_envelope

{{previous_envelope}}

### context_handoff_dir

{{context_handoff_dir}}

## Task

Two kinds of send arrive here. Tell them apart by `previous_envelope`:

**Write** — `previous_envelope` is the test plan (`TestPlanOutput`). Read `<context_handoff_dir>/test_plan.md` — the approved plan, possibly edited by the engineer — and write every case it lists, in the files it names unless it says otherwise. Do not change the file under test.

**Triage** — `previous_envelope` has `"passed": false` and `failures`: the project's test command ran your tests and they failed. The output in `failures` is verbatim; the full log is in its `artifacts`. Triage each failure as your instructions say: repair the tests that are wrong, report the bugs that are real, change nothing else.

Then emit your `Report` JSON.

## Report

Respond with ONLY valid JSON matching `TesterOutput` — no prose before or after:

```json
{
  "status": "success",
  "summary": "<one sentence: what you wrote, or what you repaired and what you reported>",
  "test_files": ["tests/test_calc.py"],
  "changed_files": ["tests/test_calc.py"],
  "cases": ["test_add_two_numbers", "test_divide_by_zero_raises"],
  "fixed": ["test_add_two_numbers expected 5 for add(2, 2); corrected to 4"],
  "suspected_bugs": [
    {
      "test": "tests/test_calc.py::test_divide_by_zero_raises",
      "observed": "divide(1, 0) returns 0",
      "expected": "raises ZeroDivisionError, as its docstring says",
      "evidence": "src/calc.py:12 — 'Raises ZeroDivisionError when b is 0'"
    }
  ],
  "artifacts": [],
  "commit_message": "<imperative one-line git subject for adding these tests>",
  "notes_for_next_agent": ""
}
```

- `test_files`: EVERY test file this workflow has written so far, not just this send's. The next test run runs exactly these.
- `changed_files`: only what THIS send changed. Empty when every failure is a suspected bug — that ends the loop.
- `suspected_bugs`: the full CURRENT list, every test still failing because of the code. Not only the new ones.
- `status` is `success` when you did your job, even if bugs remain: finding them is the job.
