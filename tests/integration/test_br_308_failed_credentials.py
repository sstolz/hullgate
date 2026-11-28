"""BR-308 (AC-09) — unknown credentials stop writing beyond 30 in 60 s per limit key: BR-055's
answer unchanged, no `access.refused`; a sustained flood stays unwritten; an authentic
credential's refusal (`revoked`, `out_of_scope`) is always written; a valid token is served."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from hullgate import oauth
from hullgate.limits import Limits
from tests.support import (
    Gateway,
    exchange,
    get_code,
    grant,
    mcp,
    refresh,
    resource,
    standard_tree,
    token_request,
)

ROLE = "mpx/cairn/staging/dev"


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _refused(gw: Gateway, reason: str | None = None) -> int:
    return sum(
        1
        for e in gw.audit()
        if e["action"] == "access.refused" and (reason is None or e["reason"] == reason)
    )


def _bad(client: TestClient, token: str | None = "hg_not-a-token") -> None:  # noqa: S107
    response = mcp(client, ROLE, token)
    assert response.status_code == 401
    assert "resource_metadata=" in response.headers["www-authenticate"]


def test_br_308_from_the_31st_no_access_refused(gw: Gateway) -> None:
    standard_tree(gw)
    clock = Clock()
    with gw.client(limits=Limits(clock=clock)) as client:
        for i in range(30):
            _bad(client, None if i % 2 else "hg_bad")
        assert _refused(gw) == 30
        first = mcp(client, ROLE, "hg_bad")
        assert first.status_code == 401  # unchanged: same status, same challenge
        assert first.headers["www-authenticate"].startswith('Bearer error="invalid_token"')
        for _ in range(20):
            _bad(client)
        assert _refused(gw) == 30
        # a sustained flood never falls back under budget (every one counted)
        for _ in range(200):
            clock.now += 1
            _bad(client)
        assert _refused(gw) == 30
        _, valid = gw.issue(ROLE)
        assert mcp(client, ROLE, valid).status_code == 200


def test_br_308_authentic_refusals_are_always_written(gw: Gateway) -> None:
    standard_tree(gw)
    gw.build(("mpx/cairn", "environment", "Other"), ("mpx/cairn/other", "role", "Dev"))
    revoked_id, revoked = gw.issue(ROLE, "gone")
    gw.ok("token", "revoke", revoked_id)
    _, elsewhere = gw.issue("mpx/cairn/other/dev", "elsewhere")
    with gw.client(limits=Limits(clock=Clock())) as client:
        for _ in range(40):
            _bad(client)
        mcp(client, ROLE, revoked)
        mcp(client, ROLE, elsewhere)
    assert _refused(gw, "revoked") == 1
    assert _refused(gw, "out_of_scope") == 1


def test_br_308_unknown_grant_at_token_stops_writing(gw: Gateway) -> None:
    standard_tree(gw)
    gw.preregister("mpx")
    form = {
        "grant_type": "refresh_token",
        "refresh_token": "unknown",
        "client_id": "client-test",
        "resource": resource(ROLE),
    }
    with gw.client(limits=Limits(clock=Clock())) as client:
        for _ in range(45):
            response = token_request(client, ROLE, dict(form))
            assert response.status_code == 400
            assert response.json()["error"] == "invalid_grant"
        unknown_client = token_request(client, ROLE, {**form, "client_id": "nobody"})
        assert unknown_client.status_code == 401
    written = [e for e in gw.audit() if e["action"] == "oauth.token"]
    assert len(written) == 30


@pytest.mark.parametrize("grant_type", ["refresh_token", "authorization_code"])
@pytest.mark.parametrize("target", ["missing", "wrong", "doubled"])
@pytest.mark.parametrize("credential", [None, "", "unknown", ["unknown", "another"]])
def test_br_308_unknown_grant_without_valid_resource_counts(
    gw: Gateway, grant_type: str, target: str, credential: str | list[str] | None
) -> None:
    """BR-308 v2: absent, empty and unknown grants count despite invalid_target answering first."""
    standard_tree(gw)
    gw.preregister("mpx")
    field = "refresh_token" if grant_type == "refresh_token" else "code"
    form: dict[str, str | list[str]] = {
        "grant_type": grant_type,
        "client_id": "client-test",
    }
    if credential is not None:
        form[field] = credential
    if target == "wrong":
        form["resource"] = resource("mpx/cairn/staging")
    elif target == "doubled":
        form["resource"] = [resource(ROLE), resource(ROLE)]
    with gw.client(limits=Limits(clock=Clock())) as client:
        for _ in range(45):
            response = token_request(client, ROLE, dict(form))
            assert response.status_code == 400
            assert response.json()["error"] == "invalid_target"
    written = [e for e in gw.audit() if e["action"] == "oauth.token"]
    assert len(written) == 30
    assert {e["reason"] for e in written} == {"invalid_target"}


@pytest.mark.parametrize("grant_type", ["refresh_token", "authorization_code"])
@pytest.mark.parametrize("target", ["missing", "wrong", "doubled"])
@pytest.mark.parametrize("multiple", [False, True])
def test_br_308_authentic_grant_without_resource_is_always_written(
    gw: Gateway, grant_type: str, target: str, multiple: bool
) -> None:
    """A stored grant keeps invalid_target written with its actor after the budget is spent."""
    standard_tree(gw)
    gw.preregister("mpx")
    field = "refresh_token" if grant_type == "refresh_token" else "code"
    unknown: dict[str, str | list[str]] = {
        "grant_type": grant_type,
        field: "unknown",
        "client_id": "client-test",
    }
    if target == "wrong":
        unknown["resource"] = resource("mpx/cairn/staging")
    elif target == "doubled":
        unknown["resource"] = [resource(ROLE), resource(ROLE)]
    with gw.client(limits=Limits(clock=Clock())) as client:
        stored = (
            grant(gw, client, ROLE)["refresh_token"]
            if grant_type == "refresh_token"
            else get_code(gw, client, ROLE)[0]
        )
        for _ in range(35):
            token_request(client, ROLE, dict(unknown))
        for _ in range(5):
            response = token_request(
                client, ROLE, {**unknown, field: ["unknown", stored] if multiple else stored}
            )
            assert response.status_code == 400
            assert response.json()["error"] == "invalid_target"
    refused = [e for e in gw.audit() if e["action"] == "oauth.token" and e["result"] == "refused"]
    assert len(refused) == 30 + 5
    assert {e["actor"] for e in refused} == {"client-test"}
    assert {e["reason"] for e in refused} == {"invalid_target"}


@pytest.mark.parametrize(
    "grant_type", [None, "", "unsupported", ["refresh_token", "refresh_token"]]
)
def test_br_308_unsupported_grant_type_counts_and_stays_silent(
    gw: Gateway, grant_type: str | list[str] | None
) -> None:
    standard_tree(gw)
    gw.preregister("mpx")
    clock = Clock()
    form: dict[str, str | list[str]] = {"client_id": "client-test", "resource": resource(ROLE)}
    if grant_type is not None:
        form["grant_type"] = grant_type
    with gw.client(limits=Limits(clock=clock)) as client:
        for i in range(230):
            if i >= 30:
                clock.now += 1
            response = token_request(client, ROLE, dict(form))
            assert response.status_code == 400
            assert response.json() == {"error": "unsupported_grant_type"}
        # The access point spends the same credential budget as token.
        _bad(client)
    written = [e for e in gw.audit() if e["action"] == "oauth.token"]
    assert len(written) == 30
    assert {e["reason"] for e in written} == {"unsupported_grant_type"}
    assert _refused(gw) == 0


@pytest.mark.parametrize("grant_type", ["authorization_code", "refresh_token"])
@pytest.mark.parametrize("target", ["right", "wrong"])
@pytest.mark.parametrize("client_id", ["client-test", "unknown-client"])
def test_br_308_multivalue_stored_grant_always_written(
    gw: Gateway, grant_type: str, target: str, client_id: str
) -> None:
    """v3: all answers stay visible, keep their actor, and consume no authentic grant."""
    standard_tree(gw)
    gw.preregister("mpx")
    field = "code" if grant_type == "authorization_code" else "refresh_token"
    form: dict[str, str | list[str]] = {
        "grant_type": grant_type,
        "client_id": client_id,
        "resource": resource(ROLE if target == "right" else "mpx/cairn/staging"),
        field: "unknown",
    }
    with gw.client(limits=Limits(clock=Clock())) as client:
        verifier = ""
        if grant_type == "authorization_code":
            stored, verifier = get_code(gw, client, ROLE)
        else:
            stored = grant(gw, client, ROLE)["refresh_token"]
        for _ in range(35):
            unknown = token_request(client, ROLE, dict(form))
        baseline = [
            e for e in gw.audit() if e["action"] == "oauth.token" and e["result"] == "refused"
        ]
        assert len(baseline) == 30
        if client_id != "client-test":
            expected = {
                "error": "invalid_client",
                "error_description": "unknown or unauthenticated client",
            }
            status = 401
        elif target == "wrong":
            expected = {
                "error": "invalid_target",
                "error_description": "exactly one resource: this access point",
            }
            status = 400
        else:
            expected = {
                "error": "invalid_grant",
                "error_description": "unknown code" if field == "code" else "unknown refresh token",
            }
            status = 400
        assert unknown.status_code == status
        assert unknown.json() == expected
        for _ in range(5):
            response = token_request(client, ROLE, {**form, field: ["unknown", stored]})
            assert response.status_code == status
            assert response.json() == expected
        # Single unknown values still write nothing after authentic refusals.
        assert token_request(client, ROLE, dict(form)).json() == expected
        refused = [
            e for e in gw.audit() if e["action"] == "oauth.token" and e["result"] == "refused"
        ]
        assert len(refused) == 35
        assert {e["actor"] for e in refused[30:]} == {client_id}
        assert {e["reason"] for e in refused[30:]} == {expected["error"]}
        # A normal exchange proves that neither use nor revocation occurred.
        accepted = (
            exchange(client, ROLE, stored, verifier)
            if field == "code"
            else refresh(client, ROLE, stored)
        )
        assert accepted.status_code == 200


@pytest.mark.parametrize("grant_type", ["authorization_code", "refresh_token"])
def test_br_308_presented_values_use_one_query(gw: Gateway, grant_type: str) -> None:
    """Thousands of unknown values cost one lookup; only the grant's own field counts."""
    standard_tree(gw)
    gw.preregister("mpx")
    field = "code" if grant_type == "authorization_code" else "refresh_token"
    other = "refresh_token" if field == "code" else "code"
    with gw.client() as client:
        stored = (
            get_code(gw, client, ROLE)[0]
            if field == "code"
            else grant(gw, client, ROLE)["refresh_token"]
        )
    with gw.connect() as conn:
        queries: list[str] = []
        conn.set_trace_callback(queries.append)
        values = [f"bad{i}" for i in range(2000)] + [stored]
        assert not oauth._presented_grant_unknown(conn, grant_type, {field: values})
        assert len(queries) == 1
        queries.clear()
        assert oauth._presented_grant_unknown(conn, grant_type, {other: values})
        assert len(queries) == 1
