"""BR-121 (F-008 lot B, AC-26): the relay announces only the `tools` capability, with no
`listChanged`; any other family of methods returns `-32601`; a request the upstream addresses to
the client receives an error from the gateway, is never forwarded and is audited; the upstream's
`instructions` and `serverInfo` are never returned."""

from __future__ import annotations

import json
import threading
import time

import pytest

from hullgate.upstream_mcp import REFUSALS_MAX
from tests.fake_upstream import Fault
from tests.relay_bed import A, Bed, call, names
from tests.support import Gateway, mcp

INIT = {"protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "t", "version": "1"}}  # fmt: skip


@pytest.fixture
def bed(gw: Gateway) -> Bed:
    return Bed(gw)


def test_br_121_only_tools_is_announced(bed: Bed) -> None:
    bed.connection("cp", A, generation="session")
    with bed.serve() as client:
        token = bed.token(A)
        init = mcp(client, A, token, "initialize", INIT)
        caps = init.json()["result"]["capabilities"]
        assert set(caps) == {"tools"}
        assert caps["tools"].get("listChanged", False) is False
        for method, params in (
            ("resources/list", {}),
            ("prompts/list", {}),
            ("completion/complete", {"ref": {"type": "ref/prompt", "name": "p"},
                                     "argument": {"name": "a", "value": "b"}}),
            ("logging/setLevel", {"level": "info"}),
        ):  # fmt: skip
            r = mcp(client, A, token, method, params)
            assert r.json()["error"]["code"] == -32601, method
        listed = mcp(client, A, token, "tools/list")
        called = call(client, A, token, "cp__read")
    for r in (init, listed, called):
        assert "INSTRUCTIONS-MARKER" not in r.text
        assert '"fake"' not in r.text  # serverInfo of the upstream
    assert init.json()["result"]["serverInfo"]["name"] == "hullgate"


def test_br_121_upstream_request_to_the_client_is_refused(bed: Bed) -> None:
    up = bed.connection("sp", A, generation="session")
    with bed.serve() as client:
        names(client, A, bed.token(A))
        up.faults.append(Fault("ask", on="tools/call"))
        response = call(client, A, bed.token(A), "sp__read")
        limit = time.monotonic() + 5
        while not up.answers and time.monotonic() < limit:
            time.sleep(0.05)
    assert response.json()["result"]["isError"] is False
    assert "ASK-MARKER" not in response.text
    assert "sampling" not in response.text
    assert [a.get("id") for a in up.answers] == ["ask-1"]
    assert up.answers[0]["error"]["code"] == -32601
    entry = [a for a in bed.gw.audit() if a["action"] == "relay.upstream_request_refused"]
    assert len(entry) == 1
    assert json.loads(entry[0]["detail"])["method"] == "sampling_createMessage"
    assert json.loads(entry[0]["detail"])["count"] == 1


def test_br_121_a_flood_of_upstream_requests_is_bounded(bed: Bed) -> None:
    """Review 462, m4: 200 requests from the upstream to the client in the stream of a single
    call: `REFUSALS_MAX` error responses, in order, from a single thread; one single audit entry
    that carries their number; nothing to the client, the call completes."""
    up = bed.connection("sf", A, generation="session")
    with bed.serve() as client:
        names(client, A, bed.token(A))
        up.faults.append(Fault("ask_flood", on="tools/call", seconds=200))
        before = threading.active_count()
        response = call(client, A, bed.token(A), "sf__read")
        peak = threading.active_count() - before
        limit = time.monotonic() + 10
        while len(up.answers) < REFUSALS_MAX and time.monotonic() < limit:
            time.sleep(0.05)
        time.sleep(0.3)
    assert response.json()["result"]["isError"] is False
    assert "ASK-MARKER" not in response.text
    assert [a.get("id") for a in up.answers] == [f"ask-{i}" for i in range(1, REFUSALS_MAX + 1)]
    assert peak < 10, peak  # one response thread per exchange, not one per request
    entry = [a for a in bed.gw.audit() if a["action"] == "relay.upstream_request_refused"]
    assert len(entry) == 1
    assert json.loads(entry[0]["detail"])["count"] == 200
