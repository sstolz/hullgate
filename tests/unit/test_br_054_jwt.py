"""BR-054 — the OAuth access token follows the RFC 9068 profile (N1)."""

from __future__ import annotations

from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from tests.support import (
    REGISTRY,
    Gateway,
    claims,
    grant,
    issuer,
    mcp,
    org,
    resource,
    standard_tree,
)

ROLE = "mpx/cairn/staging/dev"
URL = "<url>"  # the access point of ROLE, resolved per test (its identifier is drawn, F-014)


def _forge(
    gw: Gateway,
    base: str,
    change: dict[str, Any] | None = None,
    drop: tuple[str, ...] = (),
    header: dict[str, Any] | None = None,
    key: Any = None,
    alg: str = "ES256",
) -> str:
    body = claims(base)
    body.update(_resolved(change or {}))
    for name in drop:
        body.pop(name, None)
    hdr = {"typ": "at+jwt", **(header or {})}
    signing: Any = key if key is not None else gw.key.private()
    token = jwt.encode(body, signing, algorithm=alg, headers=hdr)
    REGISTRY.secrets.append(token)
    return token


def _resolved(change: dict[str, Any]) -> dict[str, Any]:
    """`<url>` and `<org>` placeholders of a parametrised change, filled with this gateway's."""

    def fill(value: Any) -> Any:
        if isinstance(value, str):
            return value.replace("<url>", resource(ROLE)).replace("<org>", org())
        if isinstance(value, list):
            return [fill(v) for v in value]
        return value

    return {k: fill(v) for k, v in change.items()}


@pytest.fixture
def issued(gw: Gateway) -> str:
    standard_tree(gw)
    gw.preregister("mpx")
    with gw.client() as client:
        token: str = grant(gw, client, ROLE)["access_token"]
    return token


def test_br_054_profile_header_and_claims(gw: Gateway, issued: str) -> None:
    header = jwt.get_unverified_header(issued)
    assert (header["typ"], header["alg"]) == ("at+jwt", "ES256")
    body = claims(issued)
    assert set(body) >= {"iss", "sub", "aud", "exp", "iat", "jti", "client_id", "scope"}
    assert body["aud"] == resource(ROLE) == f"https://gw.example.test/o/{org()}/p/{ROLE}/mcp"
    assert isinstance(body["aud"], str)
    assert body["iss"] == issuer(ROLE) == f"https://gw.example.test/o/{org()}/oauth/p/{ROLE}"
    assert body["client_id"] == "client-test"  # N1
    assert "mcp" in body["scope"].split()
    assert body["exp"] - body["iat"] == 3600
    for value in body.values():  # no tool name, no policy in the token
        assert "tool" not in str(value)


def test_br_054_resigned_reference_is_accepted(gw: Gateway, issued: str) -> None:
    with gw.client() as client:
        assert mcp(client, ROLE, _forge(gw, issued)).status_code == 200


@pytest.mark.parametrize("name", ["iss", "sub", "aud", "exp", "iat", "jti", "client_id", "scope"])
def test_br_054_missing_claim_refused(gw: Gateway, issued: str, name: str) -> None:
    with gw.client() as client:
        r = mcp(client, ROLE, _forge(gw, issued, drop=(name,)))
    assert r.status_code == 401
    assert 'error="invalid_token"' in r.headers["www-authenticate"]


@pytest.mark.parametrize(
    ("label", "change"),
    [
        ("audience as a one-element list", {"aud": [URL]}),
        (
            "audience as a two-element list",
            {"aud": [URL, "https://gw.example.test/o/<org>/p/x/y/z/w/mcp"]},
        ),
        ("foreign issuer", {"iss": "https://evil.example/o/<org>/oauth/p/" + ROLE}),
        ("iat in the future", {"iat": 4_000_000_000}),
        ("expired", {"exp": 1_000}),
        ("unknown jti", {"jti": "unknown"}),
        ("sub of another role", {"sub": "other"}),
        ("different client_id", {"client_id": "other"}),
    ],
)
def test_br_054_one_property_changed_refused(
    gw: Gateway, issued: str, label: str, change: dict[str, Any]
) -> None:
    with gw.client() as client:
        r = mcp(client, ROLE, _forge(gw, issued, change))
    assert r.status_code == 401, label


def test_br_054_typ_and_algorithm_imposed(gw: Gateway, issued: str) -> None:
    other = ec.generate_private_key(ec.SECP256R1())
    with gw.client() as client:
        assert mcp(client, ROLE, _forge(gw, issued, header={"typ": "JWT"})).status_code == 401
        assert mcp(client, ROLE, _forge(gw, issued, key=other)).status_code == 401
        hs = _forge(gw, issued, key="shared-test-secret-that-is-long-enough", alg="HS256")
        assert mcp(client, ROLE, hs).status_code == 401
        head, payload, _ = issued.split(".")
        assert mcp(client, ROLE, f"{head}.{payload}.").status_code == 401


def test_br_054_iat_within_60_s_skew_accepted(gw: Gateway, issued: str) -> None:
    with gw.client() as client:
        future = int(gw.clock()) + 50
        r = mcp(client, ROLE, _forge(gw, issued, {"iat": future}))
    assert r.status_code == 200
