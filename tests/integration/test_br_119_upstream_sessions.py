"""BR-119 (F-008 lot A, AC-24 at the library level): the upstream session is held per (role,
connection), reopens once without executing the call twice, is never shared, and closes when the
database no longer carries it — checked at every request, whatever its key."""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest

from hullgate import lifecycle, relay
from hullgate.upstream_mcp import SESSION_IDLE_S, Target, UpstreamClient
from tests import fake_upstream
from tests.fake_upstream import FakeClock, Fault, UpNet
from tests.support import Gateway, standard_tree

AUTH = "Bearer tok-sessions"
TARGET = Target("http://int-s.mcp.test/mcp", internal=True)


@pytest.fixture
def net() -> Iterator[UpNet]:
    net = fake_upstream.shared()
    net.reset()
    net.upstream("int-s.mcp.test", "session")
    yield net
    net.reset()


def test_br_119_expired_session_reopens_once_without_double_execution(net: UpNet) -> None:
    up = net.upstreams["int-s.mcp.test"]
    client = net.client()
    client.list_tools(("r", "c"), TARGET, AUTH)
    up.faults.append(Fault("expire", on="tools/call"))
    result = client.call_tool(("r", "c"), TARGET, AUTH, "write", {})
    assert result.result["isError"] is False
    assert len(up.calls("initialize")) == 2  # one reopening
    assert up.executions == {"write": 1}


def test_br_119_concurrent_calls_share_one_reopen(net: UpNet) -> None:
    up = net.upstreams["int-s.mcp.test"]
    client = net.client()
    client.list_tools(("r", "c"), TARGET, AUTH)
    with up.lock:
        up.sessions.clear()  # the session expires with 4 calls in flight
    results: list[object] = []
    barrier = threading.Barrier(4)

    def call() -> None:
        barrier.wait()
        results.append(client.call_tool(("r", "c"), TARGET, AUTH, "read", {}))

    threads = [threading.Thread(target=call) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(results) == 4
    assert len(up.calls("initialize")) == 2
    assert up.executions == {"read": 4}


def test_br_119_two_roles_two_sessions(net: UpNet) -> None:
    up = net.upstreams["int-s.mcp.test"]
    client = net.client()
    client.list_tools(("a", "c"), TARGET, AUTH)
    client.list_tools(("b", "c"), TARGET, AUTH)
    a, b = client.session(("a", "c")), client.session(("b", "c"))
    assert a is not None
    assert b is not None
    assert a.session_id != b.session_id
    assert len(up.calls("initialize")) == 2


def test_br_119_idle_sessions_are_closed_after_15_min(net: UpNet) -> None:
    up = net.upstreams["int-s.mcp.test"]
    clock = FakeClock()
    client = net.client(clock)
    client.list_tools(("a", "c"), TARGET, AUTH)
    clock.advance(SESSION_IDLE_S + 1)
    client.list_tools(("b", "c"), TARGET, AUTH)  # any request sweeps
    assert [k for k, _ in client.held()] == [("b", "c")]
    _wait_delete(up, 1)


def _wait_delete(up: fake_upstream.Upstream, n: int) -> None:
    for _ in range(100):
        if len(up.calls(None)) and len([r for r in up.calls() if r.http == "DELETE"]) >= n:
            return
        threading.Event().wait(0.02)
    raise AssertionError("end of session not received by the upstream")


def test_br_119_role_deletion_closes_sessions_at_any_request(gw: Gateway) -> None:
    """Removal of a role, then a request on another role: the removed role's session is closed
    (end received by the upstream), without an injected clock."""
    net = gw.upnet
    up = net.upstream("int-s.mcp.test", "session")
    standard_tree(gw)
    gw.ok("tree", "add", "mpx/cairn/staging", "role", "Ops")
    gw.service()
    gw.ok("service", "upstream", "gitea-dev", "--type", "mcp-public", "--mcp-url", TARGET.url,
          "--mcp-internal-host")  # fmt: skip
    gw.static("mpx/gitea-dev", "gd")
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/staging/dev")
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/staging/ops")
    cid = gw.connection_id("mpx/gitea-dev")
    dev, ops = gw.node_id("mpx/cairn/staging/dev"), gw.node_id("mpx/cairn/staging/ops")
    client: UpstreamClient = net.client()
    client.list_tools((dev, cid), TARGET, AUTH)
    client.list_tools((ops, cid), TARGET, AUTH)
    conn = gw.connect()
    run = lifecycle.Direct(conn)
    try:
        assert relay.reconcile(run, client) == []
        gw.ok("tree", "delete", "mpx/cairn/staging/ops", "--confirm", "mpx/cairn/staging/ops")
        client.list_tools((dev, cid), TARGET, AUTH)  # request on another role
        assert relay.reconcile(run, client) == [(ops, cid)]
    finally:
        conn.close()
    assert [k for k, _ in client.held()] == [(dev, cid)]
    _wait_delete(up, 1)


@pytest.mark.parametrize("change", ["unplace", "revoke", "url", "remove-upstream"])
def test_br_119_database_changes_close_sessions(gw: Gateway, change: str) -> None:
    net = gw.upnet
    net.upstream("int-s.mcp.test", "session")
    net.upstream("int-t.mcp.test", "session")
    standard_tree(gw)
    gw.service()
    gw.ok("service", "upstream", "gitea-dev", "--type", "mcp-public", "--mcp-url", TARGET.url,
          "--mcp-internal-host")  # fmt: skip
    gw.static("mpx/gitea-dev", "gd")
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/staging/dev")
    cid, dev = gw.connection_id("mpx/gitea-dev"), gw.node_id("mpx/cairn/staging/dev")
    client = net.client()
    client.list_tools((dev, cid), TARGET, AUTH)
    if change == "unplace":
        gw.ok("connection", "unplace", "mpx/gitea-dev", "mpx/cairn/staging/dev")
    elif change == "revoke":
        conn = gw.connect()
        conn.execute("UPDATE connection SET state = 'reauth_required' WHERE id = ?", (cid,))
        conn.commit()
        conn.close()
    elif change == "url":
        other = "http://int-t.mcp.test/mcp"
        gw.ok("service", "upstream", "gitea-dev", "--type", "mcp-public", "--mcp-url", other,
              "--mcp-internal-host", "--confirm", "gitea-dev")  # fmt: skip
    else:
        gw.ok("service", "upstream", "gitea-dev", "--remove", "--confirm", "gitea-dev")
    conn = gw.connect()
    try:
        assert relay.reconcile(lifecycle.Direct(conn), client) == [(dev, cid)]
    finally:
        conn.close()


def test_br_119_at_most_64_sessions_per_connection(net: UpNet) -> None:
    """Review 448, m7: a connection's 65th session closes the oldest (`DELETE` received)."""
    from hullgate.upstream_mcp import SESSIONS_PER_CONNECTION

    up = net.upstreams["int-s.mcp.test"]
    clock = FakeClock()
    client = net.client(clock)
    for i in range(SESSIONS_PER_CONNECTION + 1):
        clock.advance(1)
        client.list_tools((f"r{i:02d}", "c"), TARGET, AUTH)
    held = {k for k, _ in client.held()}
    assert len(held) == SESSIONS_PER_CONNECTION == 64
    assert ("r00", "c") not in held
    first = up.calls("tools/list")[0].headers["mcp-session-id"]
    for _ in range(100):
        if any(r.http == "DELETE" and r.headers.get("mcp-session-id") == first for r in up.calls()):
            return
        threading.Event().wait(0.02)
    raise AssertionError("end of the oldest session not received")
