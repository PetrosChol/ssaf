# Test Plan Task

## Variables

### prompt

{{prompt}}

### previous_envelope

{{previous_envelope}}

### context_handoff_dir

{{context_handoff_dir}}

## Task

Plan tests for the file named in `prompt`. Write no tests, and change nothing in the repo.

1. Analyse the file: what it is for, its public surface, its dependencies, and how this repo already tests similar code.
2. Write the plan to `<context_handoff_dir>/test_plan.md` with these sections:
   - `## What the file does` — a short paragraph, then the public surface as a list.
   - `## Approach` — runner, test file path(s), fixtures or fakes, anything mocked and why.
   - `## Cases` — a table: name · kind (`basic` / `edge` / `error`) · what it checks · where the expectation comes from.
   - `## Not covered` — what you chose not to test, and why.
   - `## Risks` — anything in the code that already looks wrong, with `file:line`.
3. Emit your `Report` JSON.

The engineer reads this file and approves it, or edits it first. Nothing is written until they do.

## Report

Respond with ONLY valid JSON matching `TestPlanOutput` — no prose before or after:

```json
{
  "status": "success",
  "summary": "<one sentence: how many cases, for what>",
  "artifacts": ["<context_handoff_dir>/test_plan.md"],
  "target": "<the file under test, repo-relative, exactly as given in prompt>",
  "purpose": "<what the file is for, one or two sentences>",
  "framework": "<the runner the tests use, e.g. pytest>",
  "test_files": ["tests/test_<name>.py"],
  "cases": [
    {"name": "test_add_two_numbers", "kind": "basic", "checks": "add(2, 3) returns 5"},
    {"name": "test_divide_by_zero_raises", "kind": "error", "checks": "divide(1, 0) raises ZeroDivisionError"}
  ],
  "notes_for_next_agent": "<what the tester must know: fixtures, imports, anything surprising>"
}
```

`test_files` are repo-relative paths the tester will create, every one inside the tests folder named in `prompt`. A gate rejects any that is not.
