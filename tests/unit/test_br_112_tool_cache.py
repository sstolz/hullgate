"""BR-112 (F-008 lot B, AC-13): definitions are cached per (role, connection), one single fetch
per cold key, and any entry is discarded as soon as the database (written by the CLI, another
process) says otherwise: placement, sensitivity, URL, state; the authorization of a call is read
again from the database at each request."""

from __future__ import annotations

import threading

import pytest

from tests.fake_upstream import Fault
from tests.relay_bed import A, B, Bed, call, names, unknown
from tests.support import Gateway, mcp


@pytest.fixture
def bed(gw: Gateway) -> Bed:
    return Bed(gw)


def test_br_112_one_cache_entry_per_role(bed: Bed) -> None:
    up = bed.connection("sh", A, B)
    relay = bed.gw.relay()
    with bed.serve(relay) as client:
        names(client, A, bed.token(A))
        names(client, B, bed.token(B))
        names(client, A, bed.token(A))
    assert len(up.calls("tools/list")) == 2
    assert len(relay.cached()) == 2


def test_br_112_concurrent_cold_lists_fetch_once(bed: Bed) -> None:
    up = bed.connection("cc", A)
    up.faults.append(Fault("delay", on="tools/list", seconds=0.5))
    with bed.serve() as client:
        token = bed.token(A)
        barrier = threading.Barrier(20)
        got: list[list[str]] = []

        def one() -> None:
            barrier.wait()
            got.append(names(client, A, token))

        threads = [threading.Thread(target=one) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
    assert len(got) == 20
    assert all(g == ["cc__read", "cc__write"] for g in got)
    assert len(up.calls("tools/list")) == 1


@pytest.mark.parametrize("change", ["unplace", "sensitive", "url", "state"])
def test_br_112_cli_writes_discard_the_warm_entry(bed: Bed, change: str) -> None:
    gw = bed.gw
    up = bed.connection("wc", A)
    relay = bed.gw.relay()
    key = (gw.node_id(A), gw.connection_id("mpx/wc"))
    with bed.serve(relay) as client:
        token = bed.token(A)
        assert names(client, A, token) == ["wc__read", "wc__write"]
        assert key in relay.cached()  # witness that the warm entry is present
        lists = len(up.calls("tools/list"))
        if change == "unplace":
            gw.ok("connection", "unplace", "mpx/wc", A)
        elif change == "sensitive":
            gw.ok("tree", "sensitive", "mpx/cairn/staging", "on")
        elif change == "url":
            gw.upnet.upstream("wd.mcp.test", "modern")
            gw.ok("service", "upstream", "swc", "--type", "mcp-public", "--mcp-url",
                  "https://wd.mcp.test/mcp", "--confirm", "swc")  # fmt: skip
        else:
            gw.ok("connection", "revoke", "mpx/wc")
        # the entry itself is discarded at the next request served, whatever it is (review
        # 462, m2: the empty list and the refusal also come from reading the database again)
        assert mcp(client, A, token, "ping").status_code == 200
        assert key not in relay.cached()
        listed = names(client, A, token) if change != "sensitive" else None
        response = call(client, A, token, "wc__read")
    if change == "url":
        assert listed == ["wc__read", "wc__write"]  # read again on the new URL, same names
        assert len(up.calls("tools/list")) == lists
        assert gw.upnet.upstreams["wd.mcp.test"].calls("tools/list")
    else:
        assert listed in (None, [])
        assert unknown(response)
        assert up.calls("tools/call") == []
        # F-015 BR-175: a sensitive placement reaches stage 2 of BR-113, a fresh listing
        assert len(up.calls("tools/list")) == lists + (1 if change == "sensitive" else 0)


def test_br_112_cli_tools_command_reads_outside_the_cache(bed: Bed) -> None:
    up = bed.connection("oc", A)
    with bed.serve() as client:
        names(client, A, bed.token(A))
    lists = len(up.calls("tools/list"))
    bed.gw.ok("connection", "tools", "mpx/oc", "--role", A)
    assert len(up.calls("tools/list")) == lists + 1


def test_br_112_authorization_is_reread_not_cached(bed: Bed) -> None:
    """A warm entry authorizes nothing: role B with no placement, name of a tool of A."""
    up = bed.connection("ar", A)
    with bed.serve() as client:
        names(client, A, bed.token(A))
        r = mcp(client, B, bed.token(B), "tools/call", {"name": "ar__read", "arguments": {}})
    assert unknown(r)
    assert up.calls("tools/call") == []
