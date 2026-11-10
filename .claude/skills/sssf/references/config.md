# Config Reference

The full `sssf.config.yaml` spec: every field, how defaults merge, and how model / thinking / tools / extensions map onto the coding agent.

It lives at **`adws/adw_sssf_config/sssf.config.yaml`** — the default path every `adw_*.py` and the justfile resolve, and where `install.py` / `make_config.py` stamp it. Pass `--config <path>` to any ADW (or set `SSSF_CONFIG` for the justfile) to run against a different roster.

## Shape

```yaml
defaults:
  coding_agent: pi
  model: google/gemini-3.6-flash        # ALWAYS provider/model-id
  thinking: medium
  harness_engineering: []
  tools: [read, bash, edit, write, grep, find, ls]
  data_dir: adws/adw_data

observability:
  db: adws/adw_data/sssf.db
  poll_ms: 500

context7:                               # optional — Layer 2, the docs phase (see Context7)
  enabled: true
  libraries: []
  from_request: true

agents:
  - name: planner
    coding_agent: pi
    model: google/gemini-3.6-flash        # ALWAYS provider/model-id
    thinking: high
    color: "#a78bfa"
    purpose: Turn a request into a plan the builder can implement without asking questions.
    prompt_engineering:
      system: adws/adw_data/prompt_engineering/planner/system.md
      user: adws/adw_data/prompt_engineering/planner/user.md
    harness_engineering:
      - json-enforcer
    tools:
      - read
      - bash
```

## Fields

### `defaults`

| Field | Type | Meaning |
|---|---|---|
| `coding_agent` | `pi` \| `claude_code` | Which interface runs the agent: `pi` (`agent_pi.py`) or Claude Code (`agent_cc.py`). Set per agent to mix both in one run. See [Claude Code backend](#claude-code-backend). |
| `model` | string | Model id. For Pi, any id registered in `~/.pi/agent/models.json`. For Claude Code, `anthropic/<model-id>` or an alias (`sonnet`, `opus`, `haiku`). Default `gemini-3.6-flash`. |
| `thinking` | enum | Reasoning effort — see below. Default `medium`. |
| `color` | hex string | Lane color for every agent that does not set its own. Default empty — the visualizer falls back to its own palette. |
| `harness_engineering` | list[string] | Coding-agent extensions. Pi: extension file paths (`-e`). Claude Code: JSON files: `*settings.json` → `--settings`, any other `.json` → `--mcp-config`. |
| `tools` | list[string] | Roster-wide tool allowlist. Every agent that omits its own `tools` inherits this. Unset = all tools usable. |
| `protected_files` | list[string] | Paths **no** agent may modify unless it names them in its own `writes`. Default: `adws/adw_modules/`, `adws/adw_sssf_config/`, `adws/adw_*.py`, `adws/conftest.py` — an agent must not be able to edit the machinery that decides whether its work passed. |
| `timeout_s` | int | Ceiling, in seconds, on **one send** to an agent (a JSON retry or gate correction is a new send). Past it the agent's whole process group is killed and the phase fails with `AgentTimeout`. `0` = no deadline. Default `3600`. Any agent may override it. |
| `data_dir` | path | Runtime home. Sessions land at `{data_dir}/sessions/{adw_id}/{agent_name}/`. Default `adws/adw_data`. |

### `observability`

| Field | Type | Meaning |
|---|---|---|
| `db` | path | SQLite trace db. `tracer.py` writes it directly; the visualizer polls it. Default `adws/adw_data/sssf.db`. |
| `poll_ms` | int | Visualizer live-poll cadence in ms. History uses the same queries, lazy-paged. Default `500`. |

### `context7`

Optional. Absent means `enabled: false`. Configures the `docs` code phase (Layer 2); Layer 1 is per agent, through `harness_engineering`. Full behaviour in [Context7](#context7).

| Field | Type | Meaning |
|---|---|---|
| `enabled` | bool | Run the `docs` phase in the ADWs that have one. Default `false`; the starter config sets `true`. |
| `required` | bool | `false` (default): if Context7 is unreachable, rate-limited or refuses the key, the affected libraries are recorded as `error`, the next agent is told it has no current docs for them, and the run continues. `true`: the phase fails and the run stops. |
| `send_request` | bool | The query sent with each lookup is the library name plus the first 300 characters of the request, so the server can rank snippets by topic. `false` sends library names and versions only. Default `true`. |
| `server_url` | URL | The Context7 MCP server, used by both layers. Default `https://mcp.context7.com/mcp`. `agents.validate()` warns if an agent's `context7.mcp.json` points elsewhere. |
| `libraries` | list | Always fetched: `{name, id?}` or a bare name. `id` (e.g. `/vercel/next.js`) skips resolution. Default `[]`. |
| `from_request` | bool | Also fetch each manifest dependency whose name appears in the request text (plain word match). Default `true`. |
| `manifests` | list[path] | Read for dependencies. Default `package.json`, `pyproject.toml`, `requirements.txt`. |
| `lockfiles` | list[path] | Read for the installed version, which beats the manifest's. Default `package-lock.json`, `uv.lock`, `poetry.lock`. |
| `max_chars_per_library` | int | Each library's docs are cut here. Default `20000`. |
| `max_chars_total` | int | Cap across all libraries in one run; later libraries are skipped once it is spent. Default `60000`. |
| `timeout_s` | int | Per MCP request. Default `30`. |

### `agents[]`

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | The identifier ADW scripts use. **ADWs name agents, never models.** |
| `purpose` | yes | One sentence: what this agent is for. Should match its `system.md` Purpose. |
| `prompt_engineering.system` | yes | Path to the system prompt — who the agent is, its single purpose, its output contract. |
| `prompt_engineering.user` | yes | Path to the default user prompt — the task template with `{{prompt}}`, `{{previous_envelope}}`, `{{context_handoff_dir}}`. |
| `color` | no | Hex swatch (`"#a78bfa"`) for this agent's lane in the visualizer. Travels config → `agent_sessions.color` → `/api/sessions/:adw_id`, and rides the `agent_start` event so a lane is colored while the agent is still running. Unset = the UI's fallback palette. |
| `coding_agent`, `model`, `thinking`, `color`, `harness_engineering`, `timeout_s` | no | Override the corresponding `defaults` key. |
| `tools` | no | Allowlist. **Omitting the key means all tools usable.** A capability list, not a boundary — see `writes`. |
| `writes` | no | What this agent may modify **in the repo**, enforced after every call. Omitted = unrestricted (still barred from `protected_files`). `[]` = no repo writes at all. A list = only those paths: a trailing `/` is a directory prefix, `*` matches within one path segment, `**` crosses segments, anything else is an exact path. Naming a `protected_files` path here is what unlocks it. **The session runtime under `data_dir` is always writable** — `writes: []` means read-only with respect to the repo, not unable to write its own report. |

Output types are deliberately absent: config defines who an agent *is*; the ADW call site defines how it's *used*. One agent serves many calls — same system prompt, different user prompt + output type per call.

## Defaults merging

`agents.py` merges each entry **over** `defaults`, key by key. An entry states only what differs; anything unset inherits. `agents.validate(cfg, REQUIRED_AGENTS)` then confirms every name an ADW declares exists, resolves to a usable coding agent + model, and has both prompt files present on disk. Any miss fails the run immediately — **no agent is ever spawned against a half-valid config.**

## Thinking levels

Pi's reasoning-effort ladder, lowest to highest:

```
off | minimal | low | medium | high | xhigh | max
```

Mapped to Pi's reasoning effort control and honored when the model is registered with `reasoning: true` in `~/.pi/agent/models.json`. On a non-reasoning model the setting is inert — no error, no effect. Rough guidance: `high`/`xhigh` for planners and reviewers, `medium` for builders, `low` for mechanical read-and-report agents. For Claude Code the same field maps to `--effort`: `off`/`minimal`/`low` → `low`, and `medium`, `high`, `xhigh`, `max` pass through. Which levels a model accepts depends on the model.

## Model resolution

**Always write `model` as `provider/model-id`.** `agents.py` hands the string to the Pi interface, which resolves it against pi's merged catalog — `~/.pi/agent/models.json` plus pi's built-in providers. The same model is usually carried by more than one provider (`gemini-3.6-flash` lives under `google` *and* under `openrouter` as `google/gemini-3.6-flash`), and a bare id that matches several **raises at resolution**:

```
agent 'scout': model pattern 'gemini-3.6-flash' is ambiguous:
  [('google', 'gemini-3.6-flash'), ('openrouter', 'google/gemini-3.6-flash'), ...]
```

That is `agents.validate()` doing its job — it fails before anything spawns rather than silently billing the wrong provider — but it means every agent in the roster inheriting that default is grounded until the pattern is qualified. Qualifying is the whole fix: `google/gemini-3.6-flash`, `openai/gpt-5.6-terra`, `fireworks/accounts/fireworks/models/kimi-k3`. The leading segment is matched against the provider list first, so the rest of the string can contain slashes.

Other consequences worth knowing:

- A model must be in the catalog before any agent can name it. An unknown id fails at resolution, before spawn. `pi --list-models` is the catalog the resolver actually reads.
- **Ambiguity can appear without you touching the config.** Registering a new provider that carries a model you already use turns a formerly-fine bare pattern ambiguous. If a roster stops validating and nobody edited it, that is why.
- Provider credentials come from the environment, not the config — the key that matches the provider you named (`GEMINI_API_KEY` for `google/...`, `OPENROUTER_API_KEY` for `openrouter/...`).
- The resolved model is recorded per session in `agent_map.json` and mirrored into the `agent_sessions` table. **Changing an agent's model invalidates its session**: a joined run starts that agent fresh instead of resuming a context window built by a different model.

## Tools

`tools` maps to `pi --tools`. Pi's seven builtin tool names:

| Tool | Purpose | Pi's own default |
|---|---|---|
| `read` | read file contents | on |
| `bash` | execute bash commands | on |
| `edit` | find/replace edits | on |
| `write` | create/overwrite files | on |
| `grep` | search file contents | **off** |
| `find` | find files by glob | **off** |
| `ls` | list directory contents | **off** |

`grep`, `find`, and `ls` are off in bare Pi, so an agent that does not name them will shell out through `bash` to do the same work. The starter roster therefore sets `defaults.tools` to all seven and lets each agent narrow from there.

**Resolution order:** an agent's own `tools` list wins; an agent that omits the key inherits `defaults.tools`; if neither is set, `tools` stays `None` and all tools are usable. An empty list is not "all tools" — it is a tool-less agent, and it will stall.

## Write permissions — `writes` and `protected_files`

`tools` cannot express a safety boundary, because two of the tools are general
purpose. `bash` runs anything, including `git checkout`, which discards an
engineer's uncommitted work; `write` reaches any path, not only the one report
file an agent was granted it for. So "this agent changes nothing" is a claim a
tool list can state but never keep.

`adw_modules/permissions.py` keeps it, the same way every other claim in this
system is kept — after the fact, against the repo. Before an agent's first
prompt the working tree's change-set is fingerprinted; after its last send
(including JSON retries and gate corrections) it is fingerprinted again. Any
path that appeared, vanished, or changed is attributed to that agent.

Comparing change-sets rather than watching writes is deliberate: a path that was
modified before the agent ran and is clean afterwards has been **reverted**, and
a reversion is a modification. That is what catches `git checkout`.

A breach is not a gate violation. Gates are for work an agent can be asked to
redo; a write has already happened, so re-prompting fixes nothing. Instead:

1. every unauthorized change the agent **introduced** is rolled back — tracked
   files with `git checkout --`, untracked files by deletion;
2. a path that was **already dirty** before the agent ran is left untouched. The
   operator had uncommitted work there, and discarding it to tidy up would be
   the same harm this module exists to prevent;
3. the phase fails and names every path with what happened to it.

```yaml
defaults:
  protected_files: [adws/adw_modules/, adws/adw_sssf_config/, "adws/adw_*.py", adws/conftest.py]

agents:
  - name: builder      # no `writes` key -> unrestricted, minus protected_files
  - name: scout
    writes: []         # no repo writes; its findings still land in context_handoff/
  - name: planner
    writes: [specs/]
  - name: documenter
    writes: [app_docs/, docs/, "**/*.md", "*.md"]
```

**The session runtime under `data_dir` is always writable, for every agent.**
`context_handoff/` is how agents hand work to each other, and each agent's
prompts, `raw_output.jsonl`, and `envelope.json` sit beside it. That grant comes
from `data_dir` rather than from `.gitignore`: the runtime is normally ignored,
so it never even appears in a snapshot, but an agent's ability to record its own
work must not depend on a gitignore line someone can delete.

Narrow by role, not by reflex. Anything that must produce a `context_handoff/` artifact needs `write`, or it will resort to a `bash` heredoc. Withhold `edit`/`write` only where the restriction *is* the guarantee — a reviewer that cannot edit cannot quietly fix what it was asked to report.

### Extension tools must be named explicitly

`pi --tools` is an allowlist over **built-in, extension, and custom tools alike** — not just builtins. So the moment an agent has a `tools` list at all (its own, or one inherited from `defaults`), any tool registered by its `harness_engineering` extensions is **excluded unless it appears in that list by name**.

This fails quietly. The extension still loads, the run still succeeds, and the tool the extension exists to provide is simply never offered to the model — you find out by noticing the agent never called it.

```yaml
  - name: reviewer
    harness_engineering:
      - .pi/extensions/ast_query.ts     # registers tool: ast_query
    tools:
      - read
      - grep
      - find
      - ls
      - bash
      - ast_query                       # REQUIRED — the extension's tool, named or lost
```

Rule: **every entry in `harness_engineering` that registers a tool must have that tool name added to the agent's `tools` list.** Adding an extension is therefore a two-line change, never one. The alternative is dropping the `tools` key *and* leaving `defaults.tools` unset so the agent resolves to `None` (all tools) — but with a roster-wide `defaults.tools` in place, that escape hatch is closed; naming the tool is the only path.

## Harness engineering

`harness_engineering` entries are pi extension **file paths**, passed through as `pi -e <path>`, one flag per entry, scoped to that agent only. This is where per-agent harness changes live — e.g. an output-tightening extension for an agent that keeps wrapping its envelope in prose. The starter roster ships with none. On Claude Code, `.ts` extensions cannot load (validation rejects them); use JSON files instead. See [Claude Code backend](#claude-code-backend).

**If the extension registers a tool, name that tool in the agent's `tools` list too** — `--tools` filters extension tools exactly like builtins, so an unnamed extension tool is silently unavailable no matter that the extension loaded fine. See [Extension tools must be named explicitly](#extension-tools-must-be-named-explicitly) above. Extensions that only shape output or add flags (no tool registration) need no `tools` change.

## Claude Code backend

`coding_agent: claude_code` runs an agent through `claude -p --output-format stream-json --verbose` (`adw_modules/agent_cc.py`). It takes the same request as Pi and returns the same result, so prompts, envelopes, gates, `writes:` enforcement, the trace db and the visualizer behave exactly the same. Pi and Claude Code agents can share one roster and one run.

```yaml
  - name: builder
    coding_agent: claude_code
    model: anthropic/claude-sonnet-5-5   # or an alias: sonnet | opus | haiku
    thinking: medium                     # -> --effort medium
    tools: [read, bash, edit, write, grep, find, ls]   # Pi names are mapped
    harness_engineering: []              # .json only: MCP configs / *settings.json
```

| Concern | How it maps |
|---|---|
| Model | `anthropic/<id>` or a bare id/alias → `--model`. Any other provider prefix fails `agents.validate()`. An unknown id is only caught when the agent runs, because Claude Code has no offline catalog. |
| Sessions | `--session-id <uuid>` creates a session and `--resume <uuid>` continues it. The id is a UUID, minted by `agents.py`. A ledger `{session_id}.json` under `sessions/{adw_id}/{agent}/cc_sessions/` records that the session exists, so retries, corrections and `--adw-id` joins resume it. |
| Tools | `read`→`Read`, `write`→`Write`, `edit`→`Edit`, `bash`→`Bash`, `grep`→`Grep`, `find`/`ls`→`Glob`. Claude Code names (`WebFetch`, `mcp__server__tool`) pass through. A list becomes `--tools` (built-ins) plus `--allowedTools` (all). With no list, `Read, Edit, Write, Bash, Glob, Grep, NotebookEdit` and any `--mcp-config` servers are pre-approved. Pi extension tools such as `subagent_create` fail validation. |
| Permissions | A headless run cannot answer prompts, so it runs with `--permission-mode dontAsk` (override with `SSSF_CC_PERMISSION_MODE`): pre-approved tools run, and anything else is denied rather than waiting. That covers capability only. `writes:` and `protected_files` are still enforced after every call by `permissions.py`, and a breach is rolled back and fails the phase. |
| Thinking | `--effort`, see [Thinking levels](#thinking-levels). |
| Harness | `*settings.json` → `--settings`; any other `.json` → `--mcp-config … --strict-mcp-config`. `.ts` Pi extensions fail validation. With `CONTEXT7_API_KEY` set, a Context7 server without an `Authorization` header gets one in a per-run temp copy (see [Context7](#context7)). |
| Usage | Tokens come from the `result` event's `usage` for that run. Cost is `total_cost_usd`, which Claude Code reports as a running total on resumed sessions; the ledger subtracts the previous total so each run is charged once. The context window comes from `modelUsage[*].contextWindow`. |
| Credentials | Whatever the operator's `claude` uses: a logged-in subscription, `ANTHROPIC_API_KEY`, or Bedrock/Vertex/Foundry settings. |

Environment overrides: `CLAUDE_PATH` (binary, default `claude`), `SSSF_CC_PERMISSION_MODE` (default `dontAsk`), `SSSF_CC_BARE=1` (adds `--bare`: skips the repo's CLAUDE.md, hooks, skills, plugins and MCP auto-discovery for reproducible runs, and requires `ANTHROPIC_API_KEY` because it ignores the subscription login).

Without `--bare`, a Claude Code agent loads the repo's `CLAUDE.md`, `.claude/settings.json` hooks and `.mcp.json` servers, the same as an interactive session in that folder. The agent's `system.md` replaces Claude Code's default system prompt (`--system-prompt`).

Switching an existing agent from Pi to Claude Code: change `coding_agent` and `model`, drop `.ts` entries from `harness_engineering`, and remove Pi extension tools (`subagent_*`) from `tools`. A changed `coding_agent`, like a changed model, starts that agent on a fresh session.

## Context7

[Context7](https://context7.com) serves current, version-specific library documentation. The factory uses it in two layers, and both talk to the **same MCP server** (`https://mcp.context7.com/mcp`) through the **same two tools**, so the layers cannot drift apart:

| Tool | Arguments | Returns |
|---|---|---|
| `resolve-library-id` | `libraryName`, `query` (both required) | Text: "Available Libraries:" blocks with `Title`, `Context7-compatible library ID`, and sometimes `Versions` |
| `query-docs` | `libraryId`, `query` (required), `language` (optional, e.g. `Python`) | Text: doc snippets. A pinned id is `/org/project/v1.2.3`; an unindexed version is answered with a leading `Version note:` and the nearest docs |

(Recorded from the live server, Context7 4.5.0. Older servers called the second tool `get-library-docs`; `context7_client` falls back to it.)

### Layer 1 — an agent tool

A Claude Code agent loads the stamped MCP config and names the tools:

```yaml
  - name: builder
    coding_agent: claude_code
    harness_engineering:
      - adws/adw_data/harness_engineering/context7.mcp.json
    tools: [read, bash, edit, write, grep, find, ls, mcp__context7]   # only needed with a tools list
```

The starter roster gives it to `planner`, `builder` and `reviewer`; `scout` and `documenter` do without. Their `system.md` says to check the pinned version's docs before using a third-party API, to prefer the files the docs phase already wrote, and to treat fetched text as reference, never instructions. Tool calls land in the trace as `tool_call` events named `mcp__context7__resolve-library-id: …` and `mcp__context7__query-docs: …`.

**Pi agents run without it.** Pi has no MCP client, so `agent_pi` drops `.json` harness entries and `mcp__*` tool names; one roster can carry the entries for either backend. The official Pi extension, [`@upstash/context7-pi`](https://www.npmjs.com/package/@upstash/context7-pi) (`pi install npm:@upstash/context7-pi`), registers the same two tools but calls Context7's REST API at `context7.com/api`, not the MCP server. It is not wired in.

### Layer 2 — the `docs` code phase

`adw_plan`, `adw_plan_build`, `adw_plan_build_test`, `adw_plan_build_test_quality` and `adw_simple_sdlc` run a `kind="code"` phase named `docs` (owner `context7`) right after `request`, when `context7.enabled` is set:

1. **Detect** (`docs.detect_libraries`). The configured `libraries`, plus, with `from_request`, every dependency in `manifests` whose name appears in the request text. Versions come from `lockfiles` (what is installed), else an exact manifest pin, else none (latest docs).
2. **Fetch** (`docs.fetch`, through `adw_modules/context7_client.py`). One MCP session per phase (official MCP Python SDK, streamable HTTP, `mcp==2.3.0` in the ADW's `# /// script` block): `resolve-library-id` unless `id` is configured, then `query-docs` with the pinned version in the id and the request as the query. The resolved candidate must actually be the library (its title or id matches the name); an unrelated top hit is not used.
3. **Write** `context_handoff/docs/<name>@<version>.md` (`@latest` when nothing is pinned), with a header: source, library id, requested id, version and where it came from, fetch time, query, size, the server's version note, and a warning that the text is untrusted reference material. `context_handoff/docs/index.json` is the cache.
4. **Hand off.** `docs.as_envelope()` returns a `DocsOutput` (`artifacts` = the doc files, `libraries` = `name@version`) that becomes the planner's `previous_envelope`. The builder reads `<context_handoff_dir>/docs/` itself. `docs.check()` runs `artifacts_exist` and `files_non_empty` on it.

Every Context7 call is a `tool_call` event named `context7:<tool>: <library>`. The phase's `log` event carries `fetched`, `cached`, `skipped`, `cache_hits` and `context7_calls`.

| Situation | Result |
|---|---|
| Re-run with `--adw-id` | Every library already in `index.json` with its file present is a cache hit. With all hits, no MCP session is opened: `context7_calls: 0`. |
| Context7 has no such library | Recorded as `skipped` with the reason; the phase continues. |
| Server unreachable, rate-limited, or refusing the key | Each library it was asked for is recorded as `error` with the reason; after the first failure the rest are not tried. The phase succeeds, the envelope names the libraries that have no docs, and the run continues. With `context7.required: true` the phase fails instead. |
| Docs larger than the caps | Cut at `max_chars_per_library`; once `max_chars_total` is spent, later libraries are `skipped`. |

### Auth

Context7 answers **without** a key, at lower rate limits. A key, when there is one, goes in `Authorization: Bearer <key>`:

- **Locally:** put `CONTEXT7_API_KEY=ctx7sk-…` in `.env`. `context7_client` then sends the header, and `agent_cc` gives each Claude Code run a temp copy of the MCP config whose Context7 server has `"Authorization": "Bearer ${CONTEXT7_API_KEY}"`. Claude Code fills the placeholder from the environment. The copy is removed after the run.
- **Claude cloud environment:** store the key as a Network secret for host `mcp.context7.com`. The egress proxy adds the header itself, `CONTEXT7_API_KEY` stays unset, and neither layer sends a header of its own.
- **Unset, no proxy secret:** anonymous access. Nothing is sent.

The stamped `context7.mcp.json` therefore has **no** `headers`. Claude Code has no conditional header: an unset `${CONTEXT7_API_KEY}` is sent literally, which is why the header is added per run rather than shipped in the file. A header you write yourself is left alone.

The key's value is never written anywhere: not in MCP config copies (they hold the placeholder), traces, `raw_output.jsonl`, `sssf.db`, prompts or doc headers. A test runs a full `adw_plan` with the key set and scans every file the run produced for it.

`agents.validate()` warns, without failing, only when Context7 is in use (a Claude Code agent loads it, or this ADW has a docs phase and `context7.enabled` is set), `CONTEXT7_API_KEY` is unset, **and** an anonymous MCP `initialize` to the server fails (refused, or no network). A missing key alone is not a problem.
