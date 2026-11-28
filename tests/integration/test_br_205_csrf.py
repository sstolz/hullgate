"""BR-205, BR-234 (AC-05) — a state-changing request proves where it comes from; a `GET` changes
nothing. Both sweeps enumerate the routes from the application object, so a route added later is
covered without a new test."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from hullgate.admin.app import API_PREFIX, CEREMONIES
from hullgate.admin.base import ApiRoute
from tests.admin_support import Console, SoftAuthenticator, bundle, concrete, counts
from tests.support import Gateway


def _path(spec: ApiRoute) -> str:
    return f"{API_PREFIX}/{concrete(spec.path)}"


def _writes(console: Console) -> list[ApiRoute]:
    return [s for s in console.app.api_routes if s.method != "GET"]


def test_br_205_exemptions_are_the_named_ceremonies(gw: Gateway) -> None:
    console = Console(gw)
    assert {s.path for s in console.app.api_routes if not s.session} == CEREMONIES
    assert {"auth/options", "auth/assert", "enrol/options", "enrol/verify"} == set(CEREMONIES)


@pytest.mark.parametrize("origin", [None, "http://evil.test", "http://localhost:1"])
def test_br_205_every_write_refuses_a_foreign_origin(gw: Gateway, origin: str | None) -> None:
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        for spec in _writes(console):
            headers = console.headers()
            if origin is None:
                del headers["Origin"]
            else:
                headers["Origin"] = origin
            response = client.request(spec.method, _path(spec), json={}, headers=headers)
            assert response.status_code == 403, (spec.path, response.text)
            assert response.json()["code"] == "csrf_refused", spec.path


@pytest.mark.parametrize("token", [None, "", "forged", "x" * 43])
def test_br_205_every_session_write_refuses_a_wrong_token(gw: Gateway, token: str | None) -> None:
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        for spec in _writes(console):
            if not spec.session:
                continue
            headers = console.headers()
            headers.pop("X-CSRF-Token")
            if token is not None:
                headers["X-CSRF-Token"] = token
            response = client.request(spec.method, _path(spec), json={}, headers=headers)
            assert response.status_code == 403, (spec.path, response.text)
            assert response.json()["code"] == "csrf_refused", spec.path


def test_br_205_token_of_another_session_is_refused(gw: Gateway) -> None:
    console = Console(gw)
    with console.client() as first, console.client() as second:
        key = console.admin(first)
        first_token = console.csrf
        assert console.sign_in(second, key).status_code == 200
        response = console.call(second, "POST", "session/signout", {}, csrf=first_token)
    assert response.status_code == 403


def test_br_205_session_returns_the_token_again(gw: Gateway) -> None:
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        at_sign_in = console.csrf
        assert console.call(client, "GET", "session").json()["csrf_token"] == at_sign_in
        assert console.call(client, "POST", "session/signout", {}).status_code == 200


def test_br_205_no_cors_header_is_ever_sent(gw: Gateway, tmp_path: Path) -> None:
    console = Console(gw, static_dir=bundle(tmp_path))
    with console.client() as client:
        console.admin(client)
        responses = [
            client.request(
                "OPTIONS",
                _path(s),
                headers={"Origin": "http://evil.test", "Access-Control-Request-Method": "POST"},
            )
            for s in console.app.api_routes
        ]
        responses += [console.call(client, s.method, concrete(s.path), {})
                      for s in console.app.api_routes]  # fmt: skip
        responses += [client.get("/admin"), client.get("/admin/assets/app.js"), client.get("/x")]
    for response in responses:
        assert not [h for h in response.headers if h.lower().startswith("access-control-")]


def test_br_234_no_get_route_writes(gw: Gateway) -> None:
    """Row count on every table around each `GET` route, signed in and not; a second passkey and
    an allow-list exist so that the reads have something to read."""
    console = Console(gw)
    with console.client() as client:
        key = console.admin(client)
        assert console.fresh(client, key).status_code == 200
        assert console.add_passkey(client, SoftAuthenticator()).status_code == 201
        put = console.call(client, "PUT", "allowlist", {"cidrs": ["203.0.113.0/24"]})
        assert put.status_code == 200
        gets = [s for s in console.app.api_routes if s.method == "GET"]
        assert gets
        for spec in gets:
            for activity in (True, False):
                before = counts(gw)
                response = client.get(_path(spec), headers=console.headers(activity=activity))
                assert response.status_code == 200, (spec.path, response.text)
                assert counts(gw) == before, spec.path
        anonymous = TestClient(console.app, base_url=console.base, client=(console.address, 1))
        for spec in gets:
            before = counts(gw)
            assert anonymous.get(_path(spec)).status_code == 401
            assert counts(gw) == before, spec.path
