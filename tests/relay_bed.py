"""Bench of the relay tests at the endpoint (F-008 lot B): tree, services with a fake MCP
upstream, placed connections, operator token of the role, `tools/list` and `tools/call` requests.

Roles: A = `mpx/cairn/staging/dev`, B = `mpx/cairn/staging/rev` (not sensitive), S =
`mpx/cairn/prod/dev` (sensitive environment). Each connection has its own service and upstream
(`<prefix>.mcp.test`), hence its own journal: the proof by absence (BR-002) is read upstream by
upstream.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx2 as httpx
from starlette.testclient import TestClient

from hullgate import lifecycle
from hullgate.relay import Relay
from tests.fake_upstream import Upstream
from tests.support import MODERN_VERSION, REGISTRY, Gateway, mcp, standard_tree

A = "mpx/cairn/staging/dev"
B = "mpx/cairn/staging/rev"
S = "mpx/cairn/prod/dev"
LEGACY = "2025-06-18"


class Bed:
    def __init__(self, gw: Gateway) -> None:
        self.gw = gw
        standard_tree(gw)
        gw.build(("mpx/cairn/staging", "role", "Rev"))
        self.tokens: dict[str, str] = {}
        self.values: dict[str, str] = {}  # prefix → value of the provided token
        self.access_calls: list[tuple[str, str | None]] = []

    def token(self, role: str) -> str:
        if role not in self.tokens:
            self.tokens[role] = self.gw.issue(role)[1]
        return self.tokens[role]

    def upstream(self, prefix: str) -> Upstream:
        return self.gw.upnet.upstreams[f"{prefix}.mcp.test"]

    def connection(
        self,
        prefix: str,
        *roles: str,
        generation: str = "modern",
        oauth: bool = False,
        confirm: bool = False,
        value: str | None = None,
    ) -> Upstream:
        """Service `s<prefix>`, upstream `https://<prefix>.mcp.test/mcp`, connection
        `mpx/<prefix>` (provided token, or authorized OAuth), placed on `roles`."""
        gw = self.gw
        service = f"s{prefix}"
        up = gw.upnet.upstream(f"{prefix}.mcp.test", generation)
        name = f"mpx/{prefix}"
        if oauth:
            gw.oauth(name, prefix, service, f"app{prefix}")
        elif value is not None:  # imposed value (markers, AC-19)
            gw.service(service)
            REGISTRY.secrets.append(value)
            gw.ok("connection", "add", name, "--service", service, "--provided", "--file",
                  "-", "--prefix", prefix, stdin=value + "\n")  # fmt: skip
            self.values[prefix] = value
        else:
            gw.service(service)
            self.values[prefix] = gw.static(name, prefix, service)
        gw.ok("service", "upstream", service, "--type", "mcp-public", "--mcp-url",
              f"https://{prefix}.mcp.test/mcp")  # fmt: skip
        for role in roles:
            extra = ("--confirm-share",) if confirm else ()
            gw.ok("connection", "place", name, role, *extra)
        return up

    def spy_access(self, monkeypatch: Any) -> list[tuple[str, str | None]]:
        """Counter of the relay's `get_access` (connection, role)."""
        original = lifecycle.access

        def spy(*args: Any, **kwargs: Any) -> Any:
            self.access_calls.append((args[3], args[4]))
            return original(*args, **kwargs)

        monkeypatch.setattr(lifecycle, "access", spy)
        return self.access_calls

    @contextmanager
    def serve(self, relay: Relay | None = None) -> Iterator[TestClient]:
        with self.gw.client(relay=relay) as client:
            yield client


def names(client: TestClient, role: str, token: str, version: str = LEGACY) -> list[str]:
    response = mcp(client, role, token, "tools/list", version=version)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert "nextCursor" not in result
    return [t["name"] for t in result["tools"]]


def listing(client: TestClient, role: str, token: str) -> list[dict[str, Any]]:
    response = mcp(client, role, token, "tools/list", version=LEGACY)
    tools: list[dict[str, Any]] = response.json()["result"]["tools"]
    return tools


def call(
    client: TestClient,
    role: str,
    token: str,
    name: str,
    arguments: dict[str, Any] | None = None,
    version: str = LEGACY,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    params = {"name": name, "arguments": arguments or {}}
    if version == MODERN_VERSION:
        params["_meta"] = {
            "io.modelcontextprotocol/protocolVersion": MODERN_VERSION,
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "1"},
        }
        headers = {"Mcp-Method": "tools/call", "Mcp-Name": name, **(headers or {})}
    response: httpx.Response = mcp(client, role, token, "tools/call", params, version, headers)
    return response


def unknown(response: httpx.Response) -> bool:
    body = response.json()
    return response.status_code == 200 and body.get("error", {}).get("code") == -32602


def gateway_error(response: httpx.Response) -> str | None:
    """Closed BR-116 code of an `isError` result produced by the gateway, otherwise None."""
    result = response.json().get("result") or {}
    if result.get("isError") is not True:
        return None
    text = str(result["content"][0]["text"])
    return text.split(" ")[0]


def tool_result(response: httpx.Response) -> dict[str, Any]:
    result: dict[str, Any] = response.json()["result"]
    return result


def echoed(response: httpx.Response) -> Any:
    """Arguments returned by the fake tool (compact JSON text)."""
    return json.loads(tool_result(response)["content"][0]["text"])
