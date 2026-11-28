"""Review 604 B1: session loss at a provider's answer must not drop an admitted act.

The review's renewal, callback and revocation probes are assertions here, extended to removal,
registration and renewal on use. Real fake-provider exchanges finish before the barrier ends
(or expires) the session. Controls keep it live; no timing sleeps or secrets in assertions.
"""

from __future__ import annotations

import sqlite3
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from hullgate import connections, lifecycle, upstream
from hullgate.admin import sessions
from tests.admin_support import Console
from tests.integration.test_hull_85_decision_recheck import _snapshot
from tests.policy_support import catalogue, gitea_tools, policy_rows
from tests.relay_bed import A, Bed
from tests.support import Gateway, standard_tree

NAME = "mpx/gitea-dev"


def _row(gw: Gateway) -> dict[str, Any]:
    def read(conn: sqlite3.Connection) -> dict[str, Any]:
        cur = conn.execute(
            "SELECT state, fail_count, renewing_until, renewal_owner, generation, removed,"
            " tokens_enc IS NOT NULL AS has_tokens FROM connection"
        )
        return dict(zip((d[0] for d in cur.description), cur.fetchone(), strict=True))

    return gw.db.call(read)


def _audit(gw: Gateway, action: str) -> list[dict[str, Any]]:
    return [e for e in gw.audit() if e["action"] == action]


def _after_answer(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch, owner: Any, name: str, event: str
) -> list[bool]:
    original = getattr(owner, name)
    answered: list[bool] = []

    def answer(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        # This also proves that the admission lock was released before the provider wait.
        assert not gw.db.call(lambda c: c.in_transaction)
        answered.append(True)
        if event == "signout":
            gw.db.call(lambda c: c.execute("DELETE FROM admin_session"))
        elif event == "expiry":
            gw.db.call(
                lambda c: c.execute(
                    "UPDATE admin_session SET last_activity_at = ?",
                    (int(gw.clock()) - sessions.IDLE_S,),
                )
            )
        return result

    monkeypatch.setattr(owner, name, answer)
    return answered


@pytest.mark.parametrize("event", ["live", "signout", "expiry"])
@pytest.mark.parametrize("route", ["connections/renew", "connections/tools", "rights"])
def test_provider_renewal_is_recorded(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch, event: str, route: str
) -> None:
    bed = Bed(gw)
    bed.connection("gd", A, oauth=True)
    provider = gw.provider("sgd")
    provider.rotate = True
    name = "mpx/gd"
    before = _row(gw)
    if route != "connections/renew":
        gw.clock.advance(3600)  # Access through lifecycle.access must renew on use.
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        answered = _after_answer(gw, monkeypatch, upstream, "refresh", event)
        response = console.call(client, "POST", route, {"name": name, "role": A})
        assert response.status_code == 200
        if route == "connections/tools":
            assert response.json()["tools"]
        elif route == "rights":
            assert response.json()["unavailable"] == []
    assert answered == [True]
    after = _row(gw)
    assert after == {**before, "generation": before["generation"] + 1}
    assert after["renewing_until"] is None
    assert after["renewal_owner"] is None
    assert [e["result"] for e in _audit(gw, "connection.renew")] == ["ok"]
    assert sum(e["live"] for e in provider.refresh_tokens.values()) == 1
    monkeypatch.undo()
    # A second rotation proves the saved refresh token is usable; no stuck lease/invalid_grant.
    assert gw.cli("connection", "renew", name).code == 0
    assert _row(gw)["generation"] == before["generation"] + 2


@pytest.mark.parametrize("event", ["live", "signout", "expiry"])
def test_callback_tokens_and_audit_are_recorded(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch, event: str
) -> None:
    standard_tree(gw)
    gw.service()
    gw.application()
    provider = gw.provider()
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        assert (
            console.call(
                client, "PUT", "applications/web-return", {"id": "app", "registered": True}
            ).status_code
            == 200
        )
        added = console.call(
            client,
            "POST",
            "connections",
            {
                "name": NAME,
                "service": "gitea-dev",
                "application": "app",
                "prefix": "gd",
            },
        )
        assert added.status_code == 201
        back = parse_qs(urlsplit(provider.approve(added.json()["authorization_url"])).query)
        answered = _after_answer(gw, monkeypatch, upstream, "exchange_code", event)
        response = console.call(
            client,
            "POST",
            "connections/callback",
            {
                "state": back["state"][0],
                "code": back["code"][0],
            },
        )
        assert response.status_code == 200
    assert answered == [True]
    assert _row(gw)["state"] == "active"
    assert _row(gw)["has_tokens"]
    assert _row(gw)["generation"] == 1
    [entry] = _audit(gw, "connection.authorize")
    assert entry["result"] == "ok"
    assert entry["actor"].startswith("admin:")
    assert sum(e["live"] for e in provider.refresh_tokens.values()) == 1
    assert not provider.calls("/login/oauth/revoke")
    monkeypatch.undo()
    assert gw.cli("connection", "renew", NAME).code == 0


@pytest.mark.parametrize("event", ["live", "signout", "expiry"])
@pytest.mark.parametrize("act", ["revoke", "remove"])
def test_upstream_revocation_is_audited(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch, event: str, act: str
) -> None:
    standard_tree(gw)
    provider = gw.oauth()
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        digest = client.get(
            "/admin/api/v1/impacts",
            params={
                "act": f"connection.{act}",
                "name": NAME,
            },
            headers=console.headers(),
        ).json()["digest"]
        answered = _after_answer(gw, monkeypatch, lifecycle, "revoke_upstream", event)
        response = console.call(
            client,
            "POST",
            f"connections/{act}",
            {
                "name": NAME,
                "impact": digest,
                "confirm": NAME,
            },
        )
        assert response.status_code == 200
    assert answered == [True]
    row = _row(gw)
    assert not row["has_tokens"]
    assert row["removed"] == (act == "remove")
    assert [e["result"] for e in _audit(gw, f"connection.{act}")] == ["ok"]
    [entry] = _audit(gw, "connection.upstream_revoke")
    assert (entry["result"], entry["reason"]) == ("ok", "revoked")
    assert entry["actor"].startswith("admin:")
    assert len(provider.calls("/login/oauth/revoke")) == 2


@pytest.mark.parametrize("event", ["live", "signout", "expiry"])
@pytest.mark.parametrize("reregister", [False, True])
def test_provider_registration_is_recorded(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch, event: str, reregister: bool
) -> None:
    standard_tree(gw)
    gw.service()
    provider = gw.provider()
    provider.dcr_secret = True
    if reregister:
        gw.ok("application", "declare", "dyn", "--service", "gitea-dev", "--mode", "dynamic")
        gw.oauth(app_id="dyn")
    before = set(provider.clients)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        payload: dict[str, Any] = {"id": "dyn", "service": "gitea-dev", "mode": "dynamic"}
        if reregister:
            digest = client.get(
                "/admin/api/v1/impacts",
                params={
                    "act": "application.reregister",
                    "id": "dyn",
                },
                headers=console.headers(),
            ).json()["digest"]
            payload.update(confirm="dyn", impact=digest)
        answered = _after_answer(gw, monkeypatch, upstream, "register", event)
        response = console.call(
            client, "POST", "applications/reregister" if reregister else "applications", payload
        )
        assert response.status_code == (200 if reregister else 201)
    assert answered == [True]
    [issued] = set(provider.clients) - before
    saved = gw.db.call(
        lambda c: c.execute(
            "SELECT client_id, secret_enc IS NOT NULL FROM application WHERE id = 'dyn'"
        ).fetchone()
    )
    assert saved == (issued, 1)
    action = "application.reregister" if reregister else "application.register"
    [entry] = _audit(gw, action)
    assert entry["result"] == "ok"
    assert entry["actor"].startswith("admin:")
    if reregister:
        assert _row(gw)["state"] == "reauth_required"
        assert not _row(gw)["has_tokens"]
        assert _audit(gw, "connection.upstream_revoke")[-1]["reason"] == "revoked"


@pytest.mark.parametrize(
    ("method", "route", "payload"),
    [
        ("connections_renew", "connections/renew", {"name": NAME}),
        (
            "applications_declare",
            "applications",
            {
                "id": "dyn",
                "service": "gitea-dev",
                "mode": "dynamic",
            },
        ),
        (
            "services_declare",
            "services",
            {
                "id": "new",
                "server": "https://new.example.test",
                "model": "generic",
            },
        ),
    ],
)
def test_session_ended_before_admission_prevents_provider_call(
    gw: Gateway,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    route: str,
    payload: dict[str, Any],
) -> None:
    from hullgate.admin.console import ConsoleApi

    standard_tree(gw)
    provider = gw.oauth()
    before = len(provider.journal)
    original = getattr(ConsoleApi, method)

    async def delayed(self: Any, ctx: Any) -> Any:
        # After the body's recheck, before the worker admits the operation.
        gw.db.call(lambda c: sessions.close(c, ctx.live.digest))
        return await original(self, ctx)

    monkeypatch.setattr(ConsoleApi, method, delayed)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        response = console.call(client, "POST", route, payload)
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"
    assert len(provider.journal) == before
    assert not _audit(gw, "connection.renew")
    assert not _audit(gw, "application.register")


@pytest.mark.parametrize("event", ["live", "signout", "expiry"])
@pytest.mark.parametrize("rediscover", [False, True])
def test_discovery_decision_rechecks_session(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch, rediscover: bool, event: str
) -> None:
    standard_tree(gw)
    provider = gw.provider(model="generic")
    if rediscover:
        gw.service(model="generic")
    before = len(provider.journal)
    original = connections._discover
    answered: list[bool] = []

    def discover(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        # Discovery runs with Database.call's process lock. Use another connection to end
        # the session; acquiring its write lock proves no transaction spans the exchange.
        conn = gw.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if event == "signout":
                conn.execute("DELETE FROM admin_session")
            elif event == "expiry":
                gw.clock.advance(sessions.IDLE_S)
            conn.execute("COMMIT")
        finally:
            conn.close()
        answered.append(True)
        return result

    monkeypatch.setattr(connections, "_discover", discover)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        state_before = _snapshot(gw)
        response = console.call(
            client,
            "POST",
            "services/rediscover" if rediscover else "services",
            {
                "id": "gitea-dev",
                "server": provider.base,
                "model": "generic",
                "confirm": "gitea-dev",
            },
        )
        assert response.status_code == ((200 if rediscover else 201) if event == "live" else 401)
    assert answered == [True]
    assert len(provider.journal) > before
    action = "service.rediscover" if rediscover else "service.declare"
    if event == "live":
        assert _audit(gw, action)[-1]["result"] == "ok"
        assert gw.db.call(connections.service_list)[0].endpoints["token_endpoint"]
    else:
        assert response.json()["code"] == "unauthenticated"
        assert _snapshot(gw) == state_before


@pytest.mark.parametrize("event", ["live", "signout", "expiry"])
@pytest.mark.parametrize("route", ["connections/tools", "placements/arguments", "tools/validate"])
def test_renewal_outcome_survives_but_next_decision_rechecks(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch, event: str, route: str
) -> None:
    bed = Bed(gw)
    up = bed.connection("gd", A, oauth=True)
    up.tools = gitea_tools()
    name = "mpx/gd"
    provider = gw.provider("sgd")
    provider.rotate = True
    catalogue(gw, name, A)
    body: dict[str, Any]
    if route == "tools/validate":
        gw.ok("policy", "pin", name, "--role", A, "on")
        catalogue(gw, name, A)
        up.tools = [{**up.tools[0], "description": "changed upstream"}, *up.tools[1:]]
        method, body = "POST", {"path": "mpx", "connection": name}
    elif route == "placements/arguments":
        method, body = (
            "PUT",
            {
                "connection": name,
                "role": A,
                "tool": "issue_write",
                "argument": "method",
                "values": ["create"],
            },
        )
    else:
        method, body = "POST", {"name": name, "role": A, "purge_names": True, "confirm": name}
    before = _row(gw)
    policies = policy_rows(gw)[:-1]  # All policy state, excluding the renewal audit count.
    gw.clock.advance(3600)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        answered = _after_answer(gw, monkeypatch, upstream, "refresh", event)
        n0 = len(gw.audit())
        response = console.call(client, method, route, body)
    assert answered == [True]
    assert _row(gw) == {**before, "generation": before["generation"] + 1}
    assert [e["result"] for e in _audit(gw, "connection.renew")] == ["ok"]
    assert sum(e["live"] for e in provider.refresh_tokens.values()) == 1
    if event == "live":
        assert response.status_code == 200
    else:
        assert response.status_code == 401
        assert response.json()["code"] == "unauthenticated"
        assert policy_rows(gw)[:-1] == policies
        assert [e["action"] for e in gw.audit()[n0:]] == ["connection.renew"]
    monkeypatch.undo()
    assert gw.cli("connection", "renew", name).code == 0
    assert _row(gw)["generation"] == before["generation"] + 2
