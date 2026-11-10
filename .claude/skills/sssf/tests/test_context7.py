"""Tests for the Context7 integration: Layer 1 (the MCP config an agent loads)
and Layer 2 (the docs code phase through context7_client).

No test touches the real Context7: the MCP tests run against a local server
built from the MCP SDK's own classes (fixtures/fake_context7.py).

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
from types import SimpleNamespace

import pytest

SKILL = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
FAKE_CLAUDE = FIXTURES / "fake_claude.py"
CONTEXT7_MCP_JSON = SKILL / "templates" / "harness_engineering" / "context7.mcp.json"
sys.path.insert(0, str(SKILL / "templates" / "adws"))
sys.path.insert(0, str(FIXTURES))

from adw_modules import agent_cc, agent_pi, agents, context7_client, docs  # noqa: E402
from adw_modules.data_types import (AgentConfig, Context7Config, PiRequest,  # noqa: E402
                                    PromptEngineering, SSSFConfig)

SECRET = "ctx7sk-test-0123456789-do-not-leak"

PYPROJECT = """\
[project]
name = "demo"
version = "0.1.0"
dependencies = ["fastapi>=0.110", "pydantic==2.11.7", "rich"]

[dependency-groups]
dev = ["pytest>=8"]
"""
UV_LOCK = """\
version = 1

[[package]]
name = "fastapi"
version = "0.115.6"

[[package]]
name = "pydantic"
version = "2.11.7"

[[package]]
name = "rich"
version = "13.9.4"
"""


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    """Every test starts like the cloud: no CONTEXT7_API_KEY in the environment."""
    monkeypatch.delenv("CONTEXT7_API_KEY", raising=False)
    context7_client._PROBES.clear()


@pytest.fixture
def fake_context7():
    pytest.importorskip("mcp")
    from fake_context7 import FakeContext7
    server = FakeContext7().start()
    yield server
    server.stop()


class _Tracer:
    def __init__(self):
        self.events = []
        self.gates = []

    def event(self, record):
        self.events.append(record)

    def gate_row(self, phase, gate, report, attempt):
        self.gates.append((gate, report.passed))


def _run(tmp_path: Path, **context7) -> SimpleNamespace:
    handoff = tmp_path / "handoff"
    handoff.mkdir(exist_ok=True)
    cfg = SSSFConfig(context7=Context7Config(enabled=True, **context7))
    return SimpleNamespace(cfg=cfg, repo_root=tmp_path, context_handoff_dir=handoff,
                           adw_id="t", phases=[SimpleNamespace(phase_id="t_02_docs")],
                           tracer=_Tracer(),
                           console=SimpleNamespace(gate_result=lambda *a: None))


def _python_repo(root: Path) -> Path:
    (root / "pyproject.toml").write_text(PYPROJECT)
    (root / "uv.lock").write_text(UV_LOCK)
    return root


# ── Layer 2: library detection ──────────────────────────────────────────────

def test_detects_python_deps_pinned_by_uv_lock(tmp_path):
    deps = docs.read_dependencies(_python_repo(tmp_path), Context7Config())
    assert {d.name: (d.version, d.source) for d in deps.values()} == {
        "fastapi": ("0.115.6", "uv.lock"), "pydantic": ("2.11.7", "uv.lock"),
        "rich": ("13.9.4", "uv.lock"), "pytest": (None, "")}


def test_detects_npm_deps_from_package_lock_then_exact_manifest_pins(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "dependencies": {"next": "^15.1.0", "zod": "3.23.8"},
        "devDependencies": {"@tanstack/react-query": "~5.0.0"}}))
    (tmp_path / "package-lock.json").write_text(json.dumps({"packages": {
        "": {}, "node_modules/next": {"version": "15.1.11"},
        "node_modules/@tanstack/react-query": {"version": "5.0.5"}}}))
    deps = {d.name: (d.version, d.source) for d in
            docs.read_dependencies(tmp_path, Context7Config()).values()}
    assert deps == {"next": ("15.1.11", "package-lock.json"), "zod": ("3.23.8", "package.json"),
                    "@tanstack/react-query": ("5.0.5", "package-lock.json")}


def test_requirements_and_poetry_lock(tmp_path):
    (tmp_path / "requirements.txt").write_text("# app\nDjango==5.1.2\nrequests>=2\n-e .\n")
    (tmp_path / "poetry.lock").write_text('[[package]]\nname = "requests"\nversion = "2.32.3"\n')
    deps = {d.name: d.version for d in docs.read_dependencies(tmp_path, Context7Config()).values()}
    assert deps == {"Django": "5.1.2", "requests": "2.32.3"}


def test_request_text_selects_libraries_by_word_match(tmp_path):
    run = _run(_python_repo(tmp_path))
    chosen = docs.detect_libraries(run, run.cfg.context7,
                                   "Add a FastAPI endpoint that validates input with Pydantic.")
    assert [(lib.name, lib.version) for lib in chosen] == [("fastapi", "0.115.6"),
                                                           ("pydantic", "2.11.7")]
    assert docs.named_in("use fastapi.", "fastapi")
    assert not docs.named_in("use fastapi-users", "fastapi")
    assert not docs.named_in("make it richer", "rich")


def test_configured_libraries_always_come_first(tmp_path):
    run = _run(_python_repo(tmp_path), libraries=[{"name": "pydantic", "id": "/pydantic/pydantic"},
                                                  "htmx"], from_request=False)
    chosen = docs.detect_libraries(run, run.cfg.context7, "mention fastapi")
    assert [(l.name, l.version, l.library_id, l.requested_by) for l in chosen] == [
        ("pydantic", "2.11.7", "/pydantic/pydantic", "config"), ("htmx", None, "", "config")]


# ── Layer 2: fetch and capture against a stubbed client ────────────────────

class StubSession:
    """What context7_client.Session looks like to docs.py."""

    def __init__(self, texts: dict | None = None, fail: Exception | None = None):
        self.texts = texts or {}
        self.fail = fail
        self.calls = 0
        self.tools = {"resolve-library-id": {}, "query-docs": {}}
        self.closed = False
        self.log = []

    def call(self, name, args):
        self.calls += 1
        self.log.append((name, args))
        if self.fail:
            raise self.fail
        if name == "resolve-library-id":
            from fake_context7 import _resolve_text
            return context7_client.ToolText(False, _resolve_text(args["libraryName"]))
        library_id = args["libraryId"]
        if library_id.startswith("/acme/"):
            return context7_client.ToolText(True, f'Library "{library_id}" not found.')
        return context7_client.ToolText(False, self.texts.get(library_id, f"docs for {library_id}"))

    def close(self):
        self.closed = True


def test_capture_writes_pinned_docs_with_an_untrusted_header(tmp_path):
    run = _run(_python_repo(tmp_path))
    stub = StubSession()
    result = docs.capture(run, "Build a FastAPI app using pydantic models",
                          open_session=lambda cfg: stub)
    assert [l.status for l in result.libraries] == ["ok", "ok"]
    assert result.context7_calls == 4 and stub.closed
    pyd = Path(result.libraries[1].path)
    assert pyd.name == "pydantic@2.11.7.md" and pyd.parent.name == "docs"
    text = pyd.read_text()
    assert "/pydantic/pydantic" in text and "2.11.7 (uv.lock)" in text
    assert "not instructions" in text
    # the version rides the id; Python gets a language hint
    queries = [args for name, args in stub.log if name == "query-docs"]
    assert queries[1]["libraryId"] == "/pydantic/pydantic/v2.11.7"
    assert queries[1]["language"] == "Python"
    assert queries[0]["libraryId"] == "/websites/fastapi_tiangolo/v0.115.6"
    tool_calls = [e.name for e in run.tracer.events if e.type == "tool_call"]
    assert tool_calls[0] == "context7:resolve-library-id: fastapi"
    assert len(tool_calls) == 4


def test_rerun_is_served_from_the_cache_without_opening_a_session(tmp_path):
    run = _run(_python_repo(tmp_path))
    docs.capture(run, "fastapi and pydantic", open_session=lambda cfg: StubSession())

    def must_not_open(cfg):
        raise AssertionError("a full cache hit must not open an MCP session")

    again = docs.capture(run, "fastapi and pydantic", open_session=must_not_open)
    assert [l.status for l in again.libraries] == ["cached", "cached"]
    assert again.cache_hits == 2 and again.context7_calls == 0
    assert docs.summary(again)["context7_calls"] == 0


def test_docs_are_truncated_per_library_and_in_total(tmp_path):
    run = _run(_python_repo(tmp_path), max_chars_per_library=100, max_chars_total=150)
    stub = StubSession(texts={"/websites/fastapi_tiangolo/v0.115.6": "F" * 500,
                              "/pydantic/pydantic/v2.11.7": "P" * 500,
                              "/textualize/rich/v13.9.4": "R" * 500})
    result = docs.capture(run, "fastapi pydantic rich", open_session=lambda cfg: stub)
    assert [(l.status, l.chars, l.truncated) for l in result.libraries] == [
        ("ok", 100, True), ("ok", 50, True), ("skipped", 0, False)]
    assert "max_chars_total" in result.libraries[2].note
    assert "truncated from 500" in Path(result.libraries[0].path).read_text()


def test_unknown_library_is_recorded_and_skipped(tmp_path):
    (tmp_path / "requirements.txt").write_text("zzqqxx==1.0\npydantic==2.11.7\n")
    run = _run(tmp_path, libraries=[{"name": "internal", "id": "/acme/internal"}])
    result = docs.capture(run, "use zzqqxx and pydantic", open_session=lambda cfg: StubSession())
    status = {l.name: (l.status, l.note) for l in result.libraries}
    assert status["internal"][0] == "skipped" and "not found" in status["internal"][1]
    assert status["zzqqxx"][0] == "skipped" and "no Context7 library" in status["zzqqxx"][1]
    assert status["pydantic"][0] == "ok"
    envelope = docs.as_envelope(result)
    assert envelope.libraries == ["pydantic@2.11.7"] and "skipped" in envelope.summary
    docs.check(run, envelope)                              # only real files are claimed


def test_network_failure_costs_the_docs_not_the_run(tmp_path):
    run = _run(_python_repo(tmp_path))
    stub = StubSession(fail=context7_client.Context7Error("cannot reach mcp.context7.com"))
    result = docs.capture(run, "fastapi with pydantic", open_session=lambda cfg: stub)
    assert [(l.name, l.status) for l in result.libraries] == [
        ("fastapi", "error"), ("pydantic", "error")]
    assert all("cannot reach" in l.note for l in result.libraries)
    assert stub.calls == 1 and stub.closed                 # one failure is enough to stop asking
    assert result.artifacts == []

    envelope = docs.as_envelope(result)
    assert envelope.status == "success" and envelope.libraries == []
    assert "fastapi, pydantic" in envelope.notes_for_next_agent
    assert "unavailable" in envelope.notes_for_next_agent
    docs.check(run, envelope)                              # nothing is claimed, so nothing is missing
    assert docs.summary(result)["errors"] == "fastapi@0.115.6, pydantic@2.11.7"


def test_a_session_that_cannot_open_is_not_fatal_either(tmp_path):
    run = _run(_python_repo(tmp_path))

    def refuse(cfg):
        raise context7_client.Context7Error("cannot open an MCP session")

    result = docs.capture(run, "fastapi", open_session=refuse)
    assert [(l.name, l.status) for l in result.libraries] == [("fastapi", "error")]


def test_request_text_stays_home_when_send_request_is_off(tmp_path):
    secret = "the unannounced Falcon launch"
    for send, expected in ((True, True), (False, False)):
        root = tmp_path / str(send)
        root.mkdir()
        run = _run(_python_repo(root), send_request=send)
        stub = StubSession()
        docs.capture(run, f"add a fastapi route for {secret}", open_session=lambda cfg: stub)
        sent = json.dumps(stub.log)
        assert "fastapi" in sent
        assert (secret in sent) is expected


def test_required_makes_a_network_failure_fail_the_phase(tmp_path):
    run = _run(_python_repo(tmp_path), required=True)
    stub = StubSession(fail=context7_client.Context7Error("cannot reach mcp.context7.com"))
    with pytest.raises(context7_client.Context7Error, match="cannot reach"):
        docs.capture(run, "fastapi", open_session=lambda cfg: stub)
    assert stub.closed
    assert not list((run.context_handoff_dir / "docs").glob("*.md")) \
        if (run.context_handoff_dir / "docs").exists() else True


def test_refusal_is_a_phase_failure_not_a_skip():
    stub = SimpleNamespace(calls=0, tools={"query-docs": {}},
                           call=lambda name, args: context7_client.ToolText(
                               True, "Rate limited or quota exceeded."))
    with pytest.raises(context7_client.Context7Error, match="CONTEXT7_API_KEY"):
        context7_client.query_docs(stub, "/pydantic/pydantic", "q")


def test_check_gates_the_envelope_claims(tmp_path):
    run = _run(tmp_path)
    missing = docs.DocsOutput(status="success", artifacts=[str(tmp_path / "gone.md")])
    with pytest.raises(RuntimeError, match="missing"):
        docs.check(run, missing)
    assert ("artifacts_exist", False) in run.tracer.gates


# ── context7_client: parsing, and a real MCP round trip against the fake ────

def test_best_match_refuses_unrelated_fuzzy_hits():
    libs = context7_client.parse_libraries(
        "Available Libraries:\n\n- Title: no-mistakes\n- Context7-compatible library ID: "
        "/kunchenguid/no-mistakes\n----------\n- Title: Next.js\n- Context7-compatible library "
        "ID: /vercel/next.js\n- Versions: v15.1.11, v16.2.9")
    assert [l.id for l in libs] == ["/kunchenguid/no-mistakes", "/vercel/next.js"]
    assert context7_client.best_match(libs, "zzqqxx") is None
    nxt = context7_client.best_match(libs, "next", "15.1.11")
    assert context7_client.versioned_id(nxt, "15.1.11") == "/vercel/next.js/v15.1.11"
    assert context7_client.versioned_id(nxt, "14.0.0") == "/vercel/next.js/v14.0.0"
    assert context7_client.versioned_id(nxt, None) == "/vercel/next.js"


def test_mcp_round_trip_sends_no_auth_header_by_default(fake_context7):
    with context7_client.Session(fake_context7.url, timeout_s=10).open() as session:
        assert set(session.tools) == {"resolve-library-id", "query-docs"}
        assert {"libraryName", "query"} <= set(session.tools["resolve-library-id"]["properties"])
        assert set(session.tools["query-docs"]["required"]) == {"libraryId", "query"}
        library = context7_client.resolve(session, "pydantic", "validators", "2.11.7")
        answer = context7_client.query_docs(session, context7_client.versioned_id(library, "2.11.7"),
                                            "validators", "Python")
        moved = context7_client.query_docs(session, "/fastapi/fastapi", "Depends")
        with pytest.raises(context7_client.LibraryNotFound):
            context7_client.query_docs(session, "/acme/nothing", "q")
    assert library.id == "/pydantic/pydantic"
    assert "no indexed documentation for version v2.11.7" in answer.version_note
    assert moved.library_id == "/websites/fastapi_tiangolo"
    assert fake_context7.auth_seen and set(fake_context7.auth_seen) == {None}


def test_mcp_round_trip_sends_the_key_only_when_set(fake_context7, monkeypatch):
    monkeypatch.setenv("CONTEXT7_API_KEY", SECRET)
    with context7_client.open_session(SimpleNamespace(server_url=fake_context7.url, timeout_s=10)) as s:
        context7_client.resolve(s, "rich", "tables")
    assert set(fake_context7.auth_seen) == {f"Bearer {SECRET}"}


def test_unreachable_server_raises_context7_error():
    pytest.importorskip("mcp")
    with pytest.raises(context7_client.Context7Error, match="cannot open an MCP session"):
        context7_client.Session("http://127.0.0.1:9/mcp", timeout_s=3).open()


# ── Layer 1: the MCP config an agent loads ──────────────────────────────────

def _request(**overrides) -> PiRequest:
    fields = dict(prompt="p", system_prompt="s", model="anthropic/haiku", thinking="low",
                  session_id="11111111-2222-3333-4444-555555555555", session_dir="/tmp/s",
                  raw_output_path="/tmp/raw.jsonl", tools=None,
                  extensions=[str(CONTEXT7_MCP_JSON)], cwd="/repo")
    fields.update(overrides)
    return PiRequest(**fields)


def _flag(cmd, name):
    return cmd[cmd.index(name) + 1]


def test_stamped_context7_config_has_no_headers():
    server = json.loads(CONTEXT7_MCP_JSON.read_text())["mcpServers"]["context7"]
    assert server == {"type": "http", "url": "https://mcp.context7.com/mcp"}


def test_build_command_loads_context7_without_a_key(tmp_path):
    cmd = agent_cc.build_command(_request(), resume=False, scratch_dir=tmp_path)
    assert _flag(cmd, "--mcp-config") == str(CONTEXT7_MCP_JSON) and "--strict-mcp-config" in cmd
    assert "mcp__context7" in _flag(cmd, "--allowedTools").split(",")
    assert not list(tmp_path.iterdir())                    # nothing rewritten


def test_build_command_with_a_tools_list_passes_mcp_context7_through():
    cmd = agent_cc.build_command(_request(tools=["read", "bash", "mcp__context7"]), resume=False)
    assert _flag(cmd, "--tools") == "Read,Bash"
    assert _flag(cmd, "--allowedTools") == "Read,Bash,mcp__context7"


def test_key_set_adds_a_placeholder_header_in_a_per_run_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXT7_API_KEY", SECRET)
    cmd = agent_cc.build_command(_request(), resume=False, scratch_dir=tmp_path)
    copy = Path(_flag(cmd, "--mcp-config"))
    assert copy.parent == tmp_path
    server = json.loads(copy.read_text())["mcpServers"]["context7"]
    assert server["headers"] == {"Authorization": "Bearer ${CONTEXT7_API_KEY}"}
    assert SECRET not in copy.read_text() and SECRET not in " ".join(cmd)


def test_an_operator_written_header_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXT7_API_KEY", SECRET)
    own = tmp_path / "mine.mcp.json"
    own.write_text(json.dumps({"mcpServers": {"context7": {
        "type": "http", "url": "https://mcp.context7.com/mcp",
        "headers": {"authorization": "Bearer ${MY_KEY}"}}}}))
    other = tmp_path / "docs.mcp.json"
    other.write_text(json.dumps({"mcpServers": {"docs": {"command": "docs-server"}}}))
    assert agent_cc.prepare_mcp_configs([str(own), str(other)], tmp_path) == [str(own), str(other)]


def test_run_removes_the_per_run_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXT7_API_KEY", SECRET)
    log = tmp_path / "argv.jsonl"
    monkeypatch.setattr(agent_cc, "CLAUDE_PATH", str(FAKE_CLAUDE))
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_LOG", str(log))
    agent_cc.run(_request(session_dir=str(tmp_path / "s"), cwd=str(tmp_path),
                          raw_output_path=str(tmp_path / "raw.jsonl")))
    copy = Path(_flag(json.loads(log.read_text().splitlines()[0]), "--mcp-config"))
    assert copy.name.endswith("context7.mcp.json") and not copy.exists()


def test_pi_ignores_mcp_configs_and_mcp_tools(monkeypatch, tmp_path):
    captured = {}

    class Stop(Exception):
        pass

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        raise Stop

    monkeypatch.setattr(agent_pi, "resolve_model", lambda pattern: ("openrouter", "google/x"))
    monkeypatch.setattr(agent_pi, "context_window", lambda provider, model: 0)
    monkeypatch.setattr(agent_pi.subprocess, "Popen", fake_popen)
    with pytest.raises(Stop):
        agent_pi.run(_request(model="openrouter/google/x", session_id="sssf-a",
                              tools=["read", "mcp__context7"],
                              extensions=["ext.ts", str(CONTEXT7_MCP_JSON)],
                              raw_output_path=str(tmp_path / "raw.jsonl")))
    cmd = captured["cmd"]
    assert _flag(cmd, "--tools") == "read" and _flag(cmd, "-e") == "ext.ts"
    assert str(CONTEXT7_MCP_JSON) not in cmd


# ── validate(): a missing key is only worth a warning when it matters ──────

def _cfg(tmp_path, coding_agent="claude_code") -> SSSFConfig:
    for name in ("s.md", "u.md"):
        (tmp_path / name).write_text("x")
    agent = AgentConfig(name="planner", coding_agent=coding_agent, model="anthropic/haiku",
                        prompt_engineering=PromptEngineering(system=str(tmp_path / "s.md"),
                                                             user=str(tmp_path / "u.md")),
                        harness_engineering=[str(CONTEXT7_MCP_JSON)])
    return SSSFConfig(agents=[agent], context7=Context7Config(enabled=True))


def test_no_warning_when_context7_is_reachable_without_a_key(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_cc, "CLAUDE_PATH", str(FAKE_CLAUDE))
    probed = []
    monkeypatch.setattr(context7_client, "probe", lambda url, timeout=5.0: probed.append(url))
    assert agents.validate(_cfg(tmp_path), ["planner"], docs_phase=True) == []
    assert probed == ["https://mcp.context7.com/mcp"]


def test_warning_when_unreachable_without_a_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(agent_cc, "CLAUDE_PATH", str(FAKE_CLAUDE))
    monkeypatch.setattr(context7_client, "probe", lambda url, timeout=5.0: "HTTP 401 Unauthorized")
    warnings = agents.validate(_cfg(tmp_path), ["planner"], docs_phase=True)
    assert len(warnings) == 1 and "CONTEXT7_API_KEY" in warnings[0] and "planner" in warnings[0]
    assert "warning:" in capsys.readouterr().err


def test_no_probe_with_a_key_or_without_context7(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_cc, "CLAUDE_PATH", str(FAKE_CLAUDE))
    monkeypatch.setattr(context7_client, "probe",
                        lambda *a, **k: pytest.fail("must not probe"))
    monkeypatch.setenv("CONTEXT7_API_KEY", SECRET)
    assert agents.validate(_cfg(tmp_path), ["planner"], docs_phase=True) == []
    monkeypatch.delenv("CONTEXT7_API_KEY")
    # A Pi agent cannot use the MCP config, and this ADW has no docs phase.
    cfg = _cfg(tmp_path, coding_agent="pi")
    cfg.agents[0].model = "openrouter/google/x"
    monkeypatch.setattr(agent_pi, "resolve_model", lambda pattern: ("openrouter", "google/x"))
    assert agents.context7_warnings(cfg, cfg.agents, docs_phase=False) == []


# ── end to end: adw_plan with the fake claude and the fake Context7 ─────────

@pytest.fixture
def docs_repo(tmp_path, fake_context7):
    import yaml
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    _python_repo(repo)
    subprocess.run([sys.executable, str(SKILL / "scripts" / "install.py")], cwd=repo,
                   check=True, capture_output=True)
    mcp_json = repo / "adws" / "adw_data" / "harness_engineering" / "context7.mcp.json"
    mcp_json.write_text(json.dumps({"mcpServers": {"context7": {
        "type": "http", "url": fake_context7.url}}}))
    config_path = repo / "adws" / "adw_sssf_config" / "sssf.config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["context7"]["server_url"] = fake_context7.url
    for agent in config["agents"]:
        if agent["name"] == "planner":
            agent.update(coding_agent="claude_code", model="anthropic/haiku",
                         harness_engineering=["adws/adw_data/harness_engineering/context7.mcp.json"],
                         tools=[t for t in agent["tools"] if not t.startswith("subagent_")])
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    subprocess.run(git + ["add", "-A"], cwd=repo, check=True)
    subprocess.run(git + ["commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _plan(repo: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
    full_env = {**os.environ, "CLAUDE_PATH": str(FAKE_CLAUDE),
                # Any text I/O left to the platform codepage is an error here
                # (on Windows that codepage cannot hold what agents return).
                "PYTHONWARNDEFAULTENCODING": "1", "PYTHONWARNINGS": "error::EncodingWarning",
                **env}
    return subprocess.run([sys.executable, "adws/adw_plan.py", *args], cwd=repo, env=full_env,
                          capture_output=True, text=True, timeout=180)


def test_adw_plan_runs_a_docs_phase_then_rejoins_from_the_cache(docs_repo, fake_context7, tmp_path):
    log = tmp_path / "argv.jsonl"
    # What crashed the docs phase on a Windows cp1253 machine: an emoji in the
    # fetched docs. Greek text too, which that codepage CAN hold, so a file
    # written in it would silently differ from one written in UTF-8.
    fake_context7.doc_body = "### Depends \U0001f446\n\nΠαράδειγμα: use Depends().\n"
    prompt = "Add a FastAPI health endpoint that returns a pydantic model"
    first = _plan(docs_repo, prompt, "--adw-id", "docs0001",
                  FAKE_CLAUDE_ARGV_LOG=str(log), CONTEXT7_API_KEY=SECRET)
    assert first.returncode == 0, first.stdout + first.stderr

    session = docs_repo / "adws" / "adw_data" / "sessions" / "docs0001"
    files = sorted(p.name for p in (session / "context_handoff" / "docs").glob("*.md"))
    assert files == ["fastapi@0.115.6.md", "pydantic@2.11.7.md"]
    fastapi_doc = (session / "context_handoff" / "docs" / files[0]).read_text(encoding="utf-8")
    assert "\U0001f446" in fastapi_doc and "Παράδειγμα" in fastapi_doc
    assert set(fake_context7.auth_seen) == {f"Bearer {SECRET}"}   # the key reached the server
    calls_after_first = len(fake_context7.calls)
    assert calls_after_first == 4

    planner_prompt = (session / "planner" / "prompts" / "user.md").read_text()
    assert '"DocsOutput"' not in planner_prompt                  # it is the envelope, not a type name
    assert "fastapi@0.115.6" in planner_prompt and "context_handoff/docs" in planner_prompt
    argv = json.loads(log.read_text().splitlines()[0])
    assert "mcp__context7" in _flag(argv, "--allowedTools").split(",")

    db_path = docs_repo / "adws" / "adw_data" / "sssf.db"
    db = sqlite3.connect(db_path)
    assert db.execute("select kind, owner, status from phases where adw_id='docs0001' "
                      "and name='docs'").fetchall() == [("code", "context7", "success")]
    assert db.execute("select count(*) from events where type='tool_call' "
                      "and name like 'context7:%'").fetchone() == (4,)

    second = _plan(docs_repo, prompt, "--adw-id", "docs0001",
                   FAKE_CLAUDE_ARGV_LOG=str(log), CONTEXT7_API_KEY=SECRET)
    assert second.returncode == 0, second.stdout + second.stderr
    assert len(fake_context7.calls) == calls_after_first           # no Context7 calls at all
    logs = [json.loads(row[0]) for row in db.execute(
        "select e.payload_json from events e join phases p on e.phase_id = p.phase_id "
        "where p.adw_id='docs0001' and p.name='docs' and e.type='log' order by p.seq")]
    assert [(l["cache_hits"], l["context7_calls"]) for l in logs
            if "cache_hits" in l] == [(0, 4), (2, 0)]

    # The key was set for both runs: its value is nowhere in the record.
    db.close()
    leaked = [str(p) for p in docs_repo.rglob("*")
              if p.is_file() and ".git" not in p.parts and SECRET.encode() in p.read_bytes()]
    assert leaked == []
    for name in ("raw_output.jsonl",):
        assert SECRET not in (session / "planner" / name).read_text()


def test_adw_plan_survives_an_unreachable_context7(docs_repo, tmp_path):
    import yaml
    config_path = docs_repo / "adws" / "adw_sssf_config" / "sssf.config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["context7"]["server_url"] = "http://127.0.0.1:9/mcp"      # nothing listens here
    config["context7"]["timeout_s"] = 5
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))

    result = _plan(docs_repo, "Add a FastAPI health endpoint", "--adw-id", "down0001")
    assert result.returncode == 0, result.stdout + result.stderr

    db = sqlite3.connect(docs_repo / "adws" / "adw_data" / "sssf.db")
    assert db.execute("select status from phases where adw_id='down0001' and name='docs'"
                      ).fetchone() == ("success",)
    assert db.execute("select status from sessions where adw_id='down0001'").fetchone() == ("success",)
    planner_prompt = (docs_repo / "adws" / "adw_data" / "sessions" / "down0001" / "planner"
                      / "prompts" / "user.md").read_text()
    assert "No docs could be fetched for: fastapi" in planner_prompt

    config["context7"]["required"] = True
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    strict = _plan(docs_repo, "Add a FastAPI health endpoint", "--adw-id", "down0002")
    assert strict.returncode == 1
    assert db.execute("select status from phases where adw_id='down0002' and name='docs'"
                      ).fetchone() == ("fail",)
