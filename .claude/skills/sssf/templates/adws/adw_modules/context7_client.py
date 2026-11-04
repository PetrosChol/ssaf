"""Context7 over MCP — the one door both Context7 layers go through.

Layer 1 hands the Context7 MCP server to an agent (`context7.mcp.json`).
Layer 2, the `docs` code phase, calls the SAME server's SAME two tools from
Python through the official MCP Python SDK (streamable HTTP), with no model in
the loop. One endpoint and one tool contract, so the layers cannot drift.

Verified against the live server (Context7 4.5.0, MCP SDK 2.3.0):

    resolve-library-id(libraryName, query)       -> text: "Available Libraries:" blocks
    query-docs(libraryId, query, language?)      -> text: snippets, maybe a "Version note:"

A version is pinned through the id: `/org/project/v1.2.3`. When that version is
not indexed the server says so in a leading `Version note:` line and serves the
nearest docs instead. A moved library answers isError "… has been redirected
to this library: /new/id." — followed once, here.

Auth. Context7 answers without a key (at lower rate limits). The key, when
there is one, rides `Authorization: Bearer <key>`:

- In a Claude cloud environment the egress proxy adds that header to every
  request for `mcp.context7.com` itself (a Network secret), and there is no
  CONTEXT7_API_KEY in the environment. So by default this module sends NO
  Authorization header of its own.
- Locally, set CONTEXT7_API_KEY (e.g. in .env) and the header is sent.

The key is read from the environment at request time and never stored,
logged, traced or written into a doc header.

Synchronous on the outside: an ADW stays plain Python. `Session` runs the
SDK's async client on a private event loop in a background thread, so one MCP
session serves a whole phase and is closed when the phase ends.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import re
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

DEFAULT_SERVER_URL = "https://mcp.context7.com/mcp"
KEY_ENV = "CONTEXT7_API_KEY"
SDK_PIN = "mcp==2.3.0"           # the ADWs with a docs phase carry this in `# /// script`

RESOLVE_TOOL = "resolve-library-id"
DOCS_TOOL = "query-docs"
LEGACY_DOCS_TOOL = "get-library-docs"   # older servers; args (context7CompatibleLibraryID, topic)

# isError texts that mean "the service refused us", not "no such library" —
# those fail the phase loudly instead of being skipped per library.
_REFUSED = re.compile(r"rate limit|quota exceeded|invalid api key|unauthori[sz]ed|"
                      r"forbidden|too many requests", re.IGNORECASE)
_NOT_FOUND = re.compile(r"not found|does not exist|no librar(y|ies) found", re.IGNORECASE)
_REDIRECT = re.compile(r"has been redirected to this library:\s*(\S+?)\.?\s*$",
                       re.IGNORECASE | re.MULTILINE)
_VERSION_NOTE = re.compile(r"^\s*Version note:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


class Context7Error(RuntimeError):
    """The service is unreachable or refused us. The phase fails on this."""


class LibraryNotFound(LookupError):
    """Context7 has no such library. Recorded and skipped, per library."""


# ── auth ─────────────────────────────────────────────────────────────────────

def api_key() -> str:
    return os.environ.get(KEY_ENV, "").strip()


def auth_headers() -> dict[str, str]:
    """`Authorization: Bearer $CONTEXT7_API_KEY` when the key is set, else nothing.

    Nothing is the cloud default: the egress proxy adds the header there.
    """
    key = api_key()
    return {"Authorization": f"Bearer {key}"} if key else {}


def redact(text: str) -> str:
    """Belt and braces for anything that ends up in a trace or an error."""
    key = api_key()
    return text.replace(key, "[CONTEXT7_API_KEY]") if key and text else text


# ── reachability (stdlib only: agents.validate imports this) ─────────────────

_PROBES: dict[str, Optional[str]] = {}


def probe(url: str = DEFAULT_SERVER_URL, timeout: float = 5.0) -> Optional[str]:
    """Can the server be reached WITHOUT a key of our own? None = yes, else why not.

    One MCP `initialize` POST. Through a proxy that injects the key this
    succeeds, and anonymous access succeeds too; a refusal (401/403/429) or a
    network failure is what makes a missing CONTEXT7_API_KEY worth a warning.
    Cached per URL for the life of the process.
    """
    if url in _PROBES:
        return _PROBES[url]
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "sssf-probe", "version": "1"}}}).encode()
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"})
    reason: Optional[str]
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            reason = None if response.status == 200 else f"HTTP {response.status}"
    except urllib.error.HTTPError as error:
        reason = f"HTTP {error.code} {error.reason}"
    except (urllib.error.URLError, OSError, ValueError) as error:
        reason = f"unreachable: {getattr(error, 'reason', error)}"
    _PROBES[url] = reason
    return reason


# ── the session ──────────────────────────────────────────────────────────────

@dataclass
class ToolText:
    """One tool call's outcome: the MCP isError flag and the joined text blocks."""

    is_error: bool
    text: str


@dataclass
class Library:
    """One `resolve-library-id` candidate."""

    id: str
    title: str = ""
    versions: list[str] = field(default_factory=list)


@dataclass
class Docs:
    """One `query-docs` answer."""

    library_id: str               # the id that answered (after any redirect)
    text: str
    version_note: str = ""


def _root_cause(error: BaseException) -> BaseException:
    """anyio task groups wrap failures in ExceptionGroups; report the real one."""
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    return error


def _describe(error: BaseException) -> str:
    error = _root_cause(error)
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    detail = f"HTTP {status}" if status else f"{type(error).__name__}: {error}"
    return redact(detail)


class Session:
    """One MCP session to a Context7 server, driven synchronously.

    The SDK's client is async and its anyio task groups must be entered and
    left by the same task, so a single coroutine owns the whole session on a
    background loop and serves calls from a queue.
    """

    def __init__(self, url: str = DEFAULT_SERVER_URL, timeout_s: float = 30.0):
        self.url = url
        self.timeout_s = float(timeout_s)
        self.tools: dict[str, dict] = {}
        self.server: str = ""
        self.calls = 0
        self._ready = threading.Event()
        self._error: Optional[BaseException] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: Optional[asyncio.Queue] = None
        self._thread = threading.Thread(target=self._thread_main, daemon=True,
                                        name="context7-mcp")

    # lifecycle
    def open(self) -> "Session":
        if self._thread.is_alive() or self._ready.is_set():
            return self                         # already open: `with open_session(...)`
        self._thread.start()
        if not self._ready.wait(self.timeout_s + 10):
            raise Context7Error(f"Context7 MCP server {self.url} did not answer within "
                                f"{self.timeout_s:.0f}s")
        if self._error is not None:
            raise Context7Error(f"cannot open an MCP session to {self.url}: "
                                f"{_describe(self._error)}") from None
        return self

    def close(self) -> None:
        if self._loop is not None and self._queue is not None and self._thread.is_alive():
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
        self._thread.join(timeout=10)

    def __enter__(self) -> "Session":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # calls
    def call(self, name: str, arguments: dict[str, Any]) -> ToolText:
        if self._loop is None or self._queue is None or not self._thread.is_alive():
            raise Context7Error(f"the MCP session to {self.url} is not open")
        future: concurrent.futures.Future = concurrent.futures.Future()
        self._loop.call_soon_threadsafe(self._queue.put_nowait, (name, arguments, future))
        self.calls += 1
        try:
            result = future.result(timeout=self.timeout_s + 10)
        except concurrent.futures.TimeoutError:
            raise Context7Error(f"{name} timed out after {self.timeout_s:.0f}s") from None
        except Exception as error:
            raise Context7Error(f"{name} failed: {_describe(error)}") from None
        text = "\n".join(getattr(block, "text", "") for block in result.content or []
                         if getattr(block, "type", "") == "text")
        is_error = bool(getattr(result, "is_error", getattr(result, "isError", False)))
        return ToolText(is_error=is_error, text=text)

    # the owning coroutine
    def _thread_main(self) -> None:
        try:
            asyncio.run(self._main())
        except BaseException as error:          # noqa: BLE001 — surfaced through open()
            self._error = self._error or error
        finally:
            self._ready.set()

    async def _main(self) -> None:
        import anyio
        import httpx2
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        timeout = httpx2.Timeout(self.timeout_s, read=max(self.timeout_s, 60.0))
        try:
            # No Authorization header unless CONTEXT7_API_KEY is set; the
            # cloud proxy supplies it for mcp.context7.com.
            async with httpx2.AsyncClient(headers=auth_headers(), timeout=timeout) as http:
                async with streamable_http_client(self.url, http_client=http) as (read, write):
                    async with ClientSession(read, write) as session:
                        with anyio.fail_after(self.timeout_s):
                            init = await session.initialize()
                            listed = await session.list_tools()
                        info = getattr(init, "server_info", None) or getattr(init, "serverInfo", None)
                        self.server = f"{getattr(info, 'name', '')} {getattr(info, 'version', '')}".strip()
                        self.tools = {t.name: (getattr(t, "input_schema", None)
                                               or getattr(t, "inputSchema", None) or {})
                                      for t in listed.tools}
                        self._ready.set()
                        await self._serve(session, anyio)
        except BaseException as error:          # noqa: BLE001
            if not self._ready.is_set():
                self._error = error
            raise
        finally:
            self._fail_pending()

    async def _serve(self, session, anyio) -> None:
        assert self._queue is not None
        while True:
            item = await self._queue.get()
            if item is None:
                return
            name, arguments, future = item
            try:
                with anyio.fail_after(self.timeout_s):
                    future.set_result(await session.call_tool(name, arguments))
            except Exception as error:          # noqa: BLE001 — handed to the caller
                future.set_exception(error)

    def _fail_pending(self) -> None:
        if self._queue is None:
            return
        while not self._queue.empty():
            item = self._queue.get_nowait()
            if item is not None and not item[2].done():
                item[2].set_exception(Context7Error("the MCP session closed"))


def open_session(cfg) -> Session:
    """One session per phase. `cfg` is a Context7Config (server_url, timeout_s)."""
    return Session(getattr(cfg, "server_url", DEFAULT_SERVER_URL),
                   getattr(cfg, "timeout_s", 30)).open()


# ── the two tools ────────────────────────────────────────────────────────────

def _refused(result: ToolText, tool: str) -> None:
    if result.is_error and _REFUSED.search(result.text):
        hint = ("" if api_key() else
                " — set CONTEXT7_API_KEY, or give the environment's proxy a secret "
                "for mcp.context7.com")
        raise Context7Error(f"Context7 refused {tool}: {redact(result.text.strip())[:300]}{hint}")


def parse_libraries(text: str) -> list[Library]:
    """`resolve-library-id` text -> candidates, in the server's ranking order."""
    libraries = []
    for block in re.split(r"^-{5,}\s*$", text, flags=re.MULTILINE):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            match = re.match(r"^\s*-\s*([^:]+):\s*(.*)$", line)
            if match:
                fields[match.group(1).strip().lower()] = match.group(2).strip()
        library_id = fields.get("context7-compatible library id", "")
        if library_id.startswith("/"):
            versions = [v.strip() for v in fields.get("versions", "").split(",") if v.strip()]
            libraries.append(Library(id=library_id, title=fields.get("title", ""),
                                     versions=versions))
    return libraries


def _norm(name: str) -> str:
    name = name.lower().split("/")[-1]
    name = re.sub(r"[^a-z0-9]", "", name)
    return name[:-2] if name.endswith("js") and len(name) > 4 else name


def _plain(version: str) -> str:
    return version.lower().lstrip("v")


def best_match(libraries: list[Library], name: str,
               version: Optional[str] = None) -> Optional[Library]:
    """The candidate that IS this library — never merely the top fuzzy hit.

    resolve-library-id always answers with something; for a name it does not
    know it returns unrelated libraries. A candidate counts only when its title
    or the last segment of its id normalizes to the package name. Among those,
    one that indexes the pinned version wins; otherwise the server's ranking.
    """
    wanted = _norm(name)
    same = [lib for lib in libraries
            if wanted and wanted in (_norm(lib.title), _norm(lib.id))]
    if version:
        for lib in same:
            if any(_plain(v) == _plain(version) for v in lib.versions):
                return lib
    return same[0] if same else None


def versioned_id(library: Library, version: Optional[str]) -> str:
    """`/org/project/<tag>` for a pinned version, using the server's own tag spelling."""
    if not version:
        return library.id
    for tag in library.versions:
        if _plain(tag) == _plain(version):
            return f"{library.id}/{tag}"
    # Not listed: ask anyway. The server serves the nearest docs with a
    # "Version note:", which the doc header records.
    return f"{library.id}/v{_plain(version)}"


def resolve(session: Session, name: str, query: str,
            version: Optional[str] = None) -> Library:
    result = session.call(RESOLVE_TOOL, {"libraryName": name, "query": query})
    _refused(result, RESOLVE_TOOL)
    if result.is_error:
        raise LibraryNotFound(result.text.strip()[:300] or f"no Context7 library for {name!r}")
    match = best_match(parse_libraries(result.text), name, version)
    if match is None:
        raise LibraryNotFound(f"no Context7 library matches {name!r}")
    return match


def query_docs(session: Session, library_id: str, query: str,
               language: Optional[str] = None) -> Docs:
    """`query-docs` for one (possibly versioned) id; follows one redirect."""
    for _ in range(2):
        if DOCS_TOOL in session.tools or not session.tools:
            tool, args = DOCS_TOOL, {"libraryId": library_id, "query": query}
            if language:
                args["language"] = language
        else:
            tool, args = LEGACY_DOCS_TOOL, {"context7CompatibleLibraryID": library_id,
                                            "topic": query}
        result = session.call(tool, args)
        _refused(result, tool)
        redirect = _REDIRECT.search(result.text) if result.is_error else None
        if redirect:
            library_id = redirect.group(1)
            continue
        if result.is_error:
            if _NOT_FOUND.search(result.text):
                raise LibraryNotFound(result.text.strip()[:300])
            raise LibraryNotFound(f"{tool} error: {result.text.strip()[:300]}")
        note = _VERSION_NOTE.search(result.text)
        return Docs(library_id=library_id, text=result.text,
                    version_note=note.group(1).strip() if note else "")
    raise LibraryNotFound(f"{library_id}: redirected more than once")
