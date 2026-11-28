"""BR-122 (F-008 lot B, AC-27): each tool call and each omission of a connection from
`tools/list` leaves an audit entry (actor, role, connection, public and upstream name, version,
outcome, duration, sizes), with no argument, result, token or header; a hostile unknown name is
truncated and filtered; a failed audit does not prevent the result. (The cancelled call, outcome
`cancelled`, belongs to lot C: BR-120.)"""

from __future__ import annotations

import json
import secrets
import sqlite3

import pytest

from hullgate import relay
from tests.fake_upstream import FakeClock, Fault
from tests.relay_bed import A, Bed, call, gateway_error, names, unknown
from tests.support import REGISTRY, Gateway

FIELDS = {"role", "public_name", "issue", "duration_ms", "request_bytes", "response_bytes"}


def _calls(gw: Gateway) -> list[dict[str, object]]:
    return [a for a in gw.audit() if a["action"] == relay.A_CALL]


def test_br_122_one_entry_per_call_with_its_fields(gw: Gateway) -> None:
    arg = "ARG-MARQUEUR-" + secrets.token_hex(8)
    res = "RES-MARQUEUR-" + secrets.token_hex(8)
    REGISTRY.secrets.extend([arg, res])
    bed = Bed(gw)
    up = bed.connection("au", A)
    bed.connection("ko", A)
    up.results["read"] = {"content": [{"type": "text", "text": res}], "isError": False}
    clock = FakeClock()
    with bed.serve(gw.relay(upclock=clock)) as client:
        token = bed.token(A)
        names(client, A, token)
        ok = call(client, A, token, "au__read", {"q": arg})
        hostile = "\x1b[31m" + "é" * 20 + "\r\n" + "z" * 200
        refused = call(client, A, token, hostile)
        clock.advance(301)
        bed.upstream("ko").faults.append(Fault("status", status=500, on="tools/list"))
        names(client, A, token)
    assert gateway_error(ok) is None
    assert unknown(refused)
    entries = _calls(gw)
    assert len(entries) == 2
    good, bad = (json.loads(str(e["detail"])) for e in entries)
    assert set(good) >= FIELDS
    assert {"connection", "upstream_name", "version"} <= set(good)
    assert (good["issue"], good["connection"], good["upstream_name"]) == ("ok", "mpx/au", "read")
    assert good["public_name"] == "au__read"
    assert good["version"] == "2026-07-28"
    assert int(good["request_bytes"]) > 0
    assert int(good["response_bytes"]) > 0
    assert str(entries[0]["actor"]).startswith("tok_")
    assert bad["issue"] == "refused"
    assert "connection" not in bad
    assert "upstream_name" not in bad
    assert len(bad["public_name"]) == 64
    assert set(bad["public_name"]) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
    )
    omitted = [a for a in gw.audit() if a["action"] == relay.A_OMITTED]
    assert [(a["path"], a["reason"]) for a in omitted] == [("mpx/ko", "outage")]
    text = json.dumps(gw.audit())
    for needle in (arg, res, bed.values["au"], token):
        assert needle not in text


def test_br_122_failing_audit_does_not_prevent_the_result(
    gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    bed = Bed(gw)
    bed.connection("au", A)
    with bed.serve() as client:
        token = bed.token(A)
        names(client, A, token)
        conn = sqlite3.connect(gw.path)
        conn.execute(
            "CREATE TRIGGER relay_audit_fails BEFORE INSERT ON audit"
            " WHEN NEW.action = 'relay.tool_call' BEGIN SELECT RAISE(ABORT, 'panne'); END"
        )
        conn.commit()
        conn.close()
        response = call(client, A, token, "au__read")
    assert response.json()["result"]["isError"] is False
    assert "relay audit not written (relay.tool_call)" in capsys.readouterr().err
