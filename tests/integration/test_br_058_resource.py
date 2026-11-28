"""BR-058 — a single resource, bound from the authorization to the renewal (T03, T04)."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from tests.support import (
    PUBLIC_URL,
    Gateway,
    authorize_params,
    exchange,
    get_code,
    grant,
    org,
    pkce,
    refresh,
    resource,
    standard_tree,
    start_authorization,
    token_request,
)

ROLE = "mpx/cairn/staging/dev"
OTHER = "mpx/cairn/prod/dev"


def _setup(gw: Gateway) -> None:
    standard_tree(gw)
    gw.preregister("mpx")


def _issued(gw: Gateway) -> tuple[int, int]:
    conn = gw.connect()
    try:
        refreshes = conn.execute("SELECT count(*) FROM refresh").fetchone()[0]
        used = conn.execute("SELECT count(*) FROM auth_code WHERE used = 1").fetchone()[0]
        return int(refreshes), int(used)
    finally:
        conn.close()


def test_br_058_authorize_resource_absent_multiple_or_other_redirects_invalid_target(
    gw: Gateway,
) -> None:
    _setup(gw)
    _, challenge = pkce()
    base = authorize_params(ROLE, challenge)
    with gw.client() as client:
        cases = [
            [(k, v) for k, v in base.items() if k != "resource"],
            [*base.items(), ("resource", resource(ROLE))],  # the same value twice
            [*((k, v) for k, v in base.items() if k != "resource"), ("resource", resource(OTHER))],
        ]
        for pairs in cases:
            from urllib.parse import urlencode

            r = client.get(f"/o/{org()}/oauth/p/{ROLE}/authorize?{urlencode(pairs)}")
            assert r.status_code == 302, r.text
            query = parse_qs(urlsplit(r.headers["location"]).query)
            assert query["error"] == ["invalid_target"]
            assert "code" not in query


def test_br_058_authorize_unknown_resource_is_an_error_page(gw: Gateway) -> None:
    _setup(gw)
    _, challenge = pkce()
    params = authorize_params(ROLE, challenge, resource=f"{PUBLIC_URL}/o/{org()}/p/mpx/x/y/z/mcp")
    with gw.client() as client:
        r = start_authorization(client, ROLE, params)
    assert r.status_code == 400
    assert "location" not in r.headers


def test_br_058_token_resource_checked_before_consuming_code(gw: Gateway) -> None:
    _setup(gw)
    with gw.client() as client:
        code, verifier = get_code(gw, client, ROLE)
        for bad in (
            [],
            [resource(ROLE), resource(ROLE)],
            [resource(OTHER)],
            [resource(ROLE) + "/"],
        ):
            r = exchange(client, ROLE, code, verifier, resource=bad)
            assert (r.status_code, r.json()["error"]) == (400, "invalid_target"), bad
            assert "access_token" not in r.json()
        assert _issued(gw) == (0, 0)
        assert exchange(client, ROLE, code, verifier).status_code == 200


def test_br_058_refresh_resource_checked_before_rotation(gw: Gateway) -> None:
    _setup(gw)
    with gw.client() as client:
        g = grant(gw, client, ROLE)
        before = _issued(gw)
        for bad in (
            [],
            [resource(ROLE), resource(ROLE)],
            [resource(ROLE), resource(OTHER)],
            [resource(OTHER)],
        ):
            r = refresh(client, ROLE, g["refresh_token"], resource=bad)
            assert (r.status_code, r.json()["error"]) == (400, "invalid_target"), bad
        assert _issued(gw) == before
        assert refresh(client, ROLE, g["refresh_token"]).status_code == 200


def test_br_058_refresh_at_another_endpoint_with_its_resource_refused(gw: Gateway) -> None:
    _setup(gw)
    with gw.client() as client:
        g = grant(gw, client, ROLE)
        form: dict[str, str | list[str]] = {
            "grant_type": "refresh_token",
            "refresh_token": g["refresh_token"],
            "client_id": "client-test",
            "resource": resource(OTHER),
        }
        r = token_request(client, OTHER, form)
        assert (r.status_code, r.json()["error"]) == (400, "invalid_grant")
        assert refresh(client, ROLE, g["refresh_token"]).status_code == 200
