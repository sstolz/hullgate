"""BR-203 (AC-03) — an enrolment link is single-use, short-lived, and kept as a digest."""

from __future__ import annotations

import hashlib

import pytest

from hullgate.admin import accounts
from tests.admin_support import Console, SoftAuthenticator, unb64
from tests.support import Gateway

EXPIRED = {
    "code": "enrolment_invalid",
    "message": "This enrolment link is used, expired or replaced.",
}


def test_br_203_link_carries_256_bits_and_works_once(gw: Gateway) -> None:
    console = Console(gw)
    token = console.bootstrap()
    assert len(unb64(token)) == 32
    with console.client() as client:
        assert console.enrol(client, token, SoftAuthenticator()).status_code == 200
        again = console.call(client, "POST", "enrol/options", {"link": token}, csrf="")
    assert again.status_code == 404
    assert again.json() == EXPIRED


@pytest.mark.parametrize("case", ["used", "expired", "replaced", "unknown"])
def test_br_203_every_dead_link_gets_the_same_page(gw: Gateway, case: str) -> None:
    console = Console(gw)
    token = console.bootstrap()
    with console.client() as client:
        if case == "used":
            assert console.enrol(client, token, SoftAuthenticator()).status_code == 200
            client.cookies.clear()
        elif case == "expired":
            gw.clock.advance(accounts.ENROLMENT_TTL_S)
        elif case == "replaced":
            console.bootstrap()
        else:
            token = token[:-4] + "AAAA"
        response = console.call(client, "POST", "enrol/options", {"link": token}, csrf="")
    assert (response.status_code, response.json()) == (404, EXPIRED)


def test_br_203_link_expiring_between_options_and_verify_is_refused(gw: Gateway) -> None:
    console = Console(gw)
    token = console.bootstrap()
    authenticator = SoftAuthenticator()
    with console.client() as client:
        options = console.call(client, "POST", "enrol/options", {"link": token}, csrf="").json()
        gw.clock.advance(accounts.ENROLMENT_TTL_S)
        credential = authenticator.create(options["publicKey"])
        response = console.call(
            client, "POST", "enrol/verify",
            {"link": token, "label": "k", "credential": credential}, csrf="",
        )  # fmt: skip
    assert response.status_code == 404
    assert response.json()["code"] == "enrolment_invalid"


def test_br_203_challenge_of_another_link_is_refused(gw: Gateway) -> None:
    console = Console(gw)
    token = console.bootstrap()
    authenticator = SoftAuthenticator()
    with console.client() as client:
        sign_in = console.call(client, "POST", "auth/options", {}, csrf="").json()["publicKey"]
        credential = authenticator.create(
            {"challenge": sign_in["challenge"], "rp": {"id": "localhost"},
             "user": {"id": "YWRt"}}
        )  # fmt: skip
        response = console.call(
            client, "POST", "enrol/verify",
            {"link": token, "label": "k", "credential": credential}, csrf="",
        )  # fmt: skip
    assert response.status_code == 400
    assert response.json()["code"] == "enrolment_failed"


def test_br_203_base_holds_the_digest_only(gw: Gateway) -> None:
    console = Console(gw)
    token = console.bootstrap()
    conn = gw.connect()
    try:
        rows = conn.execute(
            "SELECT digest, kind, expires_at - created_at FROM enrolment"
        ).fetchall()
        dump = "\n".join(conn.iterdump())
    finally:
        conn.close()
    assert rows == [(hashlib.sha256(token.encode()).hexdigest(), "bootstrap", 1800)]
    assert token not in dump
    assert token not in repr(gw.audit())
