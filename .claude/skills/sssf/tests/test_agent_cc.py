"""Tests for the Claude Code backend and the backend switch in agents.py.

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
FIXTURES = Path(__file__).resolve().parent / "fixtures"
FAKE_CLAUDE = FIXTURES / "fake_claude.py"
sys.path.insert(0, str(SKILL / "templates" / "adws"))

from adw_modules import agent_cc, agent_pi, agents  # noqa: E402
from adw_modules.data_types import AgentConfig, PiRequest, PromptEngineering  # noqa: E402


def _request(**overrides) -> PiRequest:
    fields = dict(prompt="do the thing", system_prompt="you are a builder",
                  model="anthropic/claude-sonnet-5-5", thinking="high",
                  session_id="11111111-2222-3333-4444-555555555555",
                  session_dir="/tmp/cc_sessions", raw_output_path="/tmp/raw.jsonl",
                  tools=["read", "bash", "edit", "write", "grep", "find", "ls"],
                  cwd="/repo")
    fields.update(overrides)
    return PiRequest(**fields)


def _agent(**overrides) -> AgentConfig:
    fields = dict(name="builder", coding_agent="claude_code", model="anthropic/haiku",
                  prompt_engineering=PromptEngineering(system="s.md", user="u.md"))
    fields.update(overrides)
    return AgentConfig(**fields)


def _flag(cmd: list[str], name: str) -> str:
    return cmd[cmd.index(name) + 1]


def _events(name: str) -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines() if line]


# ── config mapping ──────────────────────────────────────────────────────────

def test_resolve_model_accepts_anthropic_and_aliases():
    assert agent_cc.resolve_model("anthropic/claude-opus-5-5") == ("anthropic", "claude-opus-5-5")
    assert agent_cc.resolve_model("sonnet") == ("anthropic", "sonnet")


def test_resolve_model_rejects_other_providers():
    with pytest.raises(ValueError, match="Anthropic models only"):
        agent_cc.resolve_model("openai/gpt-5.6-terra")


def test_validate_agent_flags_pi_only_config(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_cc, "CLAUDE_PATH", str(FAKE_CLAUDE))
    problems = agent_cc.validate_agent(_agent(
        harness_engineering=["adws/adw_data/harness_engineering/subagents.ts"],
        tools=["read", "subagent_create", "WebFetch", "mcp__docs__search"]))
    assert any("subagents.ts" in p and "Pi-only" in p for p in problems)
    assert any("'subagent_create'" in p for p in problems)
    assert not any("WebFetch" in p or "mcp__docs" in p for p in problems)
    assert len(problems) == 2


def test_validate_agent_reports_missing_binary(monkeypatch):
    monkeypatch.setattr(agent_cc, "CLAUDE_PATH", "definitely-not-a-claude-binary")
    assert any("not found" in p for p in agent_cc.validate_agent(_agent()))


def test_first_send_creates_and_later_sends_resume():
    first = agent_cc.build_command(_request(), resume=False)
    later = agent_cc.build_command(_request(), resume=True)
    assert first[:5] == [agent_cc.CLAUDE_PATH, "-p", "--output-format", "stream-json", "--verbose"]
    assert _flag(first, "--session-id") == _request().session_id and "--resume" not in first
    assert _flag(later, "--resume") == _request().session_id and "--session-id" not in later
    assert _flag(first, "--model") == "claude-sonnet-5-5"
    assert _flag(first, "--system-prompt") == "you are a builder"
    assert _flag(first, "--permission-mode") == "dontAsk"
    assert _flag(first, "--effort") == "high"
    assert first[-2:] == ["--", "do the thing"]


def test_pi_tool_names_map_onto_claude_code_tools():
    cmd = agent_cc.build_command(_request(tools=["read", "bash", "find", "ls", "mcp__docs__search"]),
                                 resume=False)
    assert _flag(cmd, "--tools") == "Read,Bash,Glob"
    assert _flag(cmd, "--allowedTools") == "Read,Bash,Glob,mcp__docs__search"


def test_no_tool_list_preapproves_the_default_working_set():
    cmd = agent_cc.build_command(_request(tools=None), resume=False)
    assert "--tools" not in cmd
    assert _flag(cmd, "--allowedTools").split(",") == agent_cc.DEFAULT_ALLOWED_TOOLS


@pytest.mark.parametrize("level,effort", [("off", "low"), ("minimal", "low"),
                                          ("medium", "medium"), ("xhigh", "xhigh"),
                                          ("max", "max")])
def test_thinking_maps_to_effort(level, effort):
    assert _flag(agent_cc.build_command(_request(thinking=level), resume=False),
                 "--effort") == effort


def test_harness_json_routes_to_mcp_config_and_settings(tmp_path):
    mcp = tmp_path / "docs_mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {"docs": {"command": "docs-server"}}}))
    settings = tmp_path / "builder.settings.json"
    settings.write_text("{}")
    cmd = agent_cc.build_command(_request(tools=None, extensions=[str(mcp), str(settings)]),
                                 resume=False)
    assert _flag(cmd, "--settings") == str(settings)
    assert _flag(cmd, "--mcp-config") == str(mcp) and "--strict-mcp-config" in cmd
    assert "mcp__docs" in _flag(cmd, "--allowedTools").split(",")


# ── stream parsing (fixtures are recorded `claude -p` runs) ─────────────────

def test_recorded_first_run_parses_text_usage_and_tool_calls():
    state, tracker, records = agent_cc.StreamState(), agent_cc.ToolCallTracker(), []
    for event in _events("cc_first_run.jsonl"):
        state.observe(event)
        records.extend(tracker.observe(event) or [])
    result, ledger = agent_cc.finalize(state, None)

    assert result.text == '{"ok": true}'
    assert result.tokens == 4 + 185 + 4121 + 4404
    assert result.usage.cache_read_tokens == 4121 and result.usage.cache_write_tokens == 4404
    assert result.cost == pytest.approx(0.00101491)
    assert result.context_window == 1_000_000
    assert result.context_tokens == 2 + 4121 + 283 + 8      # the last main-thread call
    assert ledger["total_cost_usd"] == pytest.approx(0.00101491)

    assert [r["tool"] for r in records] == ["Read", "Bash"]
    assert all(r["ok"] for r in records)
    assert records[1]["label"] == "Bash: ls"
    assert records[0]["result_snippet"].endswith("hello\n2\t")
    assert {"tool_call_id", "args", "started_at", "ended_at", "duration_ms"} <= set(records[0])


def test_resumed_run_is_charged_only_its_own_share():
    state = agent_cc.StreamState()
    for event in _events("cc_resumed_run.jsonl"):
        state.observe(event)
    result, ledger = agent_cc.finalize(state, {"total_cost_usd": 0.00101491})
    assert result.text == "a.txt"
    assert result.cost == pytest.approx(0.00107095 - 0.00101491)
    assert ledger["total_cost_usd"] == pytest.approx(0.00107095)


def test_error_result_raises(tmp_path, monkeypatch):
    script = tmp_path / "claude"
    script.write_text("#!/bin/sh\necho '{\"type\":\"result\",\"subtype\":\"error_during_execution\","
                      "\"is_error\":true,\"result\":\"Not logged in\"}'\n")
    script.chmod(0o755)
    monkeypatch.setattr(agent_cc, "CLAUDE_PATH", str(script))
    with pytest.raises(RuntimeError, match="Not logged in"):
        agent_cc.run(_request(session_dir=str(tmp_path / "s"), cwd=str(tmp_path),
                              raw_output_path=str(tmp_path / "raw.jsonl")))


def test_run_resumes_once_the_session_exists(tmp_path, monkeypatch):
    log = tmp_path / "argv.jsonl"
    monkeypatch.setattr(agent_cc, "CLAUDE_PATH", str(FAKE_CLAUDE))
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_LOG", str(log))
    request = _request(session_dir=str(tmp_path / "s"), cwd=str(tmp_path),
                       raw_output_path=str(tmp_path / "raw.jsonl"))
    monkeypatch.setenv("FAKE_CLAUDE_COST", "0.01")
    first = agent_cc.run(request)
    monkeypatch.setenv("FAKE_CLAUDE_COST", "0.025")
    second = agent_cc.run(request)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert "--session-id" in calls[0] and "--resume" in calls[1]
    assert first.cost == pytest.approx(0.01) and second.cost == pytest.approx(0.015)
    assert first.tokens == second.tokens == 180


# ── backend switch ──────────────────────────────────────────────────────────

def test_backend_selection():
    assert agents.backend(_agent()) is agent_cc
    assert agents.backend(_agent(coding_agent="pi")) is agent_pi


def test_pi_command_line_is_unchanged(tmp_path, monkeypatch):
    captured = {}

    class Stop(Exception):
        pass

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        raise Stop

    monkeypatch.setattr(agent_pi, "resolve_model", lambda pattern: ("openrouter", "google/x"))
    monkeypatch.setattr(agent_pi, "context_window", lambda provider, model: 0)
    monkeypatch.setattr(agent_pi.subprocess, "Popen", fake_popen)
    request = _request(model="openrouter/google/x", thinking="medium", session_id="sssf-a-b-c",
                       tools=["read", "bash"], extensions=["ext.ts"],
                       raw_output_path=str(tmp_path / "raw.jsonl"))
    with pytest.raises(Stop):
        agent_pi.run(request)
    assert captured["cmd"] == [
        agent_pi.PI_PATH, "-p", "--mode", "json",
        "--provider", "openrouter", "--model", "google/x",
        "--thinking", "medium",
        "--session-id", "sssf-a-b-c",
        "--session-dir", "/tmp/cc_sessions",
        "--system-prompt", "you are a builder",
        "--tools", "read,bash", "-e", "ext.ts", "do the thing"]


# ── end to end through a stamped repo, with the fake binary ─────────────────

@pytest.fixture
def stamped_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
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
    full_env = {**os.environ, "CLAUDE_PATH": str(FAKE_CLAUDE), **env}
    return subprocess.run([sys.executable, "adws/adw_prompt.py", *args],
                          cwd=repo, env=full_env, capture_output=True, text=True, timeout=120)


def test_adw_runs_a_claude_code_agent_end_to_end(stamped_repo, tmp_path):
    log = tmp_path / "argv.jsonl"
    first = _adw(stamped_repo, "look around", "--agent", "scout", "--adw-id", "e2e00001",
                 FAKE_CLAUDE_ARGV_LOG=str(log))
    assert first.returncode == 0, first.stdout + first.stderr
    second = _adw(stamped_repo, "look again", "--agent", "scout", "--adw-id", "e2e00001",
                  FAKE_CLAUDE_ARGV_LOG=str(log))
    assert second.returncode == 0, second.stdout + second.stderr

    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert "--session-id" in calls[0] and "--resume" in calls[1]
    assert _flag(calls[0], "--session-id") == _flag(calls[1], "--resume")

    db = sqlite3.connect(stamped_repo / "adws" / "adw_data" / "sssf.db")
    assert db.execute("select status from sessions where adw_id='e2e00001'").fetchone() == ("success",)
    assert db.execute("select coding_agent, model from agent_sessions where agent='scout'"
                      ).fetchone() == ("claude_code", "anthropic/haiku")
    assert db.execute("select count(*) from envelopes where valid=1").fetchone() == (2,)


def test_write_outside_the_boundary_is_rolled_back(stamped_repo):
    target = stamped_repo / "calc.py"
    before = target.read_text()
    result = _adw(stamped_repo, "change nothing", "--agent", "scout",
                  FAKE_CLAUDE_WRITE=str(target))
    assert result.returncode == 1
    assert target.read_text() == before

    db = sqlite3.connect(stamped_repo / "adws" / "adw_data" / "sssf.db")
    assert db.execute("select count(*) from events where name='permission_breach'").fetchone() == (1,)
    assert db.execute("select count(*) from events where type='tool_call' and name like 'Write:%'"
                      ).fetchone() == (1,)
