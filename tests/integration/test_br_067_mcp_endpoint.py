"""BR-067 — the MCP endpoint of a role serves the protocol stateless, five versions; v2 (F-008):
the tools of the connections placed on the role are relayed."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from starlette.testclient import TestClient

from tests.support import HANDSHAKE_VERSIONS, MODERN_VERSION, Gateway, mcp, standard_tree

ROLE = "mpx/cairn/staging/dev"
ALL_VERSIONS = [*HANDSHAKE_VERSIONS, MODERN_VERSION]


@pytest.fixture
def served(gw: Gateway) -> Iterator[tuple[TestClient, str]]:
    standard_tree(gw)
    _, token = gw.issue(ROLE)
    with gw.client() as client:
        yield client, token


def _no_session(response: httpx.Response) -> bool:
    return "mcp-session-id" not in response.headers


@pytest.mark.parametrize("version", HANDSHAKE_VERSIONS)
def test_br_067_handshake_versions(served: tuple[TestClient, str], version: str) -> None:
    client, token = served
    init = mcp(
        client,
        ROLE,
        token,
        "initialize",
        {
            "protocolVersion": version,
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        },
        version=version,
    )
    assert init.status_code == 200
    result = init.json()["result"]
    assert result["protocolVersion"] == version
    assert result["serverInfo"]["name"] == "hullgate"
    ping = mcp(client, ROLE, token, "ping", version=version)
    assert (ping.status_code, ping.json()["result"]) == (200, {})
    tools = mcp(client, ROLE, token, "tools/list", version=version)
    assert tools.json()["result"]["tools"] == []
    call = mcp(
        client, ROLE, token, "tools/call", {"name": "whoami", "arguments": {}}, version=version
    )
    error = call.json()["error"]
    assert error["code"] == -32602
    assert "Unknown tool: whoami" in error["message"]
    assert all(_no_session(r) for r in (init, ping, tools, call))


def _modern(
    client: TestClient, token: str, method: str, params: dict[str, object]
) -> httpx.Response:
    meta = {
        "io.modelcontextprotocol/protocolVersion": MODERN_VERSION,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "1"},
    }
    headers = {"Mcp-Method": method}
    if "name" in params:
        headers["Mcp-Name"] = str(params["name"])
    response: httpx.Response = mcp(
        client, ROLE, token, method, {**params, "_meta": meta}, MODERN_VERSION, headers
    )
    return response


def test_br_067_modern_version_without_session(served: tuple[TestClient, str]) -> None:
    client, token = served
    discover = _modern(client, token, "server/discover", {})
    tools = _modern(client, token, "tools/list", {})
    call = _modern(client, token, "tools/call", {"name": "whoami", "arguments": {}})
    assert discover.status_code == 200
    assert MODERN_VERSION in discover.json()["result"]["supportedVersions"]
    assert tools.json()["result"]["tools"] == []
    error = call.json()["error"]
    assert "Unknown tool: whoami" in error["message"]
    assert all(_no_session(r) for r in (discover, tools, call))


def test_br_067_unsupported_version_lists_the_five(served: tuple[TestClient, str]) -> None:
    client, token = served
    response = mcp(client, ROLE, token, "tools/list", version="2099-01-01")
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["data"]["supported"] == ALL_VERSIONS
    assert response.json()["id"] == 1


def test_br_067_unsupported_version_checked_after_authorization(
    served: tuple[TestClient, str],
) -> None:
    client, _ = served
    assert mcp(client, ROLE, None, "tools/list", version="2099-01-01").status_code == 401


@pytest.mark.parametrize("version", ALL_VERSIONS)
def test_br_067_v2_relayed_tools_in_every_version(gw: Gateway, version: str) -> None:
    """BR-067 v2 (F-008, AC-30): the list is no longer empty when a connection is placed; the
    five versions are still served stateless."""
    from tests.relay_bed import A, Bed, call, gateway_error

    bed = Bed(gw)
    bed.connection("vv", A)
    with bed.serve() as client:
        token = bed.token(A)
        if version == MODERN_VERSION:
            listed = _modern(client, token, "tools/list", {})
        else:
            listed = mcp(client, A, token, "tools/list", version=version)
        assert [t["name"] for t in listed.json()["result"]["tools"]] == ["vv__read", "vv__write"]
        called = call(client, A, token, "vv__read", None, version)
        assert "mcp-session-id" not in called.headers
    assert gateway_error(called) is None
    assert _no_session(listed)
