"""A local stand-in for the Context7 MCP server, built from the MCP SDK's own
server classes and served over streamable HTTP on 127.0.0.1.

It speaks the live server's contract (recorded from Context7 4.5.0):
`resolve-library-id(libraryName, query)` and `query-docs(libraryId, query,
language?)`, plain-text results in the same layout, a "Version note:" for an
unindexed version, an isError redirect, and an isError "not found".

Every HTTP request's Authorization header is recorded in `server.auth_seen`,
so tests can prove what the client did (and did not) send.
"""

from __future__ import annotations

import socket
import threading
import time

LIBRARIES = {
    "fastapi": [("FastAPI", "/websites/fastapi_tiangolo", []),
                ("FastAPI Users", "/fastapi-users/fastapi-users", [])],
    "pydantic": [("Pydantic", "/pydantic/pydantic", ["v2.9.2", "v2.11.0", "v2.14.1"]),
                 ("Pydantic Settings", "/pydantic/pydantic-settings", [])],
    "rich": [("Rich", "/textualize/rich", [])],
}
REDIRECTS = {"/fastapi/fastapi": "/websites/fastapi_tiangolo"}


def _resolve_text(name: str) -> str:
    blocks = []
    for title, library_id, versions in LIBRARIES.get(name.lower(), [("Unrelated", "/acme/unrelated", [])]):
        lines = [f"- Title: {title}", f"- Context7-compatible library ID: {library_id}",
                 "- Description: test library", "- Code Snippets: 10",
                 "- Source Reputation: High", "- Benchmark Score: 70"]
        if versions:
            lines.append(f"- Versions: {', '.join(versions)}")
        blocks.append("\n".join(lines))
    return "Available Libraries:\n\n" + "\n----------\n".join(blocks)


def build_app(server: "FakeContext7"):
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError   # its text reaches the client

    mcp = MCPServer("Context7", version="4.5.0-fake")

    @mcp.tool(name="resolve-library-id")
    def resolve_library_id(query: str, libraryName: str) -> str:   # noqa: N803 — the wire names
        server.calls.append(("resolve-library-id", libraryName))
        return _resolve_text(libraryName)

    @mcp.tool(name="query-docs")
    def query_docs(libraryId: str, query: str, language: str | None = None) -> str:  # noqa: N803
        server.calls.append(("query-docs", libraryId))
        if libraryId in REDIRECTS:
            raise ToolError(f"Library {libraryId} has been redirected to this library: "
                             f"{REDIRECTS[libraryId]}.")
        if libraryId.startswith("/acme/"):
            raise ToolError(f'Library "{libraryId}" not found. Please check the library ID.')
        base, _, tag = libraryId.partition("/v")
        note = ""
        if tag and "pydantic" in base and f"v{tag}" not in ("v2.9.2", "v2.11.0", "v2.14.1"):
            note = (f"Version note: {base} has no indexed documentation for version v{tag}; "
                    f"v2.14.1, the newest indexed release of that line, is served instead.\n\n")
        body = server.doc_body or f"### {libraryId}\n\nUse `{query[:40]}` like this.\n"
        return note + body

    app = mcp.streamable_http_app(stateless_http=True)

    async def recording(scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            server.auth_seen.append((headers.get(b"authorization") or b"").decode() or None)
        await app(scope, receive, send)

    return recording


class FakeContext7:
    def __init__(self) -> None:
        self.auth_seen: list = []
        self.calls: list = []
        self.doc_body = ""
        self.port = 0
        self._server = None
        self._thread = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def start(self) -> "FakeContext7":
        import uvicorn

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        config = uvicorn.Config(build_app(self), host="127.0.0.1", port=self.port,
                                log_level="warning", lifespan="on")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 15
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("fake Context7 server did not start")
            time.sleep(0.05)
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
            self._thread.join(timeout=10)
