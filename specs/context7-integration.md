# Plan: Context7 integration (Layers 1 and 2)

## Context
The core four are context, model, tools and prompt. Claude Code covers the model and the tools well, but agents still write code against whatever library knowledge the model happens to have. Context7 serves current, version-specific library documentation. This plan wires it into the factory in two layers:

- **Layer 1, on demand:** Context7 becomes an MCP tool that chosen agents can call whenever they hit an unknown API.
- **Layer 2, fetched in advance:** a fixed code phase fetches docs for the libraries a run will touch, pinned to the repo's real versions, before any agent starts.

Rule 8 ("a known command is code") favours Layer 2. Layer 1 covers what Layer 2 can't predict. Pi agents keep working without Context7. Every lookup lands in the trace, and nothing loosens the `writes:` boundary.

All paths are relative to `.claude/skills/sssf/`.

## Step 0 — Check the live API (needs network access)
- Confirm the MCP tool names and parameters with `claude mcp`, by listing the server's tools. The ones expected are `resolve-library-id(libraryName, query)` and `query-docs(libraryId, query, language?)`.
- Confirm the auth header the hosted server accepts: `Authorization: Bearer <key>`.
- Read the REST API guide (`context7.com/docs/api-guide`) to pick Layer 2's transport. Prefer **REST via Python `urllib`**, which adds no dependencies. Fall back to the `ctx7` CLI (needs Node 22.13 or newer) if REST doesn't cover search plus docs.
- Confirm that `${CONTEXT7_API_KEY}` inside a `--mcp-config` file gets filled in from the environment. If it doesn't, `agent_cc` writes a resolved temp copy per run.

## Layer 1 — Context7 as an agent tool

**Files**
- `templates/harness_engineering/context7.mcp.json` (new). It defines one server, `context7`, of type `http`, at URL `https://mcp.context7.com/mcp`, with the header `Authorization: Bearer ${CONTEXT7_API_KEY}`. `install.py` already stamps everything under `harness_engineering/`.
- `templates/sssf.config.yaml`: a commented recipe for turning it on per agent:
  ```yaml
  harness_engineering:
    - adws/adw_data/harness_engineering/context7.mcp.json
  tools: [..., mcp__context7]        # needed when the agent has a tools list
  ```
  It's on by default for nobody. You opt in for the planner, the builder and optionally the reviewer.
- `templates/prompt_engineering/{planner,builder,reviewer}/system.md`: a conditional rule. If Context7 tools are available, look up any third-party API before using it, for the version in the repo's manifest or lockfile. Prefer docs already in `<context_handoff_dir>/docs/` (from Layer 2) and only query for what's missing.
- `templates/env.sample`: add `CONTEXT7_API_KEY=`.

**Code**
- `adw_modules/agent_cc.py`: no change expected. `.json` entries already go to `--mcp-config … --strict-mcp-config`. With no tools list, `mcp__context7` is pre-approved automatically. With a list, `mcp__context7` passes through to `--allowedTools`.
- `adw_modules/agents.py`, in `validate()`: if an agent loads `context7.mcp.json` and `CONTEXT7_API_KEY` is unset, warn but don't fail, since Context7 works without a key at lower rate limits.
- Pi agents: document `@upstash/context7-pi` as an optional `-e` extension, after checking how it loads. Not built in this pass.

## Layer 2 — The docs code phase

**Config** (`data_types.py`, a new `Context7Config` on `SSSFConfig`, an optional `context7:` section):
```yaml
context7:
  enabled: true
  libraries:                 # always fetched for this repo
    - name: next.js          # resolved to an id once and cached
      id: /vercel/next.js    # optional; skips resolution
  from_request: true         # also fetch manifest deps the request names
  manifests: [package.json, pyproject.toml, requirements.txt]
  max_chars_per_library: 20000   # protects the context window
  timeout_s: 30
```

**Module:** `adw_modules/docs.py` (new). It follows the `quality.py` pattern: plain functions, typed results, an `as_envelope()` adapter.
- `detect_libraries(run, cfg)`: reads the manifests and lockfiles (`package-lock.json`, `uv.lock`, `poetry.lock`) to get `{name: version}`. It keeps the configured `libraries` plus, when `from_request` is set, any manifest dependency whose name appears in the request text, using a plain word-boundary match.
- `fetch(run, lib)`: calls `resolve` then `docs` with the request text as the query and the pinned version. It writes `context_handoff/docs/<name>@<version>.md`, with a header giving the source, library id, version and fetch time. It caches by `(id, version)` inside the session, so a run joined with `--adw-id` reuses the files.
- `capture(run) -> DocsResult`: the files written, plus per library `ok`, `skipped` or `error`.
- `as_envelope(result) -> DocsOutput`: `artifacts` lists the doc files, and `notes_for_next_agent` says "read the docs under context_handoff/docs/ before using these libraries".
- Network errors fail the phase with a clear message, never with empty docs. A per-library "not found" is recorded and skipped.

**Types** (`data_types.py`): `DocsLibrary`, `DocsResult` and `DocsOutput(EnvelopeBase)`, where `DocsOutput` adds `libraries: list[str]`. This is code output, not an agent call, so the triad rule (type, `user.md` example, call site) doesn't apply.

**Workflows:** add a `docs` phase right after `request` in `adw_plan.py`, `adw_plan_build.py`, `adw_plan_build_test.py`, `adw_plan_build_test_quality.py` and `adw_simple_sdlc.py`. It runs only when `context7.enabled` is set:
```python
with run.phase(PhaseParams(name="docs", kind="code", owner="context7",
        description="Fetch version-pinned library docs so no agent plans against stale APIs")) as ph:
    fetched = docs.capture(run); ph.log(...)
plan = ph.call(AgentCall(..., previous=docs.as_envelope(fetched)))
```
- **Gate:** reuse `gates.artifacts_exist` and `gates.files_non_empty` on the docs envelope, so docs that were requested but are missing fail loudly.
- **Builder access:** the builder already gets `context_handoff_dir`. Its `user.md` gains one line: "If `<context_handoff_dir>/docs/` exists, read the files for the libraries you touch."

## Safety
- **Fetched docs are untrusted text.** Each doc file gets a header warning that its contents are reference material, not instructions. `writes:` and `protected_files` still contain any damage.
- **Context window:** each library's docs are capped at `max_chars_per_library`, with a total cap per run.
- **Secrets:** the key is read only from the environment and never written to traces, configs or doc headers. A test checks that it doesn't appear in `raw_output.jsonl`.

## Tests (`tests/test_context7.py`)
- Library detection from fixture manifests and lockfiles, including request-text matching.
- `fetch` and `capture` against a stubbed HTTP layer: caching, truncation, not-found, network failure leading to a failed phase.
- `build_command` for an agent with `context7.mcp.json`: `--mcp-config`, `--strict-mcp-config`, and `mcp__context7` in `--allowedTools`.
- An end-to-end `adw_plan` with a stubbed fetcher and the fake `claude`: a `docs` phase row, doc files in `context_handoff/docs/`, and the planner prompt containing the docs envelope.

## Verification (live, after Step 0)
1. `adw_scout` with Context7 turned on asks a version-specific question. `tool_call` rows named `mcp__context7__…` appear in `sssf.db`.
2. `adw_plan_build` on a demo repo with a pinned library. The `docs` phase succeeds, and `context_handoff/docs/<lib>@<ver>.md` exists and isn't empty. The spec cites APIs from that version.
3. Re-run with `--adw-id`. The `docs` phase reports cache hits and makes no network calls.

## Out of scope
Building the Pi extension integration, a self-hosted Context7, and Context7 for private or internal docs.
