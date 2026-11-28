"""BR-118 (F-008 lot A, AC-23 at the library level): the upstream client reaches each upstream
in the most recent generation it accepts — 2026-07-28 without a session, otherwise the handshake
(2025-11-25 proposed) with or without `Mcp-Session-Id`; an upstream without a common version
returns `upstream_unavailable`, reason "version". The client side of the access point is lot B.

A pass against the real SDK server (`mcp==2.3.0`, stateless mode) proves that the headers of
generation 2026-07-28 (BR-107 amended) are those a conforming server requires."""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import mcp_types as types
import pytest
import uvicorn
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Mount

from hullgate.upstream_mcp import MODERN, Target, UpstreamClient, UpstreamError
from tests import fake_upstream
from tests.fake_upstream import UpNet

AUTH = "Bearer tok-generations"


@pytest.fixture
def net() -> Iterator[UpNet]:
    net = fake_upstream.shared()
    net.reset()
    yield net
    net.reset()


@pytest.mark.parametrize(
    ("generation", "version", "with_session"),
    [
        ("modern", MODERN, False),
        ("session", "2025-11-25", True),
        ("stateless", "2025-11-25", False),
    ],
)
def test_br_118_each_upstream_generation_lists_and_calls(
    net: UpNet, generation: str, version: str, with_session: bool
) -> None:
    up = net.upstream(f"int-{generation}.mcp.test", generation)
    client = net.client()
    target = Target(f"http://int-{generation}.mcp.test/mcp", internal=True)
    listed = client.list_tools(("r", "c"), target, AUTH)
    assert listed.version == version
    assert [t["name"] for t in listed.tools] == ["read", "write"]
    called = client.call_tool(("r", "c"), target, AUTH, "read", {"q": 1})
    assert called.version == version
    assert called.result["content"][0]["text"] == '{"q":1}'
    session = client.session(("r", "c"))
    assert session is not None
    assert (session.session_id is not None) is with_session
    assert up.executions == {"read": 1}
    if generation == "modern":
        call = up.calls("tools/call")[-1]
        assert call.headers["mcp-method"] == "tools/call"
        assert call.headers["mcp-name"] == "read"
        assert call.headers["mcp-protocol-version"] == MODERN
        assert "mcp-session-id" not in call.headers
    else:
        # probe first, handshake next, without client capabilities (BR-121)
        methods = [r.method for r in up.calls()]
        assert methods[:3] == ["server/discover", "initialize", "notifications/initialized"]
        init = up.calls("initialize")[0].body or {}
        assert init["params"]["capabilities"] == {}
        assert init["params"]["protocolVersion"] == "2025-11-25"
        call = up.calls("tools/call")[-1]
        assert call.headers.get("mcp-protocol-version") == "2025-11-25"
        assert ("mcp-session-id" in call.headers) is with_session
        assert "mcp-method" not in call.headers


def test_br_118_upstream_without_common_version_is_unavailable(net: UpNet) -> None:
    net.upstream("int-none.mcp.test", "none")
    client = net.client()
    with pytest.raises(UpstreamError) as exc:
        client.list_tools(("r", "c"), Target("http://int-none.mcp.test/mcp", True), AUTH)
    assert (exc.value.code, exc.value.reason) == ("upstream_unavailable", "version")


def test_br_118_handshake_counter_offer_is_kept(net: UpNet) -> None:
    """An upstream that counter-proposes an older version (2024-11-05) is reached in it."""
    up = net.upstream("int-old.mcp.test", "stateless")
    net.addresses["int-old.mcp.test"] = ["10.1.1.1"]
    client = net.client()
    target = Target("http://int-old.mcp.test/mcp", True)
    original = fake_upstream.HANDSHAKE
    try:
        fake_upstream.HANDSHAKE = ("2024-11-05",)  # type: ignore[assignment]
        listed = client.list_tools(("r", "c"), target, AUTH)
    finally:
        fake_upstream.HANDSHAKE = original
    assert listed.version == "2024-11-05"
    assert "mcp-protocol-version" not in up.calls("tools/list")[-1].headers


# -- real SDK server -------------------------------------------------------------------------


def _free_port() -> int:
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.contextmanager
def sdk_server() -> Iterator[tuple[int, list[dict[str, Any]]]]:
    seen: list[dict[str, Any]] = []
    schema = {
        "type": "object",
        "properties": {
            "repo": {"type": "string", "x-mcp-header": "Repo"},
            "n": {"type": "integer"},
        },
    }

    async def list_tools(
        _ctx: ServerRequestContext[Any], _p: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[types.Tool(name="echo", description="echo", input_schema=schema)]
        )

    async def call_tool(
        _ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        seen.append(dict(params.arguments or {}))
        text = json.dumps(params.arguments, ensure_ascii=False)
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)])

    server: Server[Any] = Server(
        "sdk-upstream", version="1", on_list_tools=list_tools, on_call_tool=call_tool
    )
    manager = StreamableHTTPSessionManager(server, stateless=True, json_response=False)

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> Any:
        async with manager.run():
            yield

    app = Starlette(routes=[Mount("/mcp", app=manager.handle_request)], lifespan=lifespan)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not srv.started and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        yield port, seen
    finally:
        srv.should_exit = True
        thread.join(5)


def test_br_118_real_sdk_server_modern_with_param_headers() -> None:
    """The SDK server accepts the 2026-07-28 headers drawn from the body (including
    `Mcp-Param-Repo`, an encoded non-ASCII value), and would reject them otherwise
    (`HEADER_MISMATCH`)."""
    with sdk_server() as (port, seen):
        from hullgate.upstream_mcp import Transport

        transport = Transport(
            resolve=lambda _h, _p: ["10.20.30.40"], dial=lambda _a, _p: ("127.0.0.1", port)
        )
        client = UpstreamClient(transport)
        target = Target("http://int-sdk.mcp.test/mcp/", internal=True)
        listed = client.list_tools(("r", "c"), target, AUTH)
        assert listed.version == MODERN
        schema = listed.tools[0]["inputSchema"]
        result = client.call_tool(
            ("r", "c"), target, AUTH, "echo", {"repo": "dépôt é", "n": 3}, schema
        )
        assert result.version == MODERN
        assert seen == [{"repo": "dépôt é", "n": 3}]
        # control: no schema, hence no `Mcp-Param-Repo`: the SDK server refuses the call
        with pytest.raises(UpstreamError) as exc:
            client.call_tool(("r", "c"), target, AUTH, "echo", {"repo": "x"})
        assert (exc.value.code, exc.value.reason) == ("upstream_unavailable", "status 400")
        assert len(seen) == 1
