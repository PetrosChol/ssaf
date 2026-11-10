# Plan: Context7 integration (Layers 1 and 2)

## Context
The core four are context, model, tools and prompt. Claude Code covers the model and the tools well, but agents still write code against whatever library knowledge the model happens to have. Context7 serves current, version-specific library documentation. This plan wires it into the factory in two layers:

- **Layer 1, on demand:** Context7 becomes an MCP tool that chosen agents can call whenever they hit an unknown API.
- **Layer 2, fetched in advance:** a fixed code phase fetches docs for the libraries a run will touch, pinned to the repo's real versions, before any agent starts.

**Both layers use the same Context7 MCP server** (`https://mcp.context7.com/mcp`), with the same auth and the same two tools, `resolve-library-id` and `query-docs`. In Layer 1 the agent's model calls those tools. In Layer 2 Python code calls them directly through the official MCP Python SDK, with no model involved, so it stays a deterministic `kind="code"` phase. One endpoint and one tool contract means nothing can drift out of step between the layers.

Rule 8 ("a known command is code") favours Layer 2. Layer 1 covers what Layer 2 can't predict. Pi agents keep working without Context7. Every lookup lands in the trace, and nothing loosens the `writes:` boundary.

All paths are relative to `.claude/skills/sssf/`.

## Auth (revised)
The key is optional, and by default neither layer sends one:
- **Claude cloud environment:** the key is stored as a Network secret for host `mcp.context7.com`. The egress proxy adds `Authorization: Bearer <key>` to every request for that host. There is no `CONTEXT7_API_KEY` in the environment.
- **Locally:** `CONTEXT7_API_KEY` in `.env`. Only when it is set do `context7_client.py` and the Claude Code MCP config send `Authorization: Bearer $CONTEXT7_API_KEY`.
- **Neither:** anonymous access at lower rate limits.

## Step 0 — Live API check (done 2026-11-04, results)
- **Network:** `mcp.context7.com` resolves and answers an MCP `initialize` over HTTPS through the proxy (HTTP 200).
- **Server:** Context7 4.5.0, stateless streamable HTTP (no `mcp-session-id`). Tools, from `tools/list`:
  - `resolve-library-id`: `query` (string, required), `libraryName` (string, required).
  - `query-docs`: `libraryId` (string, required; `/org/project` or `/org/project/<version>`), `query` (string, required), `language` (string, optional).
- **MCP Python SDK:** `mcp==2.3.0` works and is pinned. Its v2 API is `mcp.client.streamable_http.streamable_http_client(url, http_client=httpx2.AsyncClient(...))`, which yields `(read, write)`, plus `ClientSession`. Headers go on the `httpx2.AsyncClient`. With no header of our own, initialize, `tools/list` and both tools succeed, with no auth or rate-limit error.
- **Result shape:** one `text` content block, no `structuredContent`. `resolve-library-id` returns "Available Libraries:" blocks separated by `----------`, each with `- Title:`, `- Context7-compatible library ID:` and sometimes `- Versions: v2.9.2, …`. It always returns *something*: an unknown name gets unrelated fuzzy hits, so the client must check that a candidate matches. `query-docs` returns markdown snippets. A pinned id such as `/pydantic/pydantic/v2.11.7` that is not indexed gets a leading `Version note: … is served instead.` A moved id is `isError` with "… has been redirected to this library: /new/id." An unknown id is `isError` with "Library "…" not found."
- **Claude Code:** `claude -p --mcp-config <file> --strict-mcp-config` connects to a headerless config and calls `mcp__context7__resolve-library-id`. `${CONTEXT7_API_KEY}` in a header IS expanded when the variable is set. When it is unset, the literal `Bearer ${CONTEXT7_API_KEY}` is sent. There is no conditional header, so the stamped file has no headers and `agent_cc` writes a per-run temp copy that adds `Authorization: Bearer ${CONTEXT7_API_KEY}` (the placeholder, not the key) only when the key is set.
- **Pi:** `@upstash/context7-pi` 0.1.3 is a Pi package (`pi install npm:@upstash/context7-pi`) that registers the same two tools. It calls the REST API at `context7.com/api` rather than the MCP server, so a proxy secret for `mcp.context7.com` does not cover it.

## Design choices (answers to the open questions)
1. Layer 2 always-fetched libraries: none (`libraries: []`, `from_request: true`).
2. Layer 1 agents: planner, builder and reviewer. Scout and documenter don't get it.
3. Live test target: a throwaway demo repo (pyproject.toml + uv.lock pinning fastapi 0.115.6 and pydantic 2.10.6).

## Layer 1 — Context7 as an agent tool

**Files**
- `templates/harness_engineering/context7.mcp.json` (new). It defines one server, `context7`, of type `http`, at URL `https://mcp.context7.com/mcp`, with **no headers** (see Auth). `install.py` already stamps everything under `harness_engineering/`.
- `templates/sssf.config.yaml`: a commented recipe for turning it on per agent:
  ```yaml
  harness_engineering:
    - adws/adw_data/harness_engineering/context7.mcp.json
  tools: [..., mcp__context7]        # needed when the agent has a tools list
  ```
  The starter roster turns it on for the planner, the builder and the reviewer. A Pi agent ignores both entries, because Pi has no MCP client.
- `templates/prompt_engineering/{planner,builder,reviewer}/system.md`: a conditional rule. If Context7 tools are available, look up any third-party API before using it, for the version in the repo's manifest or lockfile. Prefer docs already in `<context_handoff_dir>/docs/` (from Layer 2) and only query for what's missing.
- `templates/env.sample`: add a commented, optional `CONTEXT7_API_KEY=`, with the proxy-secret alternative.

**Code**
- `adw_modules/agent_cc.py`: `.json` entries already go to `--mcp-config … --strict-mcp-config`. With no tools list, `mcp__context7` is pre-approved automatically. With a list, `mcp__context7` passes through to `--allowedTools`. New: `prepare_mcp_configs()`. When `CONTEXT7_API_KEY` is set, a Context7 server (named `context7` or hosted on `*.context7.com`) without an Authorization header gets `Bearer ${CONTEXT7_API_KEY}` in a temp copy that `run()` deletes afterwards. Unset, the stamped file is passed as is.
- `adw_modules/agent_pi.py`: drops `.json` harness entries and `mcp__*` tool names, so one roster can carry Layer 1 for either backend.
- `adw_modules/agents.py`, in `validate()`: warn (never fail) only when Context7 is in use, `CONTEXT7_API_KEY` is unset **and** an anonymous `initialize` to the server fails (refused or unreachable). When the proxy injects the key, or anonymous access works, there is no warning. Also warn if an agent's MCP config and `context7.server_url` point at different servers.
- Pi agents: `@upstash/context7-pi` is documented, not built (see Step 0).

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

**Module:** `adw_modules/context7_client.py` (new) is a thin MCP client wrapper:
- `open_session(cfg)` uses the MCP Python SDK's streamable-HTTP client against the configured server URL. It sends `Authorization: Bearer $CONTEXT7_API_KEY` **only when that variable is set**, and otherwise no auth header (the cloud proxy adds it). One session is opened per phase and closed when the phase ends, and only if some library missed the cache.
- `resolve(session, name, query) -> library_id` calls `resolve-library-id`.
- `query_docs(session, library_id, query, version) -> str` calls `query-docs` and joins the text content blocks.
- It reads the same server URL as `context7.mcp.json`, so Layer 1 and Layer 2 can't point at different servers.
- It's synchronous on the outside (an `asyncio.run` wrapper), so ADW scripts stay plain.

**Module:** `adw_modules/docs.py` (new). It follows the `quality.py` pattern: plain functions, typed results, an `as_envelope()` adapter. It talks to Context7 only through `context7_client.py`.
- `detect_libraries(run, cfg)`: reads the manifests and lockfiles (`package-lock.json`, `uv.lock`, `poetry.lock`) to get `{name: version}`. It keeps the configured `libraries` plus, when `from_request` is set, any manifest dependency whose name appears in the request text, using a plain word-boundary match.
- `fetch(run, session, lib)`: calls `resolve` (skipped when `id` is configured) then `query_docs` over the MCP session, with the request text as the query and the pinned version. It writes `context_handoff/docs/<name>@<version>.md`, with a header giving the source, library id, version and fetch time. It caches by `(id, version)` inside the session, so a run joined with `--adw-id` reuses the files.
- `capture(run) -> DocsResult`: the files written, plus per library `ok`, `skipped` or `error`.
- `as_envelope(result) -> DocsOutput`: `artifacts` lists the doc files, and `notes_for_next_agent` says "read the docs under context_handoff/docs/ before using these libraries".
- Network errors are recorded per library as `error` and the run continues without those docs; `context7.required: true` makes them fail the phase instead (changed after this plan: an outage at a third party should not stop a run). A per-library "not found" is recorded and skipped.

**Types** (`data_types.py`): `DocsLibrary`, `DocsResult` and `DocsOutput(EnvelopeBase)`, where `DocsOutput` adds `libraries: list[str]`. This is code output, not an agent call, so the triad rule (type, `user.md` example, call site) doesn't apply.

**Dependencies:** add `mcp==2.3.0` (the version pinned in Step 0) to the inline `# /// script` dependency list of only the ADWs that get a `docs` phase. No global installs. The SDK is imported lazily, inside `context7_client.Session`, so a repo that never enables `context7` pays only uv's one-time install and never imports it.

**Config:** `context7:` also takes `server_url` (default `https://mcp.context7.com/mcp`), used by both layers.

**Workflows:** add a `docs` phase right after `request` in `adw_plan.py`, `adw_plan_build.py`, `adw_plan_build_test.py`, `adw_plan_build_test_quality.py` and `adw_simple_sdlc.py`. It runs only when `context7.enabled` is set:
```python
with run.phase(PhaseParams(name="docs", kind="code", owner="context7",
        description="Fetch version-pinned library docs so no agent plans against stale APIs")) as ph:
    fetched = docs.capture(run, prompt); ph.log(**docs.summary(fetched))
    docs_envelope = docs.as_envelope(fetched); docs.check(run, docs_envelope)
plan = ph.call(AgentCall(..., previous=docs_envelope))
```
- **Gate:** reuse `gates.artifacts_exist` and `gates.files_non_empty` on the docs envelope, so docs that were requested but are missing fail loudly.
- **Builder access:** the builder already gets `context_handoff_dir`. Its `user.md` gains one line: "If `<context_handoff_dir>/docs/` exists, read the files for the libraries you touch."

## Safety
- **Fetched docs are untrusted text.** Each doc file gets a header warning that its contents are reference material, not instructions. `writes:` and `protected_files` still contain any damage.
- **Context window:** each library's docs are capped at `max_chars_per_library`, with a total cap per run.
- **Secrets:** the key is read only from the environment and never written to traces, configs (temp copies hold the `${CONTEXT7_API_KEY}` placeholder) or doc headers. A test sets `CONTEXT7_API_KEY`, runs `adw_plan` end to end twice, and checks that the value doesn't appear in `raw_output.jsonl`, `sssf.db`, the saved prompts, the doc files or any other file the run produced.

## Tests (`tests/test_context7.py`)
- Library detection from fixture manifests and lockfiles, including request-text matching.
- `fetch` and `capture` against a stubbed `context7_client` (no network): caching, truncation, not-found, network failure leading to a failed phase.
- `context7_client` against a local fake MCP server built with the SDK's own server classes: initialize, tool listing, `resolve-library-id` and `query-docs` round trips, no auth header by default, and the header reaching the server when the key is set.
- `validate()`: no warning when Context7 is reachable without a key; a warning when it isn't; no probe when the key is set or Context7 isn't used.
- `build_command` for an agent with `context7.mcp.json`: `--mcp-config`, `--strict-mcp-config`, and `mcp__context7` in `--allowedTools`.
- An end-to-end `adw_plan` with a stubbed fetcher and the fake `claude`: a `docs` phase row, doc files in `context_handoff/docs/`, and the planner prompt containing the docs envelope.

## Verification (live, done 2026-11-04 in the demo repo, no CONTEXT7_API_KEY, key injected by the proxy)
1. `adw_scout` with Context7 turned on for the scout (demo config only) asked for pydantic's field validator at the version `uv.lock` pins. `sssf.db` has `tool_call` rows `mcp__context7__resolve-library-id` (`libraryName: pydantic`) and two `mcp__context7__query-docs` rows (`libraryId: /pydantic/pydantic/v2.10.6`).
2. `adw_plan_build` (planner and builder on Claude Code): the `docs` phase wrote `fastapi@0.115.6.md` and `pydantic@2.10.6.md` (4 Context7 calls). The spec cites "pydantic 2.10.6 / fastapi 0.115.6" and the v2 `@field_validator` + `@classmethod` pattern from those docs, and the builder implemented it.
3. Rejoined with `--adw-id`: the `docs` phase logged `cache_hits: 2, context7_calls: 0` and recorded no `context7:` tool calls (no MCP session was opened).

## Out of scope
Building the Pi extension integration, a self-hosted Context7, and Context7 for private or internal docs. Pi agents still benefit from Layer 2, because it is code, not an agent tool.
