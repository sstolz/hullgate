"""BR-118, BR-119, BR-117 at the endpoint (F-008 lot B: AC-23, AC-24 and AC-21 replayed on the
incoming client side). Each part is served in its generation, independently; the upstream session
is never returned to the client and closes when a role is deleted, found at a request served on
**another** role; a call of uncertain outcome is never replayed."""

from __future__ import annotations

import json
import time

import pytest

from tests.fake_upstream import FakeClock, Fault
from tests.relay_bed import A, B, Bed, call, gateway_error, names
from tests.support import HANDSHAKE_VERSIONS, MODERN_VERSION, Gateway, mcp


def _init(version: str) -> dict[str, object]:
    return {"protocolVersion": version, "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"}}  # fmt: skip


def _modern_list(client: object, token: str) -> list[str]:
    meta = {
        "io.modelcontextprotocol/protocolVersion": MODERN_VERSION,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "1"},
    }
    r = mcp(client, A, token, "tools/list", {"_meta": meta}, MODERN_VERSION,  # type: ignore[arg-type]
            {"Mcp-Method": "tools/list"})  # fmt: skip
    assert "mcp-session-id" not in r.headers
    return [t["name"] for t in r.json()["result"]["tools"]]


@pytest.mark.parametrize("upstream", ["session", "stateless", "modern"])
@pytest.mark.parametrize("client_version", ["2025-11-25", MODERN_VERSION])
def test_br_118_client_and_upstream_generations_are_independent(
    gw: Gateway, upstream: str, client_version: str
) -> None:
    bed = Bed(gw)
    up = bed.connection("gn", A, generation=upstream)
    with bed.serve() as client:
        token = bed.token(A)
        if client_version == MODERN_VERSION:
            listed = _modern_list(client, token)
        else:
            init = mcp(client, A, token, "initialize", _init(client_version), client_version)
            assert init.json()["result"]["protocolVersion"] == client_version
            assert "mcp-session-id" not in init.headers
            listed = names(client, A, token, client_version)
        assert listed == ["gn__read", "gn__write"]
        response = call(client, A, token, "gn__write", {"k": "v"}, client_version)
    assert gateway_error(response) is None, response.text
    assert "mcp-session-id" not in response.headers
    assert up.executions == {"write": 1}
    entry = [a for a in gw.audit() if a["action"] == "relay.tool_call"][-1]
    expected = MODERN_VERSION if upstream == "modern" else "2025-11-25"
    assert json.loads(entry["detail"])["version"] == expected


@pytest.mark.parametrize("version", HANDSHAKE_VERSIONS[:3])
def test_br_118_older_client_versions_list_and_call(gw: Gateway, version: str) -> None:
    bed = Bed(gw)
    bed.connection("gn", A, generation="session")
    with bed.serve() as client:
        token = bed.token(A)
        assert names(client, A, token, version) == ["gn__read", "gn__write"]
        assert gateway_error(call(client, A, token, "gn__read", None, version)) is None


def test_br_118_upstream_without_common_version(gw: Gateway) -> None:
    bed = Bed(gw)
    bed.connection("ok", A)
    bed.connection("nv", A, generation="none")
    with bed.serve() as client:
        assert names(client, A, bed.token(A)) == ["ok__read", "ok__write"]
    omitted = [a for a in gw.audit() if a["action"] == "relay.list_omitted"]
    assert [(a["path"], a["reason"]) for a in omitted] == [("mpx/nv", "version")]


def test_br_111_invalid_session_id_omits_the_connection_with_reason_session(gw: Gateway) -> None:
    bed = Bed(gw)
    bed.connection("ok", A)
    bad = bed.connection("bs", A, generation="session")
    bad.faults.append(Fault("bad_session", on="initialize"))
    with bed.serve() as client:
        assert names(client, A, bed.token(A)) == ["ok__read", "ok__write"]
    omitted = [a for a in gw.audit() if a["action"] == "relay.list_omitted"]
    assert [(a["path"], a["reason"]) for a in omitted] == [("mpx/bs", "session")]


def test_br_119_session_reopens_without_double_execution_at_the_endpoint(gw: Gateway) -> None:
    bed = Bed(gw)
    up = bed.connection("se", A, generation="session")
    with bed.serve() as client:
        token = bed.token(A)
        names(client, A, token)
        up.faults.append(Fault("expire", on="tools/call"))
        response = call(client, A, token, "se__write")
    assert gateway_error(response) is None
    assert len(up.calls("initialize")) == 2
    assert up.executions == {"write": 1}
    assert "mcp-session-id" not in response.headers


def test_br_119_deleted_role_sessions_close_on_a_request_to_another_role(gw: Gateway) -> None:
    """Reservation of the lot A review: proved by a request served, without calling `reconcile`."""
    bed = Bed(gw)
    gw.build(("mpx/cairn/staging", "role", "Tmp"))
    tmp = "mpx/cairn/staging/tmp"
    up = bed.connection("se", tmp, B, generation="session")
    relay = gw.relay()
    with bed.serve(relay) as client:
        names(client, tmp, bed.token(tmp))
        names(client, B, bed.token(B))
        held = {k[0] for k, _ in relay.client.held()}
        assert held == {gw.node_id(tmp), gw.node_id(B)}
        tmp_id = gw.node_id(tmp)
        session = up.calls("tools/list")[0].headers["mcp-session-id"]
        gw.ok("tree", "delete", tmp, "--confirm", tmp)
        mcp(client, A, bed.token(A), "ping")  # any request, on another role
        limit = time.monotonic() + 3  # the end is sent in a separate thread (2 s at most, BR-119)
        while not [r for r in up.calls() if r.http == "DELETE"] and time.monotonic() < limit:
            time.sleep(0.02)
    ended = [r for r in up.calls() if r.http == "DELETE"]
    assert [r.headers["mcp-session-id"] for r in ended] == [session]
    assert {k[0] for k, _ in relay.client.held()} == {gw.node_id(B)}
    assert tmp_id not in {k[0] for k in relay.cached()}


def test_br_117_uncertain_outcomes_at_the_endpoint(gw: Gateway) -> None:
    bed = Bed(gw)
    up = bed.connection("nr", A)
    clock = FakeClock()
    with bed.serve(gw.relay(upclock=clock)) as client:
        token = bed.token(A)
        names(client, A, token)
        up.faults.append(Fault("hang", on="tools/call"))
        clock.expect_hang(lambda: len(up.calls("tools/call")) == 1)
        late = call(client, A, token, "nr__write")
        up.faults.append(Fault("cut", on="tools/call"))
        cut = call(client, A, token, "nr__write")
        gw.upnet.refused_ports.add(443)
        refused = call(client, A, token, "nr__write")
    assert gateway_error(late) == "deadline_exceeded"
    assert gateway_error(cut) == "uncertain_outcome"
    assert gateway_error(refused) == "upstream_unavailable"
    assert len(up.calls("tools/call")) == 2  # one request per call sent, never two
