# Observability Reference

The event schema, the eight SQLite tables, the efficiency scores, and the polling contract — the one data path is **agents → sqlite → web ui**.

## Two stores, one truth

**Files are the raw record** (`raw_output.jsonl` streams, `envelope.json`, `agent_map.json`); **SQLite (`sssf.db`) is the queryable mirror** the UI reads. `tracer.py` writes both. Losing the db loses nothing that can't be rebuilt from files.

Location comes from `observability.db` in `sssf.config.yaml`, default `adws/adw_data/sssf.db` — inside the **target** repo, gitignored.

## Event schema

`tracer.py` emits these types, every one logged against its `adw_id` **and** `phase_id`:

| Type | Emitted when |
|---|---|
| `phase_start` | a `run.phase(...)` block is entered |
| `agent_start` | a coding agent is spawned or resumed for `ph.call(...)` — payload carries the agent's `model`, `thinking`, `tools`, and `prompt_hash` (its prompt version, see [Efficiency scores](#efficiency-scores)) |
| `tool_call` | a tool (`read`, `bash`, `edit`, `write`) returns — **one event per real call**, named `bash: ls -la src`, payload `{tool, tool_call_id, args, result_snippet, ok, duration_ms, agent}` |
| `handoff` | an envelope crosses from one agent to the next |
| `gate_pass` | a gate found no failed checks — payload carries `attempt`, `checks` (the evidence), and an empty `violations` |
| `gate_fail` | a gate found at least one failed check — payload carries `attempt`, `checks`, and `violations` |
| `log` | an explicit `ph.log(...)` from the ADW script |
| `agent_end` | the agent's run completes; envelope parsed or not — payload carries `cost`, `usage` (the per-component breakdown), `context_tokens`, `context_window` |
| `phase_end` | the block exits; carries the resolved status |
| `error` | a raise inside a phase block |

`parent_id` nests spans, so an agent phase expands into its tool-call spans in the UI.

**Spend is itemised per phase.** `agent_end.usage` carries tokens *and* dollars for each component pi reports — `input`, `output`, `cache_read`, `cache_write` — summed across every send the phase made, so a phase that retried on a bad envelope or a failed gate shows what all its attempts cost, not just the last one. The four components sum to `total_tokens`, and their costs sum to `total_cost`; the visualizer's Cost panel renders them as a table you can add up by eye.

`reasoning_tokens` is the thinking share and is **inside** `output_tokens`, not a fifth component — measured across every session on disk, reasoning never exceeds output and the four components always reconcile to the total. It bills at the output rate, so the panel nests it under output rather than adding it. Runs predating the breakdown have no `usage` key at all; the lump `cost` and the event's own `tokens` still stand, and the UI says so rather than rendering zeroes.

**Context is occupancy, not spend.** `events.tokens` and `sessions.total_tokens` bill every turn, so they only grow — an agent that burned 100k tokens may be sitting in a 15k window. `context_tokens` is how full the window actually was when the agent stopped, which is what the visualizer's per-lane Context bar measures against `context_window`.

It is computed the way pi computes it for its own footer and its auto-compaction trigger (`calculateContextTokens` in the coding agent's `core/compaction/compaction.ts`): take the last *valid* assistant turn — skipping `aborted` and `error` turns — and read `usage.totalTokens`, falling back to `input + output + cacheRead + cacheWrite`. Cache reads count; cached prompt is still prompt. `context_window` is the same `contextWindow` pi reads from `~/.pi/agent/models.json`, so `context_tokens / context_window` is the number pi would show. Both are NULL on rows written before the columns existed, and the lane draws no bar rather than a misleading empty one.

Two caveats worth knowing. Pi adds an *estimate* for any messages trailing the last assistant usage; in a batch (`-p`) run the session ends on that message, so the two agree. And if auto-compaction fires as the very last act of a run, the recorded number is the pre-compaction size — pi itself reports `null` in that window rather than guessing.

**Gates record evidence, not just a verdict.** A gate returns one `{item, ok, note}` check per thing it looked at, and `violations` are derived from the failed ones. Both land in `gate_results` (`checks_json` + `violations_json`) and in the `gate_pass`/`gate_fail` payload, so a green gate can answer *what did you verify* — `{"item": "…/plan.md", "ok": true, "note": "exists, 454B"}` — rather than only *did it pass*. Rows written before this existed have `checks_json` NULL; treat that as "no evidence recorded", not "nothing checked".

The gate event payload carries `attempt` too, so the `gate_results` table and the event stream are equivalent sources — a live consumer can group gate results per correction round from events alone, without a second query.

**A `tool_call` is the one event that spans time**, so it fills both `started_at` and `ended_at` on the row — the tool's real start and return. Every other type is a point in time: `started_at` is when it was recorded and `ended_at` stays NULL. Lay tool calls out on a time axis from those columns, never by parsing `payload_json` (`duration_ms` is in the payload too, as pi's own number, but it is a convenience, not the source for layout).

**Streaming is solved by construction.** `agent_pi.py` tails pi's JSONL stdout line by line and the tracer inserts each event into `sssf.db` **while the agent is still working** — never batched at phase end (verified in the first smoke run: tool calls visible mid-run). Everything downstream is a poll → render.

## Tables

```sql
sessions (
  adw_id        TEXT PRIMARY KEY,
  request       TEXT,              -- the engineer's ask
  status        TEXT,              -- running | success | fail
  engineer      TEXT,
  started_at    TEXT, ended_at TEXT,
  total_tokens  INTEGER, total_cost REAL
);

phases (
  phase_id      TEXT PRIMARY KEY,
  adw_id        TEXT REFERENCES sessions,
  seq           INTEGER,
  name TEXT, kind TEXT, owner TEXT, description TEXT,
  status        TEXT DEFAULT 'fail',   -- success must be earned
  attempt       INTEGER DEFAULT 0, retries INTEGER DEFAULT 0,
  error         TEXT,
  started_at    TEXT, ended_at TEXT
);

events (
  event_id      TEXT PRIMARY KEY,
  adw_id        TEXT REFERENCES sessions,
  phase_id      TEXT REFERENCES phases,   -- every event logs against adw + phase
  parent_id     TEXT,                     -- span nesting
  type          TEXT,   -- phase_start | phase_end | agent_start | agent_end | tool_call
                        -- | handoff | gate_pass | gate_fail | log | error
  name          TEXT,
  payload_json  TEXT,
  tokens        INTEGER,
  started_at    TEXT, ended_at TEXT   -- ended_at set only on events that span time
);

envelopes (
  envelope_id   TEXT PRIMARY KEY,
  adw_id        TEXT REFERENCES sessions,
  phase_id      TEXT REFERENCES phases,
  agent         TEXT,
  output_type   TEXT,              -- name of the data_types model it parsed against
  payload_json  TEXT,
  valid         INTEGER,
  attempt       INTEGER,
  created_at    TEXT
);

gate_results (
  id            INTEGER PRIMARY KEY,
  adw_id        TEXT REFERENCES sessions,
  phase_id      TEXT REFERENCES phases,
  attempt       INTEGER,
  gate          TEXT,
  passed        INTEGER,
  violations_json TEXT,             -- derived: the failed checks, as "item: note"
  checks_json   TEXT,               -- [{item, ok, note}] — everything the gate looked at
  created_at    TEXT
);

processes (                        -- adw_id → pid, so a stuck run can be stopped
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  adw_id        TEXT REFERENCES sessions,
  kind          TEXT,               -- 'adw' (the workflow process) | 'agent' (a coding-agent child)
  name          TEXT,               -- '' for the adw, the agent name for a child
  pid           INTEGER,
  command       TEXT,               -- what the pid WAS; pids get recycled, so verify before killing
  started_at    TEXT, ended_at TEXT -- ended_at NULL = believed alive
);

agent_sessions (                   -- the queryable mirror of agent_map.json
  adw_id        TEXT REFERENCES sessions,
  agent         TEXT,
  coding_agent  TEXT, model TEXT, color TEXT,   -- color: the config's lane swatch
  session_id    TEXT,
  context_tokens INTEGER,           -- window occupancy after the agent's last turn
  context_window INTEGER,           -- the model's ceiling, from the pi registry
  created_at    TEXT, last_used_at TEXT,
  PRIMARY KEY (adw_id, agent)
);
```

**A hung agent emits nothing**, which is exactly when you need its pid: no events, no tokens, no output to read. `processes` is the only table that can answer "what is this run running, and how do I stop it" — `just procs <adw_id>` lists what is live, `just kill <adw_id>` stops children before the parent, and both verify the recorded `command` still matches the pid before signalling it. A killed run finalizes its own trace: SIGTERM and SIGINT are turned into `SystemExit` in `session.ensure`, so the session lands on `fail` with its process rows closed instead of reading `running` forever.

`phase_scores` is the eighth table, one row per scored agent phase. Its columns are listed in [Efficiency scores](#efficiency-scores).

**Derived, never stored:** phase durations (`ended_at − started_at`), session phase-progress (query `phases` by `adw_id`), lane layout (`kind` + `owner`).

Phase status invariants: `queued` only for manifest-declared phases not yet entered (dashed in the UI); `running` on enter; only a clean exit writes `success` — agent phases additionally need the envelope parsed and gates green; everything else resolves to `fail`.

## Efficiency scores

When a run ends, green or red, `Run.score()` counts how much of each agent phase's effort was wasted and writes one row per agent phase to `phase_scores`. The counting is code over rows the tracer already wrote (`adw_modules/efficiency.py`, stdlib only), so it costs no tokens and no model reads a transcript.

```
steps      = tool calls + sends (the first prompt, plus every re-prompt)
wasted     = failed tool calls + repeated tool calls
             + invalid-envelope retries + failed gate rounds
efficiency = 1 - wasted / steps
```

- **Failed call**: a `tool_call` event whose payload has `ok: false`.
- **Repeated call**: same tool and same arguments as an earlier call in the phase, with no successful `edit`/`write` in between. Re-reading a file after changing it, or re-running the suite after a fix, is not a repeat. A call that both failed and repeated is one wasted step.
- **Invalid-envelope retry**: an `envelopes` row with `valid = 0`.
- **Failed gate round**: a distinct `attempt` in `gate_results` with at least one `passed = 0`.

A run's score is the same ratio pooled over its agent phases; `just scores` computes it in SQL, it is not stored.

```sql
phase_scores (
  phase_id        TEXT PRIMARY KEY,
  adw_id          TEXT REFERENCES sessions,
  agent TEXT, model TEXT,
  prompt_hash     TEXT,              -- which system.md + user.md the phase ran on
  steps INTEGER, wasted_steps INTEGER,
  efficiency      REAL,              -- 1 - wasted_steps / steps
  tool_calls INTEGER, failed_calls INTEGER, repeated_calls INTEGER,
  json_retries INTEGER, gate_fail_rounds INTEGER,
  calls_before_first_write INTEGER,  -- NULL = the phase never edited or wrote
  tokens INTEGER, cost REAL,         -- NULL = spend was not recorded
  cache_read_share REAL,             -- cached share of the prompt tokens sent
  context_share   REAL,              -- window occupancy when the agent stopped
  wall_ms INTEGER, tool_ms INTEGER,
  top_waste_json  TEXT,              -- [{kind, label, count, snippet}], worst first
  scored_at       TEXT
);
```

**What is in the score and what is only recorded.** Only the four waste counts move `efficiency`. `calls_before_first_write`, `cache_read_share`, `context_share`, `tokens`, `cost` and the two durations are stored beside it for you to read, and are deliberately left out: how much reading an agent should do before it writes depends on the task, and no threshold is right for every repo.

**The score is a proxy, not a verdict.** A red test run the builder needed to see is a failed call. So there is no pass mark. Compare an agent with its own history, and read `top_waste_json` (or `just score <adw_id>`) before treating a low score as a problem: it names the calls that failed or repeated, with the error text the agent kept getting.

**Prompt version.** `prompt_hash` is the first 12 hex characters of a SHA-256 over the agent's `system.md` and `user.md` templates, taken before rendering. It changes when someone edits a prompt and never because the request did; line endings are normalized, so a CRLF checkout is the same version. It rides the `agent_start` payload and is copied into `phase_scores`, which is what makes "did that prompt edit help" a query: `just scores-by-prompt` groups efficiency by agent and prompt version. The model is on the same row, so a model change is visible the same way.

**Failed phases are scored too.** `agent_end` is only written for a phase that finished, so a phase that dies writes a `log` event named `agent_spend` with the `cost` and `usage` it had accumulated, and the scorer reads spend from there. Runs that predate this have `tokens` and `cost` NULL on their failed phases.

**Scoring never changes a run's outcome.** If the scorer raises, the error is recorded as a `log` event named `efficiency_error` and the run's status and exit code stand.

**Rescoring.** `score_session` replaces a run's rows, so a session joined by a later ADW (`--adw-id`) is rescored as a whole. `just rescore` scores every run in the db, which is how runs from before this table existed get scores. Their `prompt_hash` is NULL, shown as `unknown`.

| Recipe | Shows |
|---|---|
| `just scores` | the last 10 scored runs: status, efficiency, wasted/total steps, failed and repeated calls, cost |
| `just score <adw_id>` | one run phase by phase, with the worst offending calls |
| `just scores-by-prompt` | efficiency and average cost per agent and prompt version |
| `just rescore` | (re)scores every run in the db |

## WAL pragmas

Open **every** connection — writer and reader — with:

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
```

WAL allows readers during writes. Writers are the tracers of running ADW processes; concurrent writers are fine given one small transaction per event plus `busy_timeout`. The visualizer reads on a readonly connection with exactly one exception: archiving a session (`POST /api/sessions/:adw_id/archive`) opens a second connection to set `sessions.archived`. That flag is review triage — it says a human has looked at the run — so it is the reader's state living on the row, and no tracer ever writes or reads it.

## Polling contract

**The UI never receives pushes.** No ingest endpoint, no WebSocket, no backfill or dedup logic.

Live view polls on a rowid cursor every `observability.poll_ms` (default 500):

```sql
SELECT ... FROM events WHERE adw_id = ? AND rowid > ? ORDER BY rowid LIMIT 500;
```

Keep the highest `rowid` returned as the next cursor. History is **the same queries** with filters, lazy-paged as the engineer scrolls or drills in — one mechanism serves both live and past runs, which is why there is no separate replay path.
