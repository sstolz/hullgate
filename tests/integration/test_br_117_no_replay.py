"""BR-117, BR-116 (F-008 lot A, AC-21 at the library level): a call whose request was sent
without a whole response is never replayed — `deadline_exceeded` at the deadline,
`uncertain_outcome` on a cut before it; a refused connection returns `upstream_unavailable`
without a request. The reason is measured over 5 executions (lesson of #36)."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest

from hullgate.upstream_mcp import Target, UpstreamError
from tests import fake_upstream
from tests.fake_upstream import FakeClock, Fault, UpNet

AUTH = "Bearer tok-replay"
TARGET = Target("https://nr.mcp.test/mcp")


@pytest.fixture
def net() -> Iterator[UpNet]:
    net = fake_upstream.shared()
    net.reset()
    net.upstream("nr.mcp.test", "modern")
    yield net
    net.stop.set()
    net.stop.clear()
    net.reset()


def _reached(up: fake_upstream.Upstream, n: int) -> Callable[[], bool]:
    return lambda: len(up.calls("tools/call")) >= n


@pytest.mark.parametrize(
    ("kind", "code"), [("hang", "deadline_exceeded"), ("cut", "uncertain_outcome")]
)
def test_br_117_uncertain_call_is_never_replayed(net: UpNet, kind: str, code: str) -> None:
    up = net.upstreams["nr.mcp.test"]
    for _ in range(5):
        clock = FakeClock()
        client = net.client(clock)
        client.list_tools(("r", "c"), TARGET, AUTH)
        before = len(up.calls("tools/call"))
        up.faults.append(Fault(kind, on="tools/call", seconds=5))
        if kind == "hang":
            clock.expect_hang(_reached(up, before + 1))
        with pytest.raises(UpstreamError) as exc:
            client.call_tool(("r", "c"), TARGET, AUTH, "write", {"x": 1})
        assert exc.value.code == code
        assert len(up.calls("tools/call")) == before + 1  # exactly one request
        net.stop.set()
        net.stop.clear()


def test_br_117_refused_connection_sends_nothing(net: UpNet) -> None:
    up = net.upstreams["nr.mcp.test"]
    for _ in range(5):
        client = net.client()
        client.list_tools(("r", "c"), TARGET, AUTH)
        before = len(up.calls("tools/call"))
        net.refused_ports.add(443)
        with pytest.raises(UpstreamError) as exc:
            client.call_tool(("r", "c"), TARGET, AUTH, "write", {})
        net.refused_ports.clear()
        assert exc.value.code == "upstream_unavailable"
        assert len(up.calls("tools/call")) == before
