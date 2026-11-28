"""BR-306 (AC-07) — the OAuth routes but `token`, and their metadata, are limited to 60 requests
in 60 s per limit key: 429 with `Retry-After`, JSON or the generic page by route, before any
database read; another key is served; `token` and the probes are not limited."""

from __future__ import annotations

import pytest

from hullgate import oauth
from hullgate.db import Database
from hullgate.limits import Limits
from tests.support import Gateway, oauth_path, org, resource, standard_tree

ROLE = "mpx/cairn/staging/dev"


def rs() -> str:
    """RFC 9728 metadata path of ROLE, under the latest gateway's identifier."""
    return f"/.well-known/oauth-protected-resource/o/{org()}/p/{ROLE}/mcp"


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _spend(client: object, n: int) -> None:
    from starlette.testclient import TestClient

    assert isinstance(client, TestClient)
    for _ in range(n):
        assert client.get(rs()).status_code == 200


def test_br_306_61st_request_gets_429_in_json(gw: Gateway) -> None:
    standard_tree(gw)
    clock = Clock()
    with gw.client(limits=Limits(clock=clock)) as client:
        _spend(client, 60)
        clock.now += 30
        limited = client.get(rs())
        assert limited.status_code == 429
        assert limited.json() == {"error": "rate_limited"}
        assert limited.headers["retry-after"] == "30"
        clock.now += 30
        assert client.get(rs()).status_code == 200


@pytest.mark.parametrize("action", ["authorize", "consent"])
def test_br_306_page_routes_answer_a_page(gw: Gateway, action: str) -> None:
    standard_tree(gw)
    with gw.client(limits=Limits(clock=Clock())) as client:
        _spend(client, 60)
        limited = client.get(oauth_path(ROLE, action))
        assert limited.status_code == 429
        assert limited.headers["content-type"].startswith("text/html")
        assert "retry-after" in limited.headers


def test_br_306_no_database_read_and_before_the_404(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    standard_tree(gw)
    with gw.client(limits=Limits(clock=Clock())) as client:
        _spend(client, 60)
        reads: list[object] = []
        original = Database.call

        def spy(self: Database, fn: object) -> object:
            reads.append(fn)
            return original(self, fn)  # type: ignore[arg-type]

        monkeypatch.setattr(Database, "call", spy)
        monkeypatch.setattr(oauth.AuthorizationServers, "_audit", lambda *a, **k: None)
        assert client.get(rs()).status_code == 429
        assert client.get("/oauth/p/unknown/form/x").status_code == 429  # 429 before its 404
        assert client.get("/.well-known/oauth-authorization-server/nope").status_code == 429
        assert reads == []


def test_br_306_another_key_is_served(gw: Gateway) -> None:
    standard_tree(gw)
    limits = Limits(clock=Clock())
    with gw.client(limits=limits, peer="192.0.2.10") as client:
        _spend(client, 60)
        assert client.get(rs()).status_code == 429
    with gw.client(limits=limits, peer="192.0.2.11") as client:
        assert client.get(rs()).status_code == 200


def test_br_306_token_and_probes_are_not_limited(gw: Gateway) -> None:
    standard_tree(gw)
    with gw.client(limits=Limits(clock=Clock())) as client:
        _spend(client, 60)
        token = client.post(
            oauth_path(ROLE, "token"),
            data={"grant_type": "refresh_token", "refresh_token": "x", "resource": resource(ROLE)},
        )
        assert token.status_code != 429
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200
        assert client.get(rs()).status_code == 429
