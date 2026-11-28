"""BR-124 (F-008 lot A, AC-22): a 429 from the upstream sets a "not before" date per
connection — `Retry-After`, otherwise fallback 1 s, 2 s, … bounded to 60 s and reset by a
success — shared by the roles, with no effect on the OAuth renewal; before it, `upstream_busy`
with no exchange."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from hullgate.upstream_mcp import Target, UpstreamClient, UpstreamError
from tests import fake_upstream
from tests.fake_upstream import FakeClock, Fault, UpNet
from tests.support import Gateway, standard_tree

AUTH = "Bearer tok-429"
TARGET = Target("https://rl.mcp.test/mcp")


@pytest.fixture
def net() -> Iterator[UpNet]:
    net = fake_upstream.shared()
    net.reset()
    net.upstream("rl.mcp.test", "modern")
    yield net
    net.reset()


def _busy(client: UpstreamClient, key: tuple[str, str]) -> UpstreamError:
    with pytest.raises(UpstreamError) as exc:
        client.call_tool(key, TARGET, AUTH, "read", {})
    assert exc.value.code == "upstream_busy"
    return exc.value


def test_br_124_retry_after_holds_the_connection_for_every_role(net: UpNet) -> None:
    up = net.upstreams["rl.mcp.test"]
    clock = FakeClock()
    client = net.client(clock)
    client.list_tools(("a", "c"), TARGET, AUTH)
    client.list_tools(("b", "c"), TARGET, AUTH)
    up.faults.append(Fault("status", on="tools/call", status=429, headers={"Retry-After": "5"}))
    assert _busy(client, ("a", "c")).retry_after == 5
    sent = len(up.calls())
    clock.advance(4)
    for key in (("a", "c"), ("b", "c")):  # two roles that share the connection
        err = _busy(client, key)
        assert err.retry_after == pytest.approx(1)
    assert len(up.calls()) == sent  # no exchange before the date
    clock.advance(1)
    client.call_tool(("b", "c"), TARGET, AUTH, "read", {})
    client.list_tools(("a", "other"), TARGET, AUTH)  # another connection is not touched


def test_br_124_backoff_without_retry_after_is_bounded_and_reset(net: UpNet) -> None:
    up = net.upstreams["rl.mcp.test"]
    clock = FakeClock()
    client = net.client(clock)
    client.list_tools(("a", "c"), TARGET, AUTH)
    delays = []
    for _ in range(8):
        up.faults.append(Fault("status", on="tools/call", status=429))
        err = _busy(client, ("a", "c"))
        delays.append(err.retry_after)
        clock.advance(float(err.retry_after or 0))
    assert delays == [1, 2, 4, 8, 16, 32, 60, 60]
    client.call_tool(("a", "c"), TARGET, AUTH, "read", {})  # success: reset
    up.faults.append(Fault("status", on="tools/call", status=429))
    assert _busy(client, ("a", "c")).retry_after == 1


def test_br_124_oauth_renewal_is_not_delayed(gw: Gateway) -> None:
    """The date of a 429 from the MCP upstream does not act on the OAuth token renewal."""
    standard_tree(gw)
    gw.oauth()
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/staging/dev")
    net = gw.upnet
    up = net.upstream("rl.mcp.test", "modern")
    cid = gw.connection_id("mpx/gitea-dev")
    clock = FakeClock()
    client = net.client(clock)
    client.list_tools(("r", cid), TARGET, AUTH)
    up.faults.append(Fault("status", on="tools/call", status=429, headers={"Retry-After": "600"}))
    _busy(client, ("r", cid))
    gw.expire("mpx/gitea-dev", 10)
    generation = gw.row("mpx/gitea-dev")["generation"]
    assert gw.access("mpx/gitea-dev", "mpx/cairn/staging/dev").generation == generation + 1
    assert gw.row("mpx/gitea-dev")["not_before"] is None
