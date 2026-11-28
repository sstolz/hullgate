"""BR-114 and BR-022 (F-008 lot B, AC-16): the only secret presented to the upstream is the one
`get_access(connection, role of the URL)` returns; never the client's token; a token that expires
in less than 60 s is renewed before the call; each typed error of `get_access` gives its code
with no exchange."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from hullgate import connections, lifecycle
from tests.relay_bed import A, B, Bed, call, gateway_error, names, unknown
from tests.support import Gateway


def _fp(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture
def bed(gw: Gateway) -> Bed:
    return Bed(gw)


def test_br_114_each_upstream_receives_its_connection_token(bed: Bed) -> None:
    ua = bed.connection("ta", A)
    ub = bed.connection("tb", B)
    with bed.serve() as client:
        for role, prefix in ((A, "ta"), (B, "tb")):
            names(client, role, bed.token(role))
            call(client, role, bed.token(role), f"{prefix}__read")
    for up, prefix in ((ua, "ta"), (ub, "tb")):
        got = {r.headers.get("authorization") for r in up.calls()}
        assert got == {f"Bearer {bed.values[prefix]}"}
    issued = {_fp(t) for t in bed.tokens.values()}
    seen = {
        _fp(r.headers.get("authorization", "").removeprefix("Bearer "))
        for up in (ua, ub)
        for r in up.calls()
    }
    assert not issued & seen  # BR-022: no token issued to a client reaches an upstream
    for up in (ua, ub):
        for r in up.calls():
            assert not any(t in str(r.headers) for t in bed.tokens.values())


def test_br_114_token_expiring_soon_is_renewed_before_the_call(bed: Bed) -> None:
    gw = bed.gw
    up = bed.connection("to", A, oauth=True)
    with bed.serve() as client:
        names(client, A, bed.token(A))
        before = gw.tokens("mpx/to")
        assert before is not None
        gw.expire("mpx/to", 30)
        response = call(client, A, bed.token(A), "to__read")
    after = gw.tokens("mpx/to")
    assert after is not None
    assert after["access_token"] != before["access_token"]
    assert response.json()["result"]["isError"] is False
    assert up.calls("tools/call")[-1].headers["authorization"] == f"Bearer {after['access_token']}"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (connections.NotPlaced(), None),
        (connections.NotAuthorized(), "connection_reauth_required"),
        (connections.ReauthRequired(), "connection_reauth_required"),
        (connections.TemporarilyUnavailable(7.0), "upstream_unavailable"),
        (connections.KeyUnavailableError("absent"), "upstream_unavailable"),
        (connections.IntegrityErrorAccess(), "upstream_unavailable"),
    ],
)
def test_br_114_typed_access_errors_without_exchange(
    bed: Bed, monkeypatch: pytest.MonkeyPatch, error: Exception, expected: str | None
) -> None:
    up = bed.connection("te", A)
    with bed.serve() as client:
        names(client, A, bed.token(A))
        posts = len(up.calls())

        def refuse(*_: Any, **__: Any) -> Any:
            raise error

        monkeypatch.setattr(lifecycle, "access", refuse)
        response = call(client, A, bed.token(A), "te__read")
    assert len(up.calls()) == posts
    if expected is None:
        assert unknown(response)
    else:
        assert gateway_error(response) == expected
        if isinstance(error, connections.TemporarilyUnavailable):
            assert "(retry in 7 s)" in response.json()["result"]["content"][0]["text"]
