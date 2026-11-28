"""HULL-85: park real ASGI requests after header checks, then change credentials.

The receive callback is the barrier: no timing sleeps or premature revocation. These convert
finding 1 / E1 and the extra parked MCP probe into refusal regressions.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from typing import Any

import anyio
import pytest
from starlette.testclient import TestClient
from starlette.types import Message, Scope

from hullgate.admin import accounts, sessions
from tests.admin_support import Console
from tests.relay_bed import A, Bed, names
from tests.support import ACCEPT, Gateway, ap


def parked(
    client: TestClient,
    method: str,
    path: str,
    headers: dict[str, str],
    body: dict[str, Any],
    change: Callable[[], object],
) -> tuple[int, dict[str, Any]]:
    """Deliver headers, wait in receive while a separate worker changes state, then deliver body."""
    messages: list[Message] = []
    supplied = False

    async def receive() -> dict[str, Any]:
        nonlocal supplied
        if supplied:
            await anyio.sleep_forever()
        supplied = True
        await anyio.to_thread.run_sync(change)
        return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}

    async def send(message: Message) -> None:
        messages.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "server": ("localhost", 80),
        "client": ("127.0.0.1", 50000),
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    assert client.portal is not None
    client.portal.call(functools.partial(client.app, scope, receive, send))
    start = next(m for m in messages if m["type"] == "http.response.start")
    raw = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return start["status"], json.loads(raw)


@pytest.mark.parametrize("event", ["signout", "fresh_lapsed", "idle_expiry", "absolute_expiry"])
@pytest.mark.parametrize("valid_body", [True, False])
def test_admin_parked_allowlist_is_refused(gw: Gateway, event: str, valid_body: bool) -> None:
    console = Console(gw)
    with console.client() as client:
        key = console.admin(client)
        assert console.fresh(client, key).status_code == 200
        cookie = client.cookies.get(sessions.COOKIE)
        assert cookie is not None

        def change() -> None:
            if event == "signout":
                assert console.call(client, "POST", "session/signout", {}).status_code == 200
            else:
                advance = {
                    "fresh_lapsed": sessions.FRESH_S + 1,
                    "idle_expiry": sessions.IDLE_S,
                    "absolute_expiry": sessions.ABSOLUTE_S + 3600,
                }[event]
                gw.clock.advance(advance)
                if event == "absolute_expiry":
                    # Keep idle/freshness live to isolate the absolute expiry check.
                    gw.db.call(
                        lambda c: c.execute(
                            "UPDATE admin_session SET last_activity_at = ?, fresh_until = ?",
                            (int(gw.clock()), int(gw.clock()) + 300),
                        )
                    )

        status, body = parked(
            client,
            "PUT",
            "/admin/api/v1/allowlist",
            {
                **console.headers(activity=False),
                "Host": console.origin.host + ":" + str(console.origin.port),
                "Cookie": f"{sessions.COOKIE}={cookie}",
            },
            # Invalid handler input must still lose to the post-body auth check (review N1).
            {"cidrs": ["127.0.0.0/8"]} if valid_body else {"cidrs": "invalid"},
            change,
        )
    assert (status, body.get("code")) == (
        (403, "fresh_auth_required") if event == "fresh_lapsed" else (401, "unauthenticated")
    )
    assert gw.db.call(accounts.allowlist) == []
    assert not [e for e in gw.audit() if e["action"] == "admin.allowlist"]


@pytest.mark.parametrize("event", ["revoked", "expired", "placement"])
def test_mcp_parked_tool_call_is_refused(gw: Gateway, event: str) -> None:
    bed = Bed(gw)
    up = bed.connection("pk", A)
    token_id, token = gw.issue("mpx/cairn/staging" if event == "placement" else A, "parked")
    with bed.serve() as client:
        assert "pk__write" in names(client, A, token)
        before = len(up.calls("tools/call"))

        def change() -> None:
            if event == "revoked":
                gw.ok("token", "revoke", token_id)
            elif event == "expired":
                gw.clock.advance(31 * 86400)
            else:
                gw.ok("tree", "sensitive", "mpx/cairn/staging", "on")

        status, body = parked(
            client,
            "POST",
            ap(A),
            {
                "Authorization": f"Bearer {token}",
                "Accept": ACCEPT,
                "Content-Type": "application/json",
                "MCP-Protocol-Version": "2025-06-18",
            },
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {"name": "pk__write", "arguments": {}},
            },
            change,
        )
    assert status == 401, body
    assert body["error"] == "invalid_token"
    assert len(up.calls("tools/call")) == before
    refusals = [e for e in gw.audit() if e["action"] == "access.refused"]
    assert len(refusals) == 1
    assert (
        refusals[0]["reason"]
        == {
            "revoked": "revoked",
            "expired": "invalid_token",
            "placement": "sensitive_requires_role_token",
        }[event]
    )


@pytest.mark.parametrize("route", ["allowlist", "setup", "nodes"])
@pytest.mark.parametrize("event", ["signout", "expiry"])
def test_admin_rechecks_inside_mutation_transaction(
    gw: Gateway,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    event: str,
) -> None:
    from hullgate.admin.app import AdminApi
    from hullgate.admin.console import ConsoleApi
    from hullgate.admin.organisation import OrganisationApi

    cases: dict[str, tuple[Any, str, str, str, dict[str, Any]]] = {
        "allowlist": (AdminApi, "allowlist_put", "PUT", "allowlist", {"cidrs": ["127.0.0.0/8"]}),
        "setup": (OrganisationApi, "dismiss", "POST", "setup/dismiss", {}),
        "nodes": (
            ConsoleApi,
            "nodes_add",
            "POST",
            "nodes",
            {"parent": "/", "level": "client", "name": "Late"},
        ),
    }
    owner, method, verb, path, body = cases[route]
    original = getattr(owner, method)
    checked: list[bool] = []
    resolve = sessions.resolve

    def spy(conn: Any, *args: Any, **kwargs: Any) -> Any:
        checked.append(conn.in_transaction)
        return resolve(conn, *args, **kwargs)

    async def delayed(self: Any, ctx: Any) -> Any:
        # The body and the entry recheck have completed. Change state before the write lock.
        if event == "signout":
            gw.db.call(lambda c: sessions.close(c, ctx.live.digest))
        else:
            gw.clock.advance(sessions.FRESH_S + 1 if route == "allowlist" else sessions.IDLE_S)
        return await original(self, ctx)

    monkeypatch.setattr(owner, method, delayed)
    console = Console(gw)
    with console.client() as client:
        key = console.admin(client)
        assert console.fresh(client, key).status_code == 200
        monkeypatch.setattr(sessions, "resolve", spy)
        response = console.call(client, verb, path, body)
    assert response.status_code == (403 if event == "expiry" and route == "allowlist" else 401)
    assert True in checked, "authorization must be resolved with the mutation's write lock held"
    assert gw.db.call(accounts.allowlist) == []
    assert gw.db.call(lambda c: c.execute("SELECT dismissed FROM admin_setup").fetchone()[0]) == 0
    assert (
        gw.db.call(
            lambda c: c.execute("SELECT count(*) FROM node WHERE name = 'Late'").fetchone()[0]
        )
        == 0
    )
