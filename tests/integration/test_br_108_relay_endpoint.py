"""At the endpoint (F-008 lot B): AC-07 (BR-108, saturation audited `upstream_busy`, with no "not
before" date), AC-06 (BR-107: no client header reaches the upstream), AC-22 (BR-124: 429 from the
upstream, delay returned, no exchange before the date), AC-08 (BR-125: one outbound client per use
for the listener), AC-11 (BR-110: names stable from one relay to the next, over a thousand
names)."""

from __future__ import annotations

import json
import re
import threading
import time

import pytest

from hullgate import app as app_mod
from hullgate.outbound import Outbound
from hullgate.upstream_mcp import Transport
from tests.fake_upstream import Fault, tool
from tests.relay_bed import A, Bed, call, gateway_error, names
from tests.support import PUBLIC_URL, Gateway

ALLOWED = {
    "host", "content-length", "authorization", "content-type", "accept", "mcp-protocol-version",
    "mcp-session-id", "user-agent", "mcp-method", "mcp-name",
}  # fmt: skip


@pytest.fixture
def bed(gw: Gateway) -> Bed:
    return Bed(gw)


def test_br_108_busy_connection_is_refused_at_once_and_audited(bed: Bed) -> None:
    up = bed.connection("bz", A)
    with bed.serve() as client:
        token = bed.token(A)
        names(client, A, token)
        up.faults += [Fault("delay", on="tools/call", seconds=2.0) for _ in range(4)]
        threads = [
            threading.Thread(target=call, args=(client, A, token, "bz__read")) for _ in range(4)
        ]
        for t in threads:
            t.start()
        limit = time.monotonic() + 5
        while len(up.calls("tools/call")) < 4 and time.monotonic() < limit:
            time.sleep(0.01)
        started = time.monotonic()
        fifth = call(client, A, token, "bz__read")
        elapsed = time.monotonic() - started
        for t in threads:
            t.join(10)
        after = call(client, A, token, "bz__read")  # no "not before" date
    assert gateway_error(fifth) == "upstream_busy"
    assert elapsed < 0.5, elapsed  # 100 ms aimed at; margin of the ASGI test client
    assert gateway_error(after) is None
    assert len(up.calls("tools/call")) == 5
    issues = [json.loads(a["detail"])["issue"] for a in bed.gw.audit()
              if a["action"] == "relay.tool_call"]  # fmt: skip
    assert issues.count("upstream_busy") == 1


def test_br_107_client_headers_never_reach_the_upstream(bed: Bed) -> None:
    up = bed.connection("hd", A, generation="modern")
    # Mocked routable address; no external connection is made.
    hostile = {"Cookie": "s=1", "Origin": "https://evil.example", "X-Forwarded-For": "6.6.6.6",
               "X-Connection-Id": "other"}  # fmt: skip
    with bed.serve() as client:
        names(client, A, bed.token(A))
        response = call(client, A, bed.token(A), "hd__read", headers=hostile)
    # the hostile `tools/call` did reach the upstream (review 462, m1: otherwise only the clean
    # requests would be read and the test would pass vacuously)
    assert gateway_error(response) is None, response.text
    assert len(up.calls("tools/call")) == 1
    for r in up.calls():
        assert set(r.headers) <= ALLOWED | {h for h in r.headers if h.startswith("mcp-param-")}
        assert r.headers["host"] == "hd.mcp.test"
        if r.http == "POST":  # framing headers named by BR-107 (H24), present
            assert r.headers["content-length"].isdigit()


def test_br_124_upstream_429_sets_a_not_before_date(bed: Bed) -> None:
    up = bed.connection("rl", A)
    with bed.serve() as client:
        token = bed.token(A)
        names(client, A, token)
        up.faults.append(Fault("status", status=429, on="tools/call", headers={"Retry-After": "5"}))
        first = call(client, A, token, "rl__read")
        calls = len(up.calls("tools/call"))
        second = call(client, A, token, "rl__read")
    assert gateway_error(first) == gateway_error(second) == "upstream_busy"
    assert (
        re.search(r"\(retry in \d+ s\)", second.json()["result"]["content"][0]["text"]) is not None
    )
    assert len(up.calls("tools/call")) == calls


def test_br_125_listener_builds_one_client_per_usage(
    bed: Bed, monkeypatch: pytest.MonkeyPatch
) -> None:
    gw = bed.gw
    bed.connection("on", A)
    monkeypatch.setattr(app_mod, "Outbound", lambda: gw.net.outbound())
    monkeypatch.setattr(app_mod, "Transport", lambda: gw.upnet.transport())
    token = bed.token(A)  # the test CLI builds its own: before the measure
    built_out, built_relay = Outbound.built, Transport.built
    application = app_mod.build_public_app(
        gw.db, PUBLIC_URL, key=gw.key, totp=gw.totp, clock=gw.clock,
        connections_key=gw.connections_key(),
    )  # fmt: skip
    from starlette.testclient import TestClient

    with TestClient(application) as client:
        names(client, A, token)
        for _ in range(20):  # 100 calls are played at library level (lot A)
            assert gateway_error(call(client, A, token, "on__read")) is None
    assert Outbound.built - built_out == 1
    assert Transport.built - built_relay == 1


def test_br_110_names_survive_a_new_relay_and_overflow_is_reported(bed: Bed) -> None:
    up = bed.connection("nm", A)
    up.tools = [tool("a.b/c"), tool("x" * 100), tool("___"), tool("ok")]
    with bed.serve() as client:
        first = names(client, A, bed.token(A))
    with bed.serve() as client:  # new instance, same database
        assert names(client, A, bed.token(A)) == first
    assert first[0] == "nm__a_b_c"
    assert len(first[1]) == 64
    invalid = [a for a in bed.gw.audit() if a["action"] == "connection.tool_name_invalid"]
    assert [a["reason"] for a in invalid] == ["invalid_name"] * 2  # once per fetch
    up.tools = [tool(f"t{i}") for i in range(498)]
    up.page_size = 100
    with bed.serve() as client:
        names(client, A, bed.token(A))  # 3 + 498 = 501 names kept
    up.tools = [tool(f"u{i}") for i in range(500)]
    with bed.serve() as client:
        assert names(client, A, bed.token(A)) == []  # 1,001 names: connection omitted
    omitted = [a for a in bed.gw.audit() if a["action"] == "relay.list_omitted"]
    assert [a["reason"] for a in omitted] == ["too_many_names"]
