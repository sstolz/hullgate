"""BR-108 (F-008 lot A, AC-07): at most 4 simultaneous exchanges per connection and 32 in total,
in a set of threads of the relay's own, apart from the OAuth client; beyond, `upstream_busy` at
once, with no exchange, with no "not before" date."""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Iterator

import pytest

from hullgate.upstream_mcp import (
    PER_CONNECTION,
    THREAD_NAME,
    TOTAL,
    Target,
    UpstreamClient,
    UpstreamError,
)
from tests import fake_upstream
from tests.fake_upstream import Fault, UpNet
from tests.support import Gateway, standard_tree

AUTH = "Bearer tok-concurrence"
TARGET = Target("https://busy.mcp.test/mcp")
ROLE = "mpx/cairn/staging/dev"
NAME = "mpx/gitea-dev"


@pytest.fixture
def net() -> Iterator[UpNet]:
    net = fake_upstream.shared()
    net.reset()
    net.upstream("busy.mcp.test", "modern")
    yield net
    net.stop.set()
    time.sleep(0.3)
    net.stop.clear()
    net.reset()


def _relay_threads() -> int:
    return sum(1 for t in threading.enumerate() if t.name == THREAD_NAME)


def _saturate(net: UpNet, client: UpstreamClient) -> list[threading.Thread]:
    """32 pending calls: 8 connections × 4."""
    up = net.upstreams["busy.mcp.test"]
    keys = [("r", f"c{i}") for i in range(TOTAL // PER_CONNECTION)]
    for key in keys:
        client.list_tools(key, TARGET, AUTH)
    up.faults.extend(Fault("hang", on="tools/call", seconds=60) for _ in range(TOTAL))
    threads = []
    for key in keys:
        for _ in range(PER_CONNECTION):
            t = threading.Thread(
                target=lambda k=key: _quiet(client, k), name="test-caller", daemon=True
            )
            t.start()
            threads.append(t)
    deadline = time.monotonic() + 10
    while len(up.calls("tools/call")) < TOTAL and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(up.calls("tools/call")) == TOTAL
    return threads


def _quiet(client: UpstreamClient, key: tuple[str, str]) -> None:
    with contextlib.suppress(UpstreamError):
        client.call_tool(key, TARGET, AUTH, "read", {})


def test_br_108_saturation_refuses_at_once_without_exchange(net: UpNet) -> None:
    client = net.client()
    up = net.upstreams["busy.mcp.test"]
    client.list_tools(("r", "extra"), TARGET, AUTH)  # session of the 9th connection, beforehand
    _saturate(net, client)
    assert _relay_threads() <= TOTAL
    before = len(up.calls())
    timings: list[float] = []
    for _ in range(5):
        for key in (("r", "extra"), ("r", "c0")):  # 33rd in total, 5th of one connection
            started = time.monotonic()
            with pytest.raises(UpstreamError) as exc:
                client.call_tool(key, TARGET, AUTH, "read", {})
            timings.append(time.monotonic() - started)
            assert exc.value.code == "upstream_busy"
            assert exc.value.retry_after is None
            assert _relay_threads() <= TOTAL
    assert max(timings) < 0.1
    assert len(up.calls()) == before  # no exchange
    client.transport.pacer.check("extra")  # no "not before" date set
    client.transport.pacer.check("c0")


def test_br_108_oauth_renewal_is_not_starved(gw: Gateway) -> None:
    """While the relay is saturated, an OAuth renewal completes in under 2 s."""
    net = gw.upnet
    net.upstream("busy.mcp.test", "modern")
    standard_tree(gw)
    gw.oauth()
    gw.ok("connection", "place", NAME, ROLE)
    client = net.client()
    try:
        _saturate(net, client)
        for _ in range(5):  # timing measured over 5 runs (AC-07; review 448, m5)
            gw.expire(NAME, 10)
            before = gw.row(NAME)["generation"]
            started = time.monotonic()
            got = gw.access(NAME, ROLE)
            assert time.monotonic() - started < 2.0
            assert got.generation == before + 1
            assert _relay_threads() <= TOTAL
    finally:
        net.stop.set()
        time.sleep(0.3)
        net.stop.clear()
