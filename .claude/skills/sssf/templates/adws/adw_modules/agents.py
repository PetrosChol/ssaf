"""Config loading/validation and agent execution.

Every ADW validates its agents before running (fail fast, nothing spawns
against a half-valid config). Every agent call parses against a concrete
output type; parse failures and gate violations re-prompt the SAME session
with a correction — context intact, bounded retries. Agent proposes, code
disposes.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Optional

import yaml

from . import agent_cc, agent_pi, context7_client, permissions, prompts
from .data_types import (AgentCall, AgentConfig, EnvelopeBase, EventRecord,
                         GateCheck, GateReport, Phase, PiRequest, PiResult, SSSFConfig,
                         UsageBreakdown)
from .utils import new_id

JSON_FIX_ATTEMPTS = 2      # continue-with-correction attempts for malformed JSON

# coding_agent -> the module that drives it. Every backend exposes the same
# surface: resolve_model(pattern), run(request, on_event, on_spawn, on_exit)
# and ToolCallTracker; optionally validate_agent(agent) and SESSION_ID_IS_UUID.
BACKENDS = {"pi": agent_pi, "claude_code": agent_cc}

# Per-backend session state directory under each agent's session dir. Pi's
# keeps its historical name so existing runs still rejoin.
_SESSION_DIRS = {"pi": "pi_sessions", "claude_code": "cc_sessions"}


def backend(agent: AgentConfig):
    try:
        return BACKENDS[agent.coding_agent]
    except KeyError:
        raise SystemExit(f"agent {agent.name!r}: unknown coding_agent "
                         f"{agent.coding_agent!r} — available: {sorted(BACKENDS)}")


class GateFailure(RuntimeError):
    pass


# ── config ───────────────────────────────────────────────────────────────────

def load_config(path: str = "adws/adw_sssf_config/sssf.config.yaml") -> SSSFConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8-sig")) or {}
    defaults = raw.get("defaults", {}) or {}
    for agent in raw.get("agents", []) or []:
        for key in ("coding_agent", "model", "thinking", "color", "tools", "writes"):
            if key in defaults:
                agent.setdefault(key, defaults[key])
        agent.setdefault("harness_engineering", defaults.get("harness_engineering", []))
    return SSSFConfig(**raw)


def resolve(cfg: SSSFConfig, name: str) -> AgentConfig:
    for agent in cfg.agents:
        if agent.name == name:
            return agent
    raise SystemExit(f"agent {name!r} is not defined in the config — "
                     f"available: {[a.name for a in cfg.agents]}")


def _context7_urls(agent: AgentConfig) -> list[str]:
    """URLs of the Context7 servers a Claude Code agent loads through its MCP configs."""
    if agent.coding_agent != "claude_code":
        return []                          # Pi has no MCP client; it runs without Layer 1
    urls = []
    for entry in agent.harness_engineering or []:
        if not entry.endswith(".json") or entry.endswith("settings.json"):
            continue
        try:
            servers = json.loads(Path(entry).read_text(encoding="utf-8-sig")).get("mcpServers") or {}
        except (OSError, ValueError, AttributeError):
            continue
        urls += [str(server.get("url") or "") for name, server in servers.items()
                 if agent_cc.is_context7_server(name, server)]
    return urls


def context7_warnings(cfg: SSSFConfig, agents_used: list[AgentConfig],
                      docs_phase: bool = False) -> list[str]:
    """Non-fatal Context7 notes. Context7 works without a key, so a missing
    CONTEXT7_API_KEY is only worth saying when the server cannot be reached
    without one (no proxy-injected key, anonymous access refused, or no network)."""
    layer1 = {agent.name: _context7_urls(agent) for agent in agents_used}
    layer1 = {name: urls for name, urls in layer1.items() if urls}
    layer2 = docs_phase and cfg.context7.enabled
    if not (layer1 or layer2):
        return []
    warnings = []
    if layer2:
        for name, urls in layer1.items():
            for url in urls:
                if url and url != cfg.context7.server_url:
                    warnings.append(f"agent {name!r}: its Context7 MCP server {url} differs from "
                                    f"context7.server_url {cfg.context7.server_url} — Layer 1 and "
                                    f"Layer 2 would read different servers")
    if not context7_client.api_key():
        url = cfg.context7.server_url if layer2 else next(
            (u for urls in layer1.values() for u in urls if u), context7_client.DEFAULT_SERVER_URL)
        reason = context7_client.probe(url)
        if reason:
            users = ", ".join(sorted(layer1) + (["docs phase"] if layer2 else []))
            warnings.append(f"Context7 ({users}): {url} is not usable without a key ({reason}) — "
                            f"set CONTEXT7_API_KEY in .env, or give the environment's proxy a "
                            f"secret for its host")
    return warnings


def validate(cfg: SSSFConfig, required: list[str], docs_phase: bool = False) -> list[str]:
    """Fail fast: every required name must resolve to a usable agent.

    Returns the non-fatal warnings it printed (Context7 reachability).
    `docs_phase=True` is passed by ADWs that run the Context7 docs phase.
    """
    problems = []
    usable: list[AgentConfig] = []
    for name in required:
        try:
            agent = resolve(cfg, name)
        except SystemExit as e:
            problems.append(str(e))
            continue
        if agent.coding_agent not in BACKENDS:
            problems.append(f"agent {name!r}: coding_agent {agent.coding_agent!r} "
                            f"is not implemented — available: {sorted(BACKENDS)}")
            continue
        driver = backend(agent)
        if hasattr(driver, "validate_agent"):
            problems.extend(driver.validate_agent(agent))
        for label, ref in (("system", agent.prompt_engineering.system),
                           ("user", agent.prompt_engineering.user)):
            if not Path(ref).is_file():
                problems.append(f"agent {name!r}: {label} prompt not found: {ref}")
        try:
            driver.resolve_model(agent.model)
        except ValueError as e:
            problems.append(f"agent {name!r}: {e}")
        usable.append(agent)
    if problems:
        raise SystemExit("config validation failed:\n- " + "\n- ".join(problems))
    warnings = context7_warnings(cfg, usable, docs_phase)
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return warnings


# ── execution ────────────────────────────────────────────────────────────────

def execute(run, phase: Phase, call: AgentCall) -> EnvelopeBase:
    """One agent call: render prompts -> coding agent run -> typed parse -> gates -> envelope."""
    agent = resolve(run.cfg, phase.params.owner)
    driver = backend(agent)
    agent_dir = run.session_dir / agent.name
    agent_dir.mkdir(parents=True, exist_ok=True)

    variables = {
        "prompt": call.prompt,
        "previous_envelope": call.previous.model_dump_json(indent=2) if call.previous else "(none)",
        "context_handoff_dir": str(run.context_handoff_dir),
    }
    system_text = prompts.render(agent.prompt_engineering.system, variables)
    user_text = prompts.render(agent.prompt_engineering.user, variables)
    prompts.save(agent_dir / "prompts", "system.md", system_text)
    prompts.save(agent_dir / "prompts", "user.md", user_text)

    session_id = _agent_session_id(run, agent)
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="agent_start", name=agent.name,
                                 payload={"model": agent.model, "thinking": agent.thinking,
                                          "color": agent.color,
                                          "session_id": session_id,
                                          "coding_agent": agent.coding_agent,
                                          "purpose": agent.purpose,
                                          "tools": agent.tools,  # None = all tools
                                          "harness_engineering": agent.harness_engineering}))
    run.console.agent_started(agent.name, agent.model, session_id)

    # Parse retries and gate corrections re-enter the SAME session, so the
    # last send is the one whose context occupancy is current — while spend is
    # the opposite: every send costs, so usage accumulates across all of them.
    latest: PiResult | None = None
    spent = UsageBreakdown()

    def send(prompt_text: str) -> PiResult:
        nonlocal latest
        request = PiRequest(
            prompt=prompt_text,
            system_prompt=system_text,
            model=agent.model,
            thinking=agent.thinking,
            session_id=session_id,
            # absolute: these are read by the agent subprocess, which runs in repo_root
            session_dir=str((agent_dir / _SESSION_DIRS.get(agent.coding_agent,
                                                           "pi_sessions")).resolve()),
            raw_output_path=str((agent_dir / "raw_output.jsonl").resolve()),
            tools=agent.tools,
            extensions=agent.harness_engineering,
            cwd=str(run.repo_root),
        )
        result = driver.run(
            request,
            on_event=_event_forwarder(run, phase, agent),
            on_spawn=lambda pid: run.tracer.process_start(
                run.adw_id, "agent", agent.name, pid,
                f"{agent.coding_agent} {agent.name} {agent.model}"),
            on_exit=lambda pid: run.tracer.process_end(run.adw_id, pid))
        run.add_usage(result.tokens, result.cost)
        spent.merge(result.usage)
        latest = result
        return result

    # What the tree looked like before this agent got its hands on it. Every
    # send in this phase — first prompt, JSON retries, gate corrections — is
    # measured against this one baseline.
    tree_before = permissions.snapshot(run)

    def check_permissions() -> list[str]:
        try:
            return permissions.enforce(run, phase, agent, tree_before)
        except permissions.PermissionBreach as breach:
            run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                         type="error", name="permission_breach",
                                         payload={"agent": agent.name, "error": str(breach),
                                                  "writes": agent.writes,
                                                  "protected_files": run.cfg.defaults.protected_files}))
            raise

    try:
        result = send(user_text)
        envelope, attempt = _parse_with_retries(run, phase, call, result, send)

        # claim gates — violations flow back into the SAME session as corrections
        for gate_attempt in range(1, max(1, phase.params.retries + 1) + 1):
            violations = []
            for gate in call.gates:
                report = _as_report(gate(envelope, run))
                found = report.violations
                run.tracer.gate_row(phase, gate.__name__, report, gate_attempt)
                run.tracer.event(EventRecord(
                    adw_id=run.adw_id, phase_id=phase.phase_id,
                    type="gate_fail" if found else "gate_pass", name=gate.__name__,
                    payload={"attempt": gate_attempt, "violations": found,
                             "checks": [c.model_dump() for c in report.checks]}))
                run.console.gate_result(gate.__name__, report)
                violations.extend(found)
            if not violations:
                break
            if gate_attempt > phase.params.retries:
                raise GateFailure(f"{agent.name} failed gates after {gate_attempt} attempt(s):\n- "
                                  + "\n- ".join(violations))
            phase.attempt = gate_attempt
            run.console.retry(agent.name, gate_attempt, phase.params.retries,
                              f"{len(violations)} gate violation(s)")
            correction = ("Your previous response failed validation:\n- "
                          + "\n- ".join(violations)
                          + "\n\nFix these problems, then re-emit ONLY your Report JSON.")
            result = send(correction)
            envelope, attempt = _parse_with_retries(run, phase, call, result, send)
    except BaseException as error:
        # A phase that dies — bad JSON, failed gates, a crashed or killed agent —
        # still had an agent loose in the tree. Enforce the boundary on the way
        # out too, or its unauthorized writes outlive the failure and the next
        # phase's snapshot takes them for the operator's own work.
        try:
            check_permissions()
        except permissions.PermissionBreach as breach:
            raise breach from error
        raise

    # Permission is checked after every send is done, and before the envelope is
    # accepted: an agent does not get to report success on a phase in which it
    # wrote somewhere it was not allowed to.
    touched = check_permissions()
    if touched:
        run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                     type="log", name="paths_touched",
                                     payload={"agent": agent.name, "paths": touched}))

    _persist_envelope(run, phase, agent.name, call, envelope, attempt, valid=True)
    run.console.envelope_summary(envelope)
    context = latest or result
    run.tracer.agent_session_row(run.adw_id, agent, session_id,
                                 context_tokens=context.context_tokens,
                                 context_window=context.context_window)
    run.save_agent_map(agent.name, {"session_id": session_id, "model": agent.model,
                                    "coding_agent": agent.coding_agent})
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="handoff", name=agent.name,
                                 payload={"artifacts": envelope.artifacts,
                                          "summary": envelope.summary}))
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="agent_end", name=agent.name,
                                 # Phase totals, not the last send's: a retried
                                 # phase paid for every attempt.
                                 tokens=spent.total_tokens,
                                 payload={"cost": spent.total_cost,
                                          "usage": spent.model_dump(),
                                          "context_tokens": context.context_tokens,
                                          "context_window": context.context_window}))
    run.console.agent_finished(agent.name, spent.total_tokens, spent.total_cost)
    if envelope.status != "success":
        raise RuntimeError(f"{agent.name} reported status={envelope.status!r}: {envelope.summary}")
    return envelope


# ── internals ────────────────────────────────────────────────────────────────

def _as_report(result) -> GateReport:
    """Accept a GateReport, or a legacy gate that returned a violations list."""
    if isinstance(result, GateReport):
        return result
    return GateReport(checks=[GateCheck(item=str(v), ok=False) for v in (result or [])])


def _agent_session_id(run, agent: AgentConfig) -> str:
    entry = run.agent_map.get(agent.name)
    if (entry and entry.get("model") == agent.model
            and entry.get("coding_agent", "pi") == agent.coding_agent):
        return entry["session_id"]           # rejoin the existing context window
    if getattr(backend(agent), "SESSION_ID_IS_UUID", False):
        return str(uuid.uuid4())             # claude --session-id takes UUIDs only
    return f"sssf-{run.adw_id}-{agent.name}-{new_id(4)}"


def _event_forwarder(run, phase: Phase, agent: AgentConfig):
    """One tool_call event per real tool call, with its exact args and result."""
    tracker = backend(agent).ToolCallTracker()

    def forward(event: dict) -> None:
        observed = tracker.observe(event)
        if not observed:
            return
        # Pi's tracker yields one record; Claude Code's can close several calls
        # in one message and yields a list.
        for record in observed if isinstance(observed, list) else [observed]:
            # The call's span rides the columns; duration_ms stays in the payload.
            run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                         type="tool_call", name=record.pop("label"),
                                         started_at=record.pop("started_at", None),
                                         ended_at=record.pop("ended_at", None),
                                         payload={**record, "agent": agent.name}))
    return forward


def _extract_json(text: str) -> dict:
    candidate = text
    if "```" in text:
        for block in text.split("```")[1::2]:
            block = block.removeprefix("json").strip()
            if block.startswith("{"):
                candidate = block
                break
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in the response")
    return json.loads(candidate[start:end + 1])


def _parse_with_retries(run, phase: Phase, call: AgentCall, result, send):
    """Parse the final response against the declared output type; on failure,
    continue the SAME session with a correction (bounded)."""
    for attempt in range(1, JSON_FIX_ATTEMPTS + 2):
        try:
            payload = _extract_json(result.text)
            return call.output_type.model_validate(payload), attempt
        except Exception as error:
            _persist_envelope(run, phase, phase.params.owner, call, None, attempt,
                              valid=False, raw=result.text)
            if attempt > JSON_FIX_ATTEMPTS:
                raise RuntimeError(
                    f"{phase.params.owner} never produced valid "
                    f"{call.output_type.__name__} JSON: {error}") from error
            run.console.retry(phase.params.owner, attempt, JSON_FIX_ATTEMPTS,
                              f"invalid {call.output_type.__name__} JSON: {error}")
            fields = ", ".join(call.output_type.model_fields.keys())
            result = send(
                f"Your response was not valid JSON for the required structure "
                f"({error}). Respond again with ONLY a JSON object with these "
                f"fields: {fields}. No prose, no code fences.")


def _persist_envelope(run, phase: Phase, agent_name: str, call: AgentCall,
                      envelope: Optional[EnvelopeBase], attempt: int,
                      valid: bool, raw: str = "") -> None:
    payload_json = envelope.model_dump_json(indent=2) if envelope else json.dumps({"raw": raw[-2000:]})
    run.tracer.envelope_row(phase, agent_name, call.output_type.__name__,
                            payload_json, valid, attempt)
    if envelope:
        record = {"agent_name": agent_name, "purpose": resolve(run.cfg, agent_name).purpose,
                  "output_type": call.output_type.__name__, "attempt": attempt,
                  **envelope.model_dump()}
        (run.session_dir / agent_name / "envelope.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
