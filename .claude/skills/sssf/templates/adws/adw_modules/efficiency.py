# /// script
# dependencies = []
# ///
"""Efficiency scores: how much of an agent phase's effort was wasted.

Counted from the trace, in code. No model reads a transcript to produce these:
every signal is already a row in sssf.db, so scoring is free and runs on every
run, including the failed ones.

One score per agent phase, in `phase_scores`:

    steps      = tool calls + sends (the first prompt, plus every re-prompt)
    wasted     = failed tool calls + repeated tool calls
                 + invalid-envelope retries + failed gate rounds
    efficiency = 1 - wasted / steps

A repeated call is one with the same tool and the same arguments as an earlier
call in the phase, with no successful edit or write in between. Re-reading a
file after changing it, or re-running the suite after a fix, is not a repeat.

These are proxies, not verdicts. A failing test run the builder needed to see
counts as a failed call. So compare a score with the same agent's own history
(`just scores-by-prompt`), never with a fixed pass mark, and read `top_waste`
before deciding that a low score is a problem.

Stdlib only, so it also runs as a script against an existing trace db:

    uv run adws/adw_modules/efficiency.py <adw_id>      # score one run, print the report
    uv run adws/adw_modules/efficiency.py --all         # score every run in the db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS phase_scores (
  phase_id        TEXT PRIMARY KEY,
  adw_id          TEXT REFERENCES sessions,
  agent           TEXT,
  model           TEXT,
  prompt_hash     TEXT,               -- which system.md + user.md this phase ran on
  steps           INTEGER,            -- tool calls + sends
  wasted_steps    INTEGER,
  efficiency      REAL,               -- 1 - wasted_steps / steps
  tool_calls      INTEGER,
  failed_calls    INTEGER,
  repeated_calls  INTEGER,
  json_retries    INTEGER,            -- envelopes that did not parse
  gate_fail_rounds INTEGER,           -- gate attempts with at least one failed gate
  calls_before_first_write INTEGER,   -- NULL = the phase never edited or wrote
  tokens          INTEGER,            -- NULL = spend was not recorded
  cost            REAL,
  cache_read_share REAL,              -- cached share of the prompt tokens sent
  context_share   REAL,               -- window occupancy when the agent stopped
  wall_ms         INTEGER,
  tool_ms         INTEGER,
  top_waste_json  TEXT,               -- [{kind, label, count, snippet}], worst first
  scored_at       TEXT
);
"""

MUTATING_TOOLS = {"edit", "write", "multiedit", "notebookedit"}
TOP_WASTE = 5
SNIPPET_CHARS = 200


def _json(text) -> dict:
    try:
        value = json.loads(text or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _ms_between(start, end):
    try:
        delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    except (TypeError, ValueError):
        return None
    return int(delta.total_seconds() * 1000)


def _share(part, whole):
    return round(part / whole, 4) if whole else None


def measure_calls(calls: list[dict]) -> dict:
    """Fold a phase's tool calls, in order, into counts and the worst offenders.

    `calls` are tool_call payloads with their event `label` added.
    """
    seen: set[str] = set()
    failed = repeated = wasted = tool_ms = 0
    before_first_write = None
    offenders: dict[tuple[str, str], dict] = {}
    for index, call in enumerate(calls):
        tool = str(call.get("tool") or "").lower()
        ok = call.get("ok", True) is not False
        key = tool + " " + json.dumps(call.get("args") or {}, sort_keys=True, default=str)
        is_repeat = key in seen
        tool_ms += int(call.get("duration_ms") or 0)
        if tool in MUTATING_TOOLS and ok:
            if before_first_write is None:
                before_first_write = index
            seen.clear()            # the tree changed: looking again is not a repeat
        else:
            seen.add(key)
        failed += not ok
        repeated += is_repeat
        if ok and not is_repeat:
            continue
        wasted += 1                 # a call that failed AND repeated is one wasted step
        kind = "repeated" if is_repeat else "failed"
        entry = offenders.setdefault((kind, str(call.get("label") or tool)),
                                     {"kind": kind, "label": str(call.get("label") or tool),
                                      "count": 0, "snippet": ""})
        entry["count"] += 1
        if not ok and call.get("result_snippet"):
            entry["snippet"] = str(call["result_snippet"])[:SNIPPET_CHARS]
    top = sorted(offenders.values(), key=lambda entry: -entry["count"])[:TOP_WASTE]
    return {"tool_calls": len(calls), "failed_calls": failed, "repeated_calls": repeated,
            "wasted_calls": wasted, "calls_before_first_write": before_first_write,
            "tool_ms": tool_ms, "top_waste": top}


def score_phase(conn: sqlite3.Connection, phase_id: str) -> dict | None:
    """Score one agent phase from its trace rows. None if no agent ever started in it."""
    phase = conn.execute("SELECT adw_id, owner, started_at, ended_at FROM phases"
                         " WHERE phase_id=?", (phase_id,)).fetchone()
    if not phase:
        return None
    adw_id, owner, started_at, ended_at = phase
    start, calls, usage_rows, spend_rows = None, [], [], []
    for etype, name, payload_json in conn.execute(
            "SELECT type, name, payload_json FROM events WHERE phase_id=? ORDER BY rowid",
            (phase_id,)):
        payload = _json(payload_json)
        if etype == "agent_start":
            start = payload
        elif etype == "tool_call":
            calls.append({**payload, "label": name})
        elif etype == "agent_end":
            usage_rows.append(payload)
        elif etype == "log" and name == "agent_spend":
            spend_rows.append(payload)     # what a phase that died had already spent
    if start is None:
        return None

    measured = measure_calls(calls)
    json_retries = conn.execute("SELECT COUNT(*) FROM envelopes WHERE phase_id=? AND valid=0",
                                (phase_id,)).fetchone()[0]
    gate_fail_rounds = conn.execute("SELECT COUNT(DISTINCT attempt) FROM gate_results"
                                    " WHERE phase_id=? AND passed=0", (phase_id,)).fetchone()[0]
    steps = measured["tool_calls"] + 1 + json_retries + gate_fail_rounds
    wasted = measured["wasted_calls"] + json_retries + gate_fail_rounds

    spend = usage_rows or spend_rows
    usage = [row.get("usage") or {} for row in spend]
    tokens = sum(int(u.get("total_tokens") or 0) for u in usage) if spend else None
    cost = sum(float(row.get("cost") or 0.0) for row in spend) if spend else None
    cache_read = sum(int(u.get("cache_read_tokens") or 0) for u in usage)
    prompt_tokens = cache_read + sum(int(u.get("input_tokens") or 0)
                                     + int(u.get("cache_write_tokens") or 0) for u in usage)
    last = usage_rows[-1] if usage_rows else {}

    return {
        "phase_id": phase_id, "adw_id": adw_id, "agent": owner,
        "model": start.get("model"), "prompt_hash": start.get("prompt_hash"),
        "steps": steps, "wasted_steps": wasted,
        "efficiency": round(1 - wasted / steps, 4),
        "tool_calls": measured["tool_calls"], "failed_calls": measured["failed_calls"],
        "repeated_calls": measured["repeated_calls"],
        "json_retries": json_retries, "gate_fail_rounds": gate_fail_rounds,
        "calls_before_first_write": measured["calls_before_first_write"],
        "tokens": tokens, "cost": cost,
        "cache_read_share": _share(cache_read, prompt_tokens),
        "context_share": _share(int(last.get("context_tokens") or 0),
                                int(last.get("context_window") or 0)),
        "wall_ms": _ms_between(started_at, ended_at), "tool_ms": measured["tool_ms"],
        "top_waste_json": json.dumps(measured["top_waste"]),
        "scored_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    }


def score_session(conn: sqlite3.Connection, adw_id: str) -> dict | None:
    """Score every agent phase of a run and store the rows. Safe to call again:
    a session joined by a later ADW is simply rescored.

    Returns the run's pooled totals, or None when it had no agent phase to score.
    """
    conn.executescript(SCHEMA)
    steps = wasted = phases = 0
    for (phase_id,) in conn.execute("SELECT phase_id FROM phases WHERE adw_id=?"
                                    " AND kind='agent' ORDER BY seq", (adw_id,)).fetchall():
        row = score_phase(conn, phase_id)
        if row is None:
            continue
        columns = ", ".join(row)
        conn.execute(f"INSERT OR REPLACE INTO phase_scores ({columns})"
                     f" VALUES ({', '.join('?' for _ in row)})", tuple(row.values()))
        steps += row["steps"]
        wasted += row["wasted_steps"]
        phases += 1
    if not phases:
        return None
    return {"adw_id": adw_id, "phases": phases, "steps": steps, "wasted_steps": wasted,
            "efficiency": round(1 - wasted / steps, 4)}


def report(conn: sqlite3.Connection, adw_id: str) -> str:
    """The stored scores of one run, per phase, with what the waste actually was."""
    rows = conn.execute(
        "SELECT p.seq, p.name, s.agent, s.prompt_hash, s.efficiency, s.wasted_steps, s.steps,"
        " s.failed_calls, s.repeated_calls, s.json_retries, s.gate_fail_rounds,"
        " s.calls_before_first_write, s.tokens, s.cost, s.top_waste_json"
        " FROM phase_scores s JOIN phases p ON p.phase_id = s.phase_id"
        " WHERE s.adw_id=? ORDER BY p.seq", (adw_id,)).fetchall()
    if not rows:
        return f"{adw_id}: no scored agent phases"
    lines = []
    for (seq, name, agent, prompt_hash, efficiency, wasted, steps, failed, repeated,
         json_retries, gate_rounds, before_write, tokens, cost, top_json) in rows:
        spend = f"{tokens:,} tokens, ${cost:.4f}" if tokens is not None else "spend not recorded"
        lines.append(f"{seq:>2} {name} ({agent}, prompt {prompt_hash or 'unknown'})  "
                     f"efficiency {efficiency:.2f}  {wasted}/{steps} steps wasted  {spend}")
        lines.append(f"     failed calls {failed}, repeated calls {repeated}, "
                     f"invalid envelopes {json_retries}, failed gate rounds {gate_rounds}, "
                     f"calls before first write "
                     f"{'none' if before_write is None else before_write}")
        try:
            top = json.loads(top_json or "[]")
        except ValueError:
            top = []
        for entry in top:
            snippet = " ".join(str(entry.get("snippet") or "").split())
            lines.append(f"     {entry['count']}x {entry['kind']}: {entry['label']}"
                         + (f"  -> {snippet}" if snippet else ""))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("adw_id", nargs="?", help="the run to score and report")
    parser.add_argument("--all", action="store_true", help="score every run in the db")
    parser.add_argument("--db", default="adws/adw_data/sssf.db")
    args = parser.parse_args()
    if not args.adw_id and not args.all:
        parser.error("give an adw_id, or --all")
    try:
        conn = sqlite3.connect(f"file:{args.db}?mode=rw", uri=True, isolation_level=None)
    except sqlite3.OperationalError:
        print(f"no trace db at {args.db}", file=sys.stderr)
        return 1
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    if args.all:
        ids = [row[0] for row in conn.execute("SELECT adw_id FROM sessions ORDER BY started_at")]
        scored = [summary for summary in (score_session(conn, adw_id) for adw_id in ids) if summary]
        print(f"scored {len(scored)} of {len(ids)} run(s)")
        return 0
    if score_session(conn, args.adw_id) is None:
        print(f"{args.adw_id}: no agent phases to score", file=sys.stderr)
        return 1
    print(report(conn, args.adw_id))
    return 0


if __name__ == "__main__":
    sys.exit(main())
