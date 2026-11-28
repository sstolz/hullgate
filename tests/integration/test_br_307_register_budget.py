"""BR-307 (AC-08) — dynamic registration is limited to 20 per hour per limit key: the 21st gets
429 with `Retry-After` and writes no client row."""

from __future__ import annotations

from hullgate.limits import Limits
from tests.support import Gateway, oauth_path, standard_tree

ROLE = "mpx/cairn/staging/dev"
META = {"redirect_uris": ["https://client.example.test/cb"], "client_name": "t"}


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _clients(gw: Gateway) -> int:
    conn = gw.connect()
    try:
        return int(conn.execute("SELECT count(*) FROM client WHERE kind = 'dcr'").fetchone()[0])
    finally:
        conn.close()


def test_br_307_21st_register_gets_429_and_writes_no_row(gw: Gateway) -> None:
    standard_tree(gw)
    gw.ok("client", "dcr", "on")
    clock = Clock()
    with gw.client(limits=Limits(clock=clock)) as client:
        for _ in range(20):
            assert client.post(oauth_path(ROLE, "register"), json=META).status_code == 201
            clock.now += 61  # stays under BR-306's 60 a minute
        assert _clients(gw) == 20
        limited = client.post(oauth_path(ROLE, "register"), json=META)
        assert limited.status_code == 429
        assert limited.json() == {"error": "rate_limited"}
        assert int(limited.headers["retry-after"]) > 0
        assert _clients(gw) == 20
        clock.now = 100.0 + 3600.0  # the first registration leaves the hour
        assert client.post(oauth_path(ROLE, "register"), json=META).status_code == 201


def test_br_307_unchanged_when_registration_is_off(gw: Gateway) -> None:
    standard_tree(gw)
    clock = Clock()
    with gw.client(limits=Limits(clock=clock)) as client:
        for _ in range(25):
            assert client.post(oauth_path(ROLE, "register"), json=META).status_code == 404
            clock.now += 61
