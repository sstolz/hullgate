"""BR-125 (F-008 lot A, AC-08): one outbound client per process and per use; `get_access`
never builds one, and the OAuth client's cap of 16 threads is effective."""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from hullgate import connections, outbound
from hullgate.outbound import Outbound
from hullgate.upstream_mcp import Target, Transport, UpstreamClient
from tests.fake_provider import Reply
from tests.support import Gateway, standard_tree

ROLE = "mpx/cairn/staging/dev"


def test_br_125_one_client_per_usage(gw: Gateway) -> None:
    standard_tree(gw)
    gw.service()
    gw.static("mpx/gitea-dev", "gd")
    gw.ok("connection", "place", "mpx/gitea-dev", ROLE)
    net = gw.upnet
    net.upstream("one.mcp.test", "modern")
    built_out, built_relay = Outbound.built, Transport.built
    out = gw.net.outbound()
    client = UpstreamClient(net.transport())
    key = gw.connections_key()
    cid, rid = gw.connection_id("mpx/gitea-dev"), gw.node_id(ROLE)

    async def accesses() -> None:
        for _ in range(100):
            await connections.get_access(gw.db, key, cid, rid, out=out, clock=gw.clock)

    asyncio.run(accesses())
    target = Target("https://one.mcp.test/mcp")
    for _ in range(100):
        client.call_tool((rid, cid), target, "Bearer x", "read", {})
    assert Outbound.built - built_out == 1
    assert Transport.built - built_relay == 1


def test_br_125_access_without_client_is_a_programming_error(gw: Gateway) -> None:
    standard_tree(gw)
    with pytest.raises(TypeError):
        asyncio.run(connections.get_access(gw.db, gw.connections_key(), "c", "r"))  # type: ignore[call-arg]


def test_br_125_oauth_thread_cap_is_effective(gw: Gateway) -> None:
    """50 concurrent `get_access` on 20 connections to renew: never more than 16 live OAuth
    exchange threads (a single `Outbound`, so a single cap)."""
    standard_tree(gw)
    names = [f"mpx/o{i:02d}" for i in range(20)]
    provider = None
    for i, name in enumerate(names):
        provider = gw.oauth(name, f"o{i:02d}")
        gw.ok("connection", "place", name, ROLE)
    assert provider is not None
    for name in names:
        gw.expire(name, 10)
    provider.script.extend(Reply(normal=True, delay=1.0) for _ in range(40))
    out = gw.net.outbound()
    key = gw.connections_key()
    ids = [(gw.connection_id(n), gw.node_id(ROLE)) for n in names]
    peak = 0
    done = threading.Event()

    def sample() -> None:
        nonlocal peak
        while not done.is_set():
            live = sum(1 for t in threading.enumerate() if t.name == outbound.THREAD_NAME)
            peak = max(peak, live)
            time.sleep(0.01)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()

    async def all_of_them() -> list[object]:
        # enough calling threads that only the `Outbound` cap limits the exchanges
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=64))
        calls = [
            connections.get_access(gw.db, key, cid, rid, out=out, clock=gw.clock)
            for cid, rid in (ids * 3)[:50]
        ]
        return await asyncio.gather(*calls, return_exceptions=True)

    results = asyncio.run(all_of_them())
    done.set()
    sampler.join()
    assert 0 < peak <= outbound.MAX_WORKERS == 16
    assert any(isinstance(r, connections.Access) for r in results)
    # demand exceeded the cap: exchanges were refused for want of a thread (transient)
    assert any(isinstance(r, connections.TemporarilyUnavailable) for r in results)
