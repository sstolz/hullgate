"""Recheck the deciding transaction after read-only waits.

The review probes use deterministic barriers at a lease poll and token acquisition. Compare
all domain tables across a refused decision, including placements, pins, names and audit.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

import httpx
import pytest

from hullgate import lifecycle
from hullgate.admin import sessions
from tests.admin_support import Console
from tests.integration.test_br_211_policy_parity import C, _bed
from tests.policy_support import catalogue
from tests.relay_bed import A, Bed
from tests.support import Gateway

NAME = "mpx/gd"


def _snapshot(gw: Gateway) -> dict[str, str]:
    def read(conn: sqlite3.Connection) -> dict[str, str]:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name != 'admin_session'"
        ).fetchall()
        # Only digests escape this helper; assertion output never contains stored credentials.
        return {
            name: hashlib.sha256(
                repr(
                    conn.execute('SELECT * FROM "' + name.replace('"', '""') + '"').fetchall()  # noqa: S608 (quoted identifier from sqlite_master)
                ).encode()
            ).hexdigest()
            for (name,) in tables
        }

    return gw.db.call(read)


def _end(gw: Gateway, event: str) -> None:
    if event == "signout":
        gw.db.call(lambda c: c.execute("DELETE FROM admin_session"))
    elif event == "expiry":
        gw.clock.advance(sessions.IDLE_S)


def _refused(response: httpx.Response) -> None:
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"


@pytest.mark.parametrize("event", ["live", "signout", "expiry"])
@pytest.mark.parametrize("act", ["revoke", "remove"])
def test_decision_after_lease_wait(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch, event: str, act: str
) -> None:
    bed = Bed(gw)
    bed.connection("gd", A, oauth=True)
    provider = gw.provider("sgd")
    console = Console(gw)
    waits: list[float] = []

    def sleep(_s: float) -> None:
        if not waits:
            _end(gw, event)
        waits.append(gw.clock())
        gw.clock.advance(1.0)

    monkeypatch.setitem(getattr(lifecycle, act).__kwdefaults__, "sleep", sleep)
    with console.client() as client:
        console.admin(client)
        digest = client.get(
            "/admin/api/v1/impacts",
            params={"act": f"connection.{act}", "name": NAME},
            headers=console.headers(),
        ).json()["digest"]
        until = int(gw.clock() + lifecycle.LEASE_S)
        gw.db.call(
            lambda c: c.execute(
                "UPDATE connection SET renewing_until = ?, renewal_owner = 'other'", (until,)
            )
        )
        before = _snapshot(gw)
        calls = len(provider.journal)
        response = console.call(
            client, "POST", f"connections/{act}", {"name": NAME, "impact": digest, "confirm": NAME}
        )
    assert waits
    if event != "live":
        _refused(response)
        assert _snapshot(gw) == before
        assert len(provider.journal) == calls
    else:
        assert response.status_code == 200
        assert gw.db.call(
            lambda c: c.execute("SELECT removed, tokens_enc IS NULL FROM connection").fetchone()
        ) == (act == "remove", 1)
        assert any(e["action"] == f"connection.{act}" and e["result"] == "ok" for e in gw.audit())


@pytest.mark.parametrize("event", ["live", "signout", "expiry"])
@pytest.mark.parametrize("route", ["connections/tools", "placements/arguments", "tools/validate"])
def test_decision_with_upstream_token_in_hand(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch, event: str, route: str
) -> None:
    if route == "connections/tools":
        bed = Bed(gw)
        bed.connection("gd", A, oauth=True)
        gw.provider("sgd")
        method, body = "POST", {"name": NAME, "role": A, "purge_names": True, "confirm": NAME}
        action = "connection.tools_purge"
    else:
        bed = _bed(gw)
        if route == "tools/validate":
            gw.ok("policy", "pin", C, "--role", A, "on")
            catalogue(gw, C, A)
            up = bed.upstream("gt")
            up.tools = [{**up.tools[0], "description": "changed upstream"}, *up.tools[1:]]
            method, body = "POST", {"path": "mpx", "connection": C}
            action = "tool.validate"
        else:
            method, body = (
                "PUT",
                {
                    "connection": C,
                    "role": A,
                    "tool": "issue_write",
                    "argument": "method",
                    "values": ["create"],
                },
            )
            action = "policy.argument_set"
    original = lifecycle.access
    seen: list[bool] = []

    def access(*args: Any, **kwargs: Any) -> Any:
        got = original(*args, **kwargs)
        if not seen:
            _end(gw, event)
        seen.append(True)
        return got

    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        before = _snapshot(gw)
        n0 = len(gw.audit())
        monkeypatch.setattr(lifecycle, "access", access)
        response = console.call(client, method, route, body)
    assert seen == [True]
    if event != "live":
        _refused(response)
        assert _snapshot(gw) == before
    else:
        assert response.status_code == 200
        assert _snapshot(gw) != before
        assert any(e["action"] == action and e["result"] == "ok" for e in gw.audit()[n0:])
