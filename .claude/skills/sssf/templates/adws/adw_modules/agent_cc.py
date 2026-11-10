"""Claude Code interface — the second coding agent, beside Pi.

Runs `claude -p --output-format stream-json --verbose` and tails its JSONL
stdout line by line, forwarding each event to a callback WHILE the agent works,
exactly like agent_pi. It takes the same request and returns the same result,
so everything above this module (prompts, envelopes, gates, permissions, the
tracer, the visualizer) is backend-agnostic.

Where Claude Code differs from Pi, and how this module absorbs it:

- Sessions. Pi's `--session-id` is create-or-continue. Claude Code needs a UUID
  and two different flags: `--session-id <uuid>` creates, `--resume <uuid>`
  continues. A small ledger file per session (in the request's session_dir)
  records that the session exists, so the caller keeps making one call.
- Cost. On a resumed session Claude Code reports the WHOLE conversation's
  running cost in `total_cost_usd`. The ledger keeps the last running total so
  each run is charged only its own share. `usage` is already per-run.
- Permissions. A headless `claude -p` run denies anything that would prompt.
  The agent's tools are pre-approved with `--allowedTools` and the run uses
  `--permission-mode dontAsk` (override: SSSF_CC_PERMISSION_MODE). That is the
  capability list only — the `writes:` boundary is still enforced afterwards by
  permissions.py, the same as for Pi.
- Tools. The roster may keep Pi's lowercase names (read, bash, edit, write,
  grep, find, ls); they map onto Claude Code's (Read, Bash, Edit, Write, Grep,
  Glob). Claude Code names (`WebFetch`, `mcp__server__tool`) pass through.
- Thinking. The roster's thinking level maps onto `--effort`.
- Harness. Pi extensions (`.ts`) cannot load here. `harness_engineering`
  entries for a Claude Code agent are JSON: `*settings.json` files go to
  `--settings`, any other `.json` file to `--mcp-config`.
- Context7 auth. `context7.mcp.json` ships with NO headers: in a Claude cloud
  environment the egress proxy adds `Authorization` for mcp.context7.com, and
  anonymous access works anywhere. Claude Code has no conditional header (an
  unset `${CONTEXT7_API_KEY}` is sent literally), so when CONTEXT7_API_KEY IS
  set, each run gets a temp copy of the config whose Context7 server carries
  `Authorization: Bearer ${CONTEXT7_API_KEY}` — the placeholder, which Claude
  Code fills from the environment. The key itself is never written to disk.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from . import process
from .agent_pi import ARG_VALUE_CHARS, RESULT_SNIPPET_CHARS, _clip, _label
from .data_types import PiRequest, PiResult
from .utils import now_iso, operator_env

CLAUDE_PATH = os.environ.get("CLAUDE_PATH", "claude")
PERMISSION_MODE = os.environ.get("SSSF_CC_PERMISSION_MODE", "dontAsk")
# --bare skips CLAUDE.md, hooks, skills, plugins and MCP auto-discovery, so a
# run is the same on every machine — but it also skips the subscription login
# and needs ANTHROPIC_API_KEY. Opt in with SSSF_CC_BARE=1.
BARE = os.environ.get("SSSF_CC_BARE", "").lower() in ("1", "true", "yes")

# agents.py mints UUID session ids for this backend: `--session-id` rejects others.
SESSION_ID_IS_UUID = True

PROVIDER = "anthropic"

PI_TO_CC_TOOLS = {
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "bash": "Bash",
    "grep": "Grep",
    "find": "Glob",
    "ls": "Glob",
}

# What a Claude Code agent with no `tools:` list may use without prompting.
# Naming Glob/Grep also brings them back on macOS/Linux, where Claude Code
# leaves them out of the default set in favour of find/grep through Bash.
DEFAULT_ALLOWED_TOOLS = ["Read", "Edit", "Write", "Bash", "Glob", "Grep", "NotebookEdit"]

# The roster's thinking scale -> Claude Code's --effort levels.
EFFORT = {
    "off": "low",
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}


# ── config mapping ──────────────────────────────────────────────────────────

def resolve_model(pattern: str) -> tuple[str, str]:
    """`anthropic/<id>` or a bare id/alias (`sonnet`, `claude-opus-5-5`).

    Claude Code only reaches Anthropic models (directly, or through the
    Bedrock / Vertex / Foundry settings of the operator's own install), so any
    other provider prefix is a config error, caught before anything spawns. An
    unknown id is not: Claude Code has no offline catalog to check it against,
    so it surfaces when the agent runs.
    """
    pattern = (pattern or "").strip()
    if not pattern:
        raise ValueError("model is empty")
    if "/" in pattern:
        provider, model_id = pattern.split("/", 1)
        if provider != PROVIDER:
            raise ValueError(f"model {pattern!r}: coding_agent claude_code runs Anthropic "
                             f"models only — write it as anthropic/<model-id> or a bare "
                             f"alias such as sonnet or opus")
        if not model_id:
            raise ValueError(f"model {pattern!r} names no model id")
        return PROVIDER, model_id
    return PROVIDER, pattern


def map_tools(tools: Optional[list[str]]) -> list[str]:
    """Roster tool names -> Claude Code tool names, order kept, duplicates dropped."""
    mapped: list[str] = []
    for name in tools or []:
        cc = PI_TO_CC_TOOLS.get(name, name)
        if cc not in mapped:
            mapped.append(cc)
    return mapped


def _unmappable(tools: Optional[list[str]]) -> list[str]:
    """Lowercase names are Pi's vocabulary; only the ones in the map translate."""
    return [name for name in tools or []
            if name[:1].islower() and name not in PI_TO_CC_TOOLS
            and not name.startswith("mcp__")]


def _split_harness(entries: list[str]) -> tuple[list[str], list[str]]:
    settings = [e for e in entries if e.endswith("settings.json")]
    mcp = [e for e in entries if e.endswith(".json") and e not in settings]
    return settings, mcp


def _mcp_server_names(paths: list[str]) -> list[str]:
    names = []
    for path in paths:
        try:
            servers = json.loads(Path(path).read_text(encoding="utf-8-sig")).get("mcpServers", {}) or {}
        except (OSError, ValueError, AttributeError):
            continue
        names.extend(servers.keys())
    return names


CONTEXT7_KEY_ENV = "CONTEXT7_API_KEY"
CONTEXT7_AUTH = "Bearer ${" + CONTEXT7_KEY_ENV + "}"   # expanded by Claude Code, not by us


def is_context7_server(name: str, server) -> bool:
    if not isinstance(server, dict):
        return False
    host = (urlparse(str(server.get("url") or "")).hostname or "").lower()
    return name == "context7" or host == "context7.com" or host.endswith(".context7.com")


def _with_context7_auth(path: str) -> Optional[dict]:
    """The config with an Authorization placeholder on its Context7 server(s),
    or None when nothing needs adding (no Context7 server, or a header already set)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    changed = False
    for name, server in (servers or {}).items():
        if not is_context7_server(name, server):
            continue
        headers = server.get("headers") or {}
        if any(key.lower() == "authorization" for key in headers):
            continue                          # the operator wrote their own
        server["headers"] = {**headers, "Authorization": CONTEXT7_AUTH}
        changed = True
    return data if changed else None


def prepare_mcp_configs(paths: list[str], scratch_dir: Optional[Path]) -> list[str]:
    """MCP config paths to hand to `--mcp-config` for THIS run.

    Unchanged unless CONTEXT7_API_KEY is set; then a Context7 server without
    an Authorization header is given one, in a copy under `scratch_dir`.
    """
    if not os.environ.get(CONTEXT7_KEY_ENV, "").strip():
        return list(paths)
    prepared = []
    for index, path in enumerate(paths):
        data = _with_context7_auth(path)
        if data is None:
            prepared.append(path)
            continue
        if scratch_dir is None:
            scratch_dir = Path(tempfile.mkdtemp(prefix="sssf-mcp-"))
        copy = Path(scratch_dir) / f"{index:02d}_{Path(path).name}"
        copy.write_text(json.dumps(data, indent=2), encoding="utf-8")
        prepared.append(str(copy))
    return prepared


def validate_agent(agent) -> list[str]:
    """Problems this backend can see before anything spawns."""
    problems = []
    if not shutil.which(CLAUDE_PATH):
        problems.append(f"agent {agent.name!r}: Claude Code binary {CLAUDE_PATH!r} not found "
                        f"on PATH — install Claude Code or set CLAUDE_PATH")
    for entry in agent.harness_engineering or []:
        if not entry.endswith(".json"):
            problems.append(f"agent {agent.name!r}: harness_engineering entry {entry!r} is not "
                            f"usable with claude_code — pi extensions (.ts) are Pi-only; use an "
                            f"MCP config (.json) or a settings file (*settings.json)")
        elif not Path(entry).is_file():
            problems.append(f"agent {agent.name!r}: harness_engineering file not found: {entry}")
    for name in _unmappable(agent.tools):
        problems.append(f"agent {agent.name!r}: tool {name!r} has no Claude Code equivalent "
                        f"(Pi extension tools do not exist here) — remove it or use a Claude "
                        f"Code tool name")
    return problems


def build_command(request: PiRequest, resume: bool,
                  scratch_dir: Optional[Path] = None) -> list[str]:
    """The exact argv for one non-interactive Claude Code turn.

    `scratch_dir` receives per-run MCP config copies (see prepare_mcp_configs);
    run() passes a temp dir it removes when the turn ends.
    """
    _, model_id = resolve_model(request.model)
    cmd = [CLAUDE_PATH, "-p", "--output-format", "stream-json", "--verbose"]
    if BARE:
        cmd.append("--bare")
    cmd += ["--model", model_id,
            "--system-prompt", request.system_prompt,
            "--permission-mode", PERMISSION_MODE]
    effort = EFFORT.get(request.thinking)
    if effort:
        cmd += ["--effort", effort]
    cmd += ["--resume" if resume else "--session-id", request.session_id]

    settings, mcp = _split_harness(request.extensions)
    if request.tools is not None:
        mapped = map_tools(request.tools)
        builtin = [t for t in mapped if not t.startswith("mcp__")]
        cmd += ["--tools", ",".join(builtin)]
        allowed = mapped
    else:
        allowed = DEFAULT_ALLOWED_TOOLS + [f"mcp__{n}" for n in _mcp_server_names(mcp)]
    if allowed:
        cmd += ["--allowedTools", ",".join(allowed)]
    for path in settings:
        cmd += ["--settings", path]
    if mcp:
        cmd += ["--mcp-config", *prepare_mcp_configs(mcp, scratch_dir), "--strict-mcp-config"]
    # `--` ends the option list: --mcp-config is variadic, and a prompt that
    # starts with "-" must never be read as a flag.
    cmd += ["--", request.prompt]
    return cmd


# ── session ledger ──────────────────────────────────────────────────────────

def _ledger_path(request: PiRequest) -> Path:
    return Path(request.session_dir) / f"{request.session_id}.json"


def _read_ledger(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _write_ledger(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entry, indent=2), encoding="utf-8")


# ── stream parsing ──────────────────────────────────────────────────────────

def _result_text(content) -> str:
    """A tool_result's content: a string, or a list of content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content
                       if isinstance(part, dict) and part.get("type") == "text")
    return ""


def _context_tokens(usage: dict) -> int:
    """Tokens occupying the window after one API call: the whole prompt sent
    (fresh input + cache reads + cache writes) plus what the model wrote."""
    return int(sum(usage.get(part) or 0 for part in (
        "input_tokens", "cache_read_input_tokens",
        "cache_creation_input_tokens", "output_tokens")))


class ToolCallTracker:
    """Folds Claude Code's stream into ONE normalized record per finished tool call.

    A call is announced as a `tool_use` block inside an `assistant` message and
    finishes as a `tool_result` block inside the next `user` message. One user
    message can close several calls, so `observe` returns a list. The record
    shape is identical to agent_pi.ToolCallTracker's, so the tracer and the UI
    cannot tell the backends apart.
    """

    def __init__(self) -> None:
        self._open: dict[str, dict] = {}

    def observe(self, event: dict) -> Optional[list[dict]]:
        etype = event.get("type")
        content = (event.get("message") or {}).get("content")
        if not isinstance(content, list):
            return None
        if etype == "assistant":
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    self._announce(block, event.get("parent_tool_use_id"))
            return None
        if etype != "user":
            return None
        records = []
        for block in content:
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                continue
            call_id = str(block.get("tool_use_id") or "")
            opened = self._open.pop(call_id, {})
            tool = str(opened.get("tool") or "tool")
            args = opened.get("args") or {}
            record = {
                "tool": tool,
                "tool_call_id": call_id,
                "args": {key: _clip(value, ARG_VALUE_CHARS) if isinstance(value, str) else value
                         for key, value in args.items()},
                "ok": not block.get("is_error", False),
                "label": _label(tool, args),
            }
            if opened.get("parent_tool_use_id"):
                record["parent_tool_use_id"] = opened["parent_tool_use_id"]
            text = _result_text(block.get("content"))
            if text:
                record["result_snippet"] = _clip(text, RESULT_SNIPPET_CHARS)
            record["ended_at"] = now_iso()
            if opened.get("clock"):
                record["duration_ms"] = int((time.monotonic() - opened["clock"]) * 1000)
            if opened.get("started_at"):
                record["started_at"] = opened["started_at"]
            records.append(record)
        return records or None

    def _announce(self, block: dict, parent: Optional[str]) -> None:
        call_id = block.get("id")
        if not call_id or str(call_id) in self._open:
            return
        self._open[str(call_id)] = {
            "tool": block.get("name") or "",
            "args": block.get("input") or {},
            "parent_tool_use_id": parent,
            "started_at": now_iso(),
            "clock": time.monotonic(),
        }


class StreamState:
    """Everything one run's stream says about its outcome, folded line by line."""

    def __init__(self) -> None:
        self.text = ""
        self.result: Optional[dict] = None
        self.context_tokens = 0
        self.session_seen = False

    def observe(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "system" and event.get("subtype") == "init":
            self.session_seen = True
        elif etype == "assistant" and not event.get("parent_tool_use_id"):
            message = event.get("message") or {}
            text = "".join(part.get("text", "") for part in message.get("content") or []
                           if isinstance(part, dict) and part.get("type") == "text")
            if text:
                self.text = text            # last main-thread assistant text wins
            turn = _context_tokens(message.get("usage") or {})
            if turn:
                self.context_tokens = turn
        elif etype == "result":
            self.result = event


def _context_window(result: dict) -> int:
    windows = [int(entry.get("contextWindow") or 0)
               for entry in (result.get("modelUsage") or {}).values()
               if isinstance(entry, dict)]
    return max(windows, default=0)


def finalize(state: StreamState, ledger: Optional[dict]) -> tuple[PiResult, dict]:
    """Turn a folded stream into a PiResult plus the ledger entry to store."""
    result = state.result or {}
    out = PiResult(text=result.get("result") or state.text,
                   context_tokens=state.context_tokens,
                   context_window=_context_window(result))
    running = float(result.get("total_cost_usd") or 0.0)
    previous = float((ledger or {}).get("total_cost_usd") or 0.0)
    # The running total only grows; anything else means it was not cumulative.
    cost = running - previous if running >= previous else running
    out.usage.add_claude_code(result.get("usage") or {}, cost)
    out.tokens = out.usage.total_tokens
    out.cost = out.usage.total_cost
    entry = {"session_id": result.get("session_id") or "",
             "total_cost_usd": running if result else previous,
             "updated_at": now_iso()}
    return out, entry


# ── run ─────────────────────────────────────────────────────────────────────

def run(request: PiRequest, on_event: Optional[Callable[[dict], None]] = None,
        on_spawn: Optional[Callable[[int], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None) -> PiResult:
    """Run one non-interactive Claude Code turn — same contract as agent_pi.run."""
    scratch = Path(tempfile.mkdtemp(prefix="sssf-cc-"))
    try:
        return _run(request, scratch, on_event, on_spawn, on_exit)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _run(request: PiRequest, scratch: Path, on_event, on_spawn, on_exit) -> PiResult:
    ledger_path = _ledger_path(request)
    ledger = _read_ledger(ledger_path)
    cmd = build_command(request, resume=ledger is not None, scratch_dir=scratch)

    raw_path = Path(request.raw_output_path)
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    state = StreamState()
    with raw_path.open("a", encoding="utf-8") as raw:
        def on_line(line: str) -> None:
            nonlocal ledger
            raw.write(line)
            raw.flush()                      # events land on disk as they happen
            line = line.strip()
            if not line:
                return
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                return
            first_sighting = not state.session_seen
            state.observe(event)
            if first_sighting and state.session_seen and ledger is None:
                # The session exists from here on: any later call must resume
                # it, even if this one dies before it reports a result.
                ledger = {"session_id": request.session_id, "total_cost_usd": 0.0,
                          "updated_at": now_iso()}
                _write_ledger(ledger_path, ledger)
            if on_event:
                on_event(event)

        # process.stream owns the child: DEVNULL stdin, drained stderr, the
        # timeout, and killing the whole process group however this is left.
        returncode, stderr = process.stream(
            cmd, request.cwd, operator_env(), on_line,
            on_spawn=on_spawn, on_exit=on_exit, timeout_s=request.timeout_s)

    result, entry = finalize(state, ledger)
    result.session_id = request.session_id
    result.returncode = returncode
    if state.result is not None:
        _write_ledger(ledger_path, entry)
    if state.result and state.result.get("is_error"):
        raise RuntimeError(f"claude reported an error ({state.result.get('subtype')}): "
                           f"{str(state.result.get('result') or '')[-800:]}"
                           f"{' — ' + stderr.strip()[-400:] if stderr.strip() else ''}")
    if returncode != 0 and not result.text:
        raise RuntimeError(f"claude exited {returncode}: {stderr.strip()[-800:]}")
    return result
