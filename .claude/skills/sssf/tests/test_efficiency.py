"""Tests for the efficiency scores (adw_modules/efficiency.py) and the prompt hash.

Run from the repo root:
    uv run --with pytest,pydantic,pyyaml,python-dotenv,rich,mcp==2.3.0 pytest .claude/skills/sssf/tests -q
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
FAKE_CLAUDE = SKILL / "tests" / "fixtures" / "fake_claude.py"
sys.path.insert(0, str(SKILL / "templates" / "adws"))

from adw_modules import efficiency, prompts  # noqa: E402
from adw_modules.data_types import (EventRecord, GateCheck, GateReport, Phase,  # noqa: E402
                                    PhaseParams, SSSFConfig)
from adw_modules.runner import Run  # noqa: E402
from adw_modules.tracer import Tracer  # noqa: E402


def _call(tool: str, ok: bool = True, **args) -> dict:
    return {"tool": tool, "args": args, "ok": ok, "label": f"{tool}: {args}",
            "duration_ms": 10, "result_snippet": "" if ok else "boom"}


# ── counting tool calls ─────────────────────────────────────────────────────

def test_a_clean_phase_wastes_nothing():
    measured = efficiency.measure_calls([_call("read", path="a.py"), _call("grep", pattern="x"),
                                         _call("edit", path="a.py")])
    assert measured["wasted_calls"] == 0
    assert measured["calls_before_first_write"] == 2
    assert measured["tool_ms"] == 30
    assert measured["top_waste"] == []


def test_identical_call_is_a_repeat_until_the_tree_changes():
    read = _call("read", path="a.py")
    measured = efficiency.measure_calls([read, read, _call("edit", path="a.py"), read])
    # the second read is a repeat; the read after the edit is looking at new content
    assert measured["repeated_calls"] == 1
    assert measured["wasted_calls"] == 1


def test_a_failed_edit_does_not_excuse_a_repeat():
    read = _call("read", path="a.py")
    measured = efficiency.measure_calls([read, _call("edit", ok=False, path="a.py"), read])
    assert measured["failed_calls"] == 1 and measured["repeated_calls"] == 1
    assert measured["calls_before_first_write"] is None      # nothing was ever written


def test_a_call_that_failed_and_repeated_is_one_wasted_step():
    bad = _call("bash", ok=False, command="curl localhost:3000")
    measured = efficiency.measure_calls([bad, bad, bad])
    assert measured["failed_calls"] == 3 and measured["repeated_calls"] == 2
    assert measured["wasted_calls"] == 3
    # worst offender first, with the error it kept hitting
    assert [(e["kind"], e["count"]) for e in measured["top_waste"]] == [("repeated", 2), ("failed", 1)]
    assert measured["top_waste"][0]["snippet"] == "boom"


def test_tool_names_match_across_backends():
    """Pi says `edit`, Claude Code says `Edit`: both reset the repeat window."""
    read = _call("Read", file_path="a.py")
    measured = efficiency.measure_calls([read, _call("Edit", file_path="a.py"), read])
    assert measured["repeated_calls"] == 0 and measured["calls_before_first_write"] == 1


# ── scoring a phase from trace rows ─────────────────────────────────────────

@pytest.fixture
def tracer(tmp_path):
    return Tracer(tmp_path / "sssf.db", tmp_path / "events.jsonl")


def _phase(tracer: Tracer, adw_id: str = "s0000001", seq: int = 1, kind: str = "agent") -> Phase:
    tracer.session_start(adw_id, "t")
    phase = Phase(phase_id=f"{adw_id}_{seq:02d}_build", adw_id=adw_id, seq=seq,
                  params=PhaseParams(name="build", kind=kind, owner="builder",
                                     description="Implement the plan exactly"),
                  status="success", started_at="2026-01-01T00:00:00.000+00:00",
                  ended_at="2026-01-01T00:00:05.000+00:00")
    tracer.phase_upsert(phase)
    return phase


def _event(tracer: Tracer, phase: Phase, etype: str, name: str = "builder", **payload) -> None:
    tracer.event(EventRecord(adw_id=phase.adw_id, phase_id=phase.phase_id, type=etype,
                             name=name, payload=payload))


def test_phase_score_counts_every_kind_of_waste(tracer):
    phase = _phase(tracer)
    _event(tracer, phase, "agent_start", model="google/x", prompt_hash="abc123abc123")
    for call in (_call("read", path="a.py"), _call("bash", ok=False, command="bun tset"),
                 _call("read", path="a.py"), _call("edit", path="a.py")):
        _event(tracer, phase, "tool_call", name=call.pop("label"), **call)
    tracer.envelope_row(phase, "builder", "BuildOutput", "{}", valid=False, attempt=1)
    tracer.envelope_row(phase, "builder", "BuildOutput", "{}", valid=True, attempt=2)
    failed_gate = GateReport(checks=[GateCheck(item="src/a.py", ok=False, note="missing")])
    tracer.gate_row(phase, "artifacts_exist", failed_gate, attempt=1)
    tracer.gate_row(phase, "files_non_empty", failed_gate, attempt=1)   # same round
    tracer.gate_row(phase, "artifacts_exist", GateReport(checks=[]), attempt=2)
    _event(tracer, phase, "agent_end", cost=0.5, context_tokens=50_000, context_window=200_000,
           usage={"total_tokens": 1000, "input_tokens": 100, "cache_read_tokens": 700,
                  "cache_write_tokens": 200})

    summary = efficiency.score_session(tracer.conn, phase.adw_id)
    # 4 tool calls + 1 send + 1 invalid envelope + 1 failed gate round = 7 steps;
    # wasted: the failed bash, the repeated read, the bad envelope, the gate round
    assert summary == {"adw_id": phase.adw_id, "phases": 1, "steps": 7, "wasted_steps": 4,
                       "efficiency": round(1 - 4 / 7, 4)}
    row = dict(zip(*_row(tracer.conn, phase.phase_id)))
    assert (row["agent"], row["model"], row["prompt_hash"]) == ("builder", "google/x", "abc123abc123")
    assert (row["failed_calls"], row["repeated_calls"]) == (1, 1)
    assert (row["json_retries"], row["gate_fail_rounds"]) == (1, 1)
    assert row["calls_before_first_write"] == 3
    assert (row["tokens"], row["cost"]) == (1000, 0.5)
    assert row["cache_read_share"] == 0.7 and row["context_share"] == 0.25
    assert row["wall_ms"] == 5000 and row["tool_ms"] == 40
    assert json.loads(row["top_waste_json"])[0]["snippet"] == "boom"

    report = efficiency.report(tracer.conn, phase.adw_id)
    assert "4/7 steps wasted" in report and "prompt abc123abc123" in report
    assert "1x failed: bash" in report and "-> boom" in report


def _row(conn: sqlite3.Connection, phase_id: str):
    cursor = conn.execute("SELECT * FROM phase_scores WHERE phase_id=?", (phase_id,))
    return [column[0] for column in cursor.description], cursor.fetchone()


def test_a_dead_phase_is_scored_from_its_spend_log(tracer):
    """No agent_end is written for a phase that died; its spend rides a log event."""
    phase = _phase(tracer)
    _event(tracer, phase, "agent_start", model="google/x")       # a run predating prompt_hash
    _event(tracer, phase, "log", name="agent_spend", cost=0.2, usage={"total_tokens": 400})
    for attempt in (1, 2, 3):
        tracer.envelope_row(phase, "builder", "BuildOutput", "{}", valid=False, attempt=attempt)

    efficiency.score_session(tracer.conn, phase.adw_id)
    row = dict(zip(*_row(tracer.conn, phase.phase_id)))
    assert (row["steps"], row["wasted_steps"], row["efficiency"]) == (4, 3, 0.25)
    assert (row["tokens"], row["cost"], row["prompt_hash"]) == (400, 0.2, None)
    assert row["cache_read_share"] is None and row["context_share"] is None


def test_only_agent_phases_that_started_an_agent_are_scored(tracer):
    code = _phase(tracer, seq=1, kind="code")
    _event(tracer, code, "log", name="test_1", passed=True)
    never_started = _phase(tracer, seq=2)                         # died before any agent ran
    assert efficiency.score_session(tracer.conn, code.adw_id) is None
    assert _row(tracer.conn, never_started.phase_id)[1] is None


def test_rescoring_a_joined_session_replaces_its_rows(tracer):
    phase = _phase(tracer)
    _event(tracer, phase, "agent_start", model="google/x")
    efficiency.score_session(tracer.conn, phase.adw_id)
    bad = _call("bash", ok=False, command="x")
    _event(tracer, phase, "tool_call", name=bad.pop("label"), **bad)
    efficiency.score_session(tracer.conn, phase.adw_id)
    assert tracer.conn.execute("SELECT COUNT(*), MAX(wasted_steps) FROM phase_scores"
                               ).fetchone() == (1, 1)


def test_a_broken_scorer_never_fails_the_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    cfg = SSSFConfig()
    tracer = Tracer(cfg.observability.db, f"{cfg.defaults.data_dir}/sessions/x0000001/events.jsonl")
    tracer.session_start("x0000001", "t")
    run = Run(cfg=cfg, adw_id="x0000001", tracer=tracer, engineer="t")
    with run.phase(PhaseParams(name="request", kind="engineer", owner="t",
                               description="Capture the incoming ask")):
        pass

    def explode(conn, adw_id):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(efficiency, "score_session", explode)
    assert run.finish() == 0
    assert tracer.conn.execute("SELECT status FROM sessions").fetchone() == ("success",)
    assert tracer.conn.execute("SELECT COUNT(*) FROM events WHERE name='efficiency_error'"
                               ).fetchone() == (1,)


# ── the prompt hash ─────────────────────────────────────────────────────────

def test_prompt_fingerprint_tracks_the_templates_not_the_request(tmp_path):
    system, user = tmp_path / "system.md", tmp_path / "user.md"
    system.write_bytes(b"# Builder\n\nBe exact.\n")
    user.write_bytes(b"## prompt\n\n{{prompt}}\n")
    first = prompts.fingerprint(system, user)
    assert len(first) == 12
    assert prompts.fingerprint(system, user) == first             # stable

    system.write_bytes(b"# Builder\r\n\r\nBe exact.\r\n")
    assert prompts.fingerprint(system, user) == first             # a CRLF checkout is the same prompt

    user.write_bytes(b"## prompt\n\n{{prompt}}\n\nBe brief.\n")
    assert prompts.fingerprint(system, user) != first             # an edit is a new version
    assert prompts.fingerprint(user, system) != prompts.fingerprint(system, user)


# ── end to end through a stamped repo, with the fake binary ─────────────────

@pytest.fixture
def stamped_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run([sys.executable, str(SKILL / "scripts" / "install.py")], cwd=repo,
                   check=True, capture_output=True)
    import yaml
    config_path = repo / "adws" / "adw_sssf_config" / "sssf.config.yaml"
    config = yaml.safe_load(config_path.read_text())
    for agent in config["agents"]:
        if agent["name"] == "scout":
            agent.update(coding_agent="claude_code", model="anthropic/haiku",
                         harness_engineering=[],
                         tools=[t for t in agent["tools"] if not t.startswith("subagent_")])
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    subprocess.run(git + ["add", "-A"], cwd=repo, check=True)
    subprocess.run(git + ["commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _adw(repo: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
    full_env = {**os.environ, "CLAUDE_PATH": str(FAKE_CLAUDE),
                "PYTHONWARNDEFAULTENCODING": "1", "PYTHONWARNINGS": "error::EncodingWarning",
                **env}
    return subprocess.run([sys.executable, "adws/adw_prompt.py", *args],
                          cwd=repo, env=full_env, capture_output=True, text=True, timeout=120)


def _scores(repo: Path, adw_id: str) -> list[tuple]:
    db = sqlite3.connect(repo / "adws" / "adw_data" / "sssf.db")
    return db.execute("SELECT prompt_hash, steps, wasted_steps, efficiency, tokens"
                      " FROM phase_scores WHERE adw_id=?", (adw_id,)).fetchall()


def test_every_run_is_scored_and_carries_its_prompt_version(stamped_repo):
    prompt_dir = stamped_repo / "adws" / "adw_data" / "prompt_engineering" / "scout"
    expected = prompts.fingerprint(prompt_dir / "system.md", prompt_dir / "user.md")

    first = _adw(stamped_repo, "look around", "--agent", "scout", "--adw-id", "eff00001")
    assert first.returncode == 0, first.stdout + first.stderr
    assert "efficiency 1.00" in first.stdout
    [(prompt_hash, steps, wasted, score, tokens)] = _scores(stamped_repo, "eff00001")
    assert (prompt_hash, steps, wasted, score) == (expected, 1, 0, 1.0)
    assert tokens and tokens > 0

    # a different request is the same prompt version; an edited template is not
    system = prompt_dir / "system.md"
    system.write_text(system.read_text(encoding="utf-8") + "\n- Be brief.\n", encoding="utf-8")
    second = _adw(stamped_repo, "look elsewhere", "--agent", "scout", "--adw-id", "eff00002")
    assert second.returncode == 0, second.stdout + second.stderr
    [(edited_hash, *_)] = _scores(stamped_repo, "eff00002")
    assert edited_hash == prompts.fingerprint(system, prompt_dir / "user.md") != expected


def test_a_failed_run_is_scored_with_what_it_spent(stamped_repo):
    result = _adw(stamped_repo, "look around", "--agent", "scout", "--adw-id", "eff00003",
                  FAKE_CLAUDE_BAD_JSON="1")
    assert result.returncode == 1
    # one send plus two correction sends, every answer unparseable: 3 invalid envelopes
    [(_, steps, wasted, score, tokens)] = _scores(stamped_repo, "eff00003")
    assert (steps, wasted, score) == (4, 3, 0.25)
    assert tokens and tokens > 0                    # spend survives the failure

    script = stamped_repo / "adws" / "adw_modules" / "efficiency.py"
    shown = subprocess.run([sys.executable, str(script), "eff00003"], cwd=stamped_repo,
                           capture_output=True, text=True)
    assert shown.returncode == 0, shown.stderr
    assert "3/4 steps wasted" in shown.stdout and "invalid envelopes 3" in shown.stdout

    everything = subprocess.run([sys.executable, str(script), "--all"], cwd=stamped_repo,
                                capture_output=True, text=True)
    assert everything.returncode == 0 and "scored 1 of 1 run(s)" in everything.stdout
