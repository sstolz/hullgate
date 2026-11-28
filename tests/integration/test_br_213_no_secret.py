"""BR-213 (AC-12) — the admin API returns no secret.

Sweep: every read route of the application object, on a seeded base holding known values (operator
token, provided connection token, application secret, enrolment link, session values, and the full
digests of each), with the bodies searched for them. The one exception, the issue response of an
operator token (BR-053 v3), has no route before lot B.
"""

from __future__ import annotations

import hashlib

from hullgate.admin import sessions
from hullgate.admin.app import API_PREFIX
from tests.admin_support import Console, SoftAuthenticator, concrete
from tests.support import REGISTRY, Gateway, standard_tree


def _seed(gw: Gateway) -> list[str]:
    standard_tree(gw)
    _, operator_token = gw.issue("mpx/cairn/staging/dev")
    gw.service()
    provided = gw.static()
    secret = "app-secret-" + "s" * 24
    REGISTRY.secrets.append(secret)
    gw.ok("application", "declare", "app", "--service", "gitea-dev", "--mode", "declared",
          "--client-id", "client-app", "--secret-file", "-", stdin=secret + "\n")  # fmt: skip
    return [operator_token, provided, secret]


def test_br_213_no_read_route_returns_a_secret(gw: Gateway) -> None:
    known = _seed(gw)
    console = Console(gw)
    key = SoftAuthenticator()
    link = console.bootstrap()
    known.append(link)
    with console.client() as client:
        assert console.enrol(client, link, key).status_code == 200
    with console.client() as client:
        assert console.sign_in(client, key).status_code == 200
        value = client.cookies.get(sessions.COOKIE)
        assert value
        assert console.fresh(client, key).status_code == 200
        assert console.add_passkey(client, SoftAuthenticator()).status_code == 201
        known.append(value)
        conn = gw.connect()
        try:
            stored = [r[0] for r in conn.execute("SELECT digest FROM admin_session")]
            stored += [r[0] for r in conn.execute("SELECT csrf_digest FROM admin_session")]
            stored += [r[0] for r in conn.execute("SELECT digest FROM enrolment")]
            stored += [r[0] for r in conn.execute("SELECT hash FROM operator_token")]
        finally:
            conn.close()
        needles = known + [hashlib.sha256(k.encode()).hexdigest() for k in known] + stored
        gets = [s for s in console.app.api_routes if s.method == "GET"]
        assert len(gets) >= 4
        for spec in gets:
            response = client.get(f"{API_PREFIX}/{concrete(spec.path)}", headers=console.headers())
            assert response.status_code == 200, spec.path
            assert response.headers["cache-control"] == "no-store"
            leaks = [n for n in needles if n and n in response.text]
            assert leaks == [], spec.path


def test_br_213_every_api_response_is_no_store(gw: Gateway) -> None:
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        responses = [console.call(client, "GET", "session")]
        responses += [console.call(client, s.method, concrete(s.path), {})
                      for s in console.app.api_routes]  # fmt: skip
        responses.append(client.get(f"{API_PREFIX}/nowhere"))
    for response in responses:
        assert response.headers["cache-control"] == "no-store"


def test_br_213_br_053_v3_a_token_value_is_in_its_issue_response_only(gw: Gateway) -> None:
    """The one exception: the issue response of an operator token, once; every read afterwards,
    and the audit, are searched for the value and its digest."""
    standard_tree(gw)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        issued = console.call(client, "POST", "tokens",
                              {"path": "mpx/cairn/staging/dev", "label": "agent"})  # fmt: skip
        assert issued.status_code == 201, issued.text
        value = issued.json()["value"]
        REGISTRY.secrets.append(value)
        assert value.startswith("hg_")
        assert issued.headers["cache-control"] == "no-store"
        needles = [value, hashlib.sha256(value.encode()).hexdigest()]
        for spec in console.app.api_routes:
            if spec.method != "GET":
                continue
            response = client.get(f"{API_PREFIX}/{concrete(spec.path)}", headers=console.headers())
            assert response.status_code == 200, spec.path
            assert not [n for n in needles if n in response.text], spec.path
        listed = console.call(client, "GET", "tokens").json()["tokens"]
    assert [t["id"] for t in listed if t["holder"] == "agent"] == [issued.json()["id"]]
    assert not [e for e in gw.audit() if value in str(e)]


def test_br_213_br_086_secrets_enter_by_body_and_are_never_echoed(gw: Gateway) -> None:
    standard_tree(gw)
    gw.service()
    console = Console(gw)
    provided = "tok-" + "p" * 30
    secret = "app-secret-" + "q" * 24
    REGISTRY.secrets += [provided, secret]
    with console.client() as client:
        console.admin(client)
        answers = [
            console.call(client, "POST", "connections", {
                "name": "mpx/gitea-dev", "service": "gitea-dev", "prefix": "gd",
                "provided": {"value": provided, "account": "bot"},
            }),
            console.call(client, "POST", "connections/reauthorize", {
                "name": "mpx/gitea-dev", "provided": {"value": provided + "2"},
            }),
            console.call(client, "POST", "applications", {
                "id": "app", "service": "gitea-dev", "mode": "declared",
                "client_id": "client-app", "secret": secret,
            }),
        ]  # fmt: skip
        REGISTRY.secrets.append(provided + "2")
        answers += [console.call(client, "GET", "connections"),
                    console.call(client, "GET", "applications")]  # fmt: skip
    assert [a.status_code for a in answers[:3]] == [201, 200, 201], [a.text for a in answers]
    for answer in answers:
        assert provided not in answer.text
        assert secret not in answer.text
    assert answers[4].json()["applications"][0]["secret"] == "encrypted"  # noqa: S105
