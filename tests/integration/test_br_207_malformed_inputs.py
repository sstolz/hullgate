"""BR-202, BR-203, BR-206, BR-207 (review 560, B1 and N2) — malformed input on the ceremonies,
without a session, gets the rule's refusal: never a 500, and, where the rule counts and audits a
failure, the failure is counted, audited and the challenge consumed (the transaction is not rolled
back by an unhandled exception)."""

from __future__ import annotations

import base64
import copy
from typing import Any

import pytest
from starlette.testclient import TestClient

from hullgate.admin.app import API_PREFIX, _host_port
from tests.admin_support import Console, b64
from tests.support import Gateway


def _attempts(gw: Gateway) -> int:
    conn = gw.connect()
    try:
        return int(conn.execute("SELECT count(*) FROM admin_attempt").fetchone()[0])
    finally:
        conn.close()


def _challenges(gw: Gateway) -> int:
    conn = gw.connect()
    try:
        return int(conn.execute("SELECT count(*) FROM webauthn_challenge").fetchone()[0])
    finally:
        conn.close()


def _refusals(gw: Gateway, action: str) -> list[dict[str, Any]]:
    return [e for e in gw.audit() if e["action"] == action and e["result"] == "refused"]


# -- BR-203: a non-ASCII enrolment link --------------------------------------------------------


@pytest.mark.parametrize("path", ["enrol/options", "enrol/verify"])
def test_br_203_non_ascii_link_is_an_invalid_link(gw: Gateway, path: str) -> None:
    console = Console(gw)
    console.bootstrap()
    body: dict[str, Any] = {"link": "lien-é-" + "x" * 30}
    if path == "enrol/verify":
        body |= {"label": "k", "credential": {"response": {}}}
    with console.client() as client:
        response = console.call(client, "POST", path, body, csrf="")
    assert response.status_code == 404
    assert response.json()["code"] == "enrolment_invalid"
    assert _attempts(gw) == 1
    assert len(_refusals(gw, "admin.enrolment.refused")) == 1


def test_br_207_non_ascii_links_close_the_window(gw: Gateway) -> None:
    console = Console(gw)
    token = console.bootstrap()
    with console.client() as client:
        codes = [
            console.call(client, "POST", "enrol/options", {"link": "é"}, csrf="").status_code
            for _ in range(5)
        ]
        assert codes == [404] * 5
        good = console.call(client, "POST", "enrol/options", {"link": token}, csrf="")
    assert good.status_code == 429


# -- BR-202, BR-207: malformed assertions naming a registered credential ------------------------


def _drop(field: str) -> Any:
    def mutate(c: dict[str, Any]) -> None:
        if field in c:
            del c[field]
        else:
            del c["response"][field]

    return mutate


def _set(path: tuple[str, ...], value: Any) -> Any:
    def mutate(c: dict[str, Any]) -> None:
        target = c
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return mutate


MUTATIONS = {
    "no_signature": _drop("signature"),
    "no_raw_id": _drop("rawId"),
    "wrong_type": _set(("type",), "password"),
    "authenticator_data_not_string": _set(("response", "authenticatorData"), 5),
    "user_handle_not_string": _set(("response", "userHandle"), 5),
}


@pytest.mark.parametrize("case", sorted(MUTATIONS))
def test_br_202_malformed_assertion_is_a_counted_failure(gw: Gateway, case: str) -> None:
    console = Console(gw)
    with console.client() as client:
        key = console.admin(client)
    with console.client() as client:
        options = console.call(client, "POST", "auth/options", {}, csrf="").json()["publicKey"]
        valid = key.get(options)
        broken = copy.deepcopy(valid)
        MUTATIONS[case](broken)
        before = (_attempts(gw), len(_refusals(gw, "admin.sign_in")), _challenges(gw))
        response = console.call(client, "POST", "auth/assert", {"credential": broken}, csrf="")
        assert response.status_code == 401, response.text
        assert response.json()["code"] == "sign_in_failed"
        after = (_attempts(gw), len(_refusals(gw, "admin.sign_in")), _challenges(gw))
        assert after == (before[0] + 1, before[1] + 1, before[2] - 1)  # counted, audited, consumed
        # the challenge is gone: the valid assertion on it no longer signs in (single use)
        replay = console.call(client, "POST", "auth/assert", {"credential": valid}, csrf="")
        assert replay.status_code == 401


def test_br_202_nested_client_data_is_a_failure(gw: Gateway) -> None:
    console = Console(gw)
    with console.client() as client:
        key = console.admin(client)
    with console.client() as client:
        options = console.call(client, "POST", "auth/options", {}, csrf="").json()["publicKey"]
        credential = key.get(options)
        credential["response"]["clientDataJSON"] = b64(b"[" * 30000)
        response = console.call(client, "POST", "auth/assert", {"credential": credential}, csrf="")
    assert response.status_code == 401
    assert _attempts(gw) == 1


# -- N2: the counter is judged on verified data only ---------------------------------------------


def test_br_202_unverified_counter_is_not_audited_as_regressed(gw: Gateway) -> None:
    console = Console(gw)
    with console.client() as client:
        key = console.admin(client)
        assert console.sign_in(client, key).status_code == 200  # stored counter 1
        options = console.call(client, "POST", "auth/options", {}, csrf="").json()["publicKey"]
        forged = key.get(options, count=0)
        forged["response"]["signature"] = base64.urlsafe_b64encode(b"\x30\x00").decode()
        response = console.call(client, "POST", "auth/assert", {"credential": forged}, csrf="")
    assert response.status_code == 401
    assert _refusals(gw, "admin.sign_in")[-1]["reason"] == "invalid_assertion"


# -- BR-206: a Host whose port is not ASCII digits ------------------------------------------------


@pytest.mark.parametrize(
    "host",
    # the raw byte 0xb2, which ASGI decodes as latin-1 into `²` (a digit `int()` refuses): the
    # bytes the listener failed on before the fix (lot A review round 3, N5); then a UTF-8
    # non-ASCII port and a trailing letter
    [b"localhost:\xb2", "localhost:١٤٧٩١".encode(), b"localhost:14791x"],
)
def test_br_206_non_ascii_port_gets_421(gw: Gateway, host: bytes) -> None:
    console = Console(gw)
    with console.client() as client:
        response = client.get(f"{API_PREFIX}/session", headers={b"Host": host})
    assert response.status_code == 421
    assert response.content == b""


def test_br_206_the_port_parser_refuses_a_latin1_digit() -> None:
    """`²` passes `str.isdigit()` and fails `int()`: the parser answers None, never raises."""
    assert "²".isdigit()
    assert _host_port("localhost:²", 80) is None
    assert _host_port("localhost:14791", 80) == ("localhost", 14791)


# -- deeply nested JSON bodies --------------------------------------------------------------------


@pytest.mark.parametrize("path", ["auth/options", "auth/assert", "enrol/options", "enrol/verify"])
def test_br_207_deeply_nested_body_is_an_invalid_request(gw: Gateway, path: str) -> None:
    console = Console(gw)
    body = b"[" * 30000
    assert len(body) < 64 * 1024
    client = TestClient(console.app, base_url=console.base, client=(console.address, 1))
    response = client.post(
        f"{API_PREFIX}/{path}",
        content=body,
        headers={**console.headers(csrf=""), "Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"
