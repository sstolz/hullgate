"""BR-060 — no code without the operator's TOTP authentication at the barrier."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2 as httpx
from starlette.testclient import TestClient

from hullgate import keys
from hullgate.cimd import Fetcher
from tests.support import (
    CLIENT_ID,
    REDIRECT,
    Gateway,
    authorize_params,
    code_from,
    consent,
    consent_page,
    org,
    pkce,
    request_id,
    standard_tree,
    start_authorization,
)

ROLE = "mpx/cairn/prod/dev"


def _setup(gw: Gateway) -> None:
    standard_tree(gw)
    gw.ok("tree", "add", "/", "client", "Acme")
    gw.preregister("mpx")


def _codes(gw: Gateway) -> int:
    conn = gw.connect()
    try:
        return int(conn.execute("SELECT count(*) FROM auth_code").fetchone()[0])
    finally:
        conn.close()


def _pending(gw: Gateway, client: object) -> str:
    from starlette.testclient import TestClient

    assert isinstance(client, TestClient)
    _, challenge = pkce()
    first = start_authorization(client, ROLE, authorize_params(ROLE, challenge))
    assert first.status_code == 302
    location = first.headers["location"]
    assert (
        location == f"https://gw.example.test/o/{org()}/oauth/p/{ROLE}/consent"
    )  # m1: nothing in the URL
    assert "code=" not in location
    return request_id(first)


def test_br_060_authorize_goes_to_the_barrier_never_to_a_code(gw: Gateway) -> None:
    _setup(gw)
    with gw.client() as client:
        req = _pending(gw, client)
        assert len(req) >= 32  # ≥ 192 bits en base64url
        page = consent_page(client, ROLE, req)
    assert page.status_code == 200
    assert "client-test" in page.text
    assert ROLE in page.text
    assert "Sensitive environment</dt><dd>yes" in page.text
    assert "acme" not in page.text  # no data of another node
    assert page.headers["cache-control"].startswith("no-store")
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert _codes(gw) == 0


def test_br_060_wrong_code_shows_page_without_consuming_request(gw: Gateway) -> None:
    _setup(gw)
    with gw.client() as client:
        req = _pending(gw, client)
        bad = consent(gw, client, ROLE, req, code="000000")
        if bad.status_code == 302:  # unlikely coincidence: 000000 right
            return
        assert bad.status_code == 200
        assert "Code refused" in bad.text
        assert "location" not in bad.headers
        good = consent(gw, client, ROLE, req)
        code_from(good)
        again = consent(gw, client, ROLE, req)  # a success consumes the request
        assert again.status_code == 400
    assert _codes(gw) == 1
    assert "consent_failed" in [e["reason"] for e in gw.audit()]


def test_br_060_same_totp_code_never_accepted_twice(gw: Gateway) -> None:
    _setup(gw)
    with gw.client() as client:
        code = gw.totp_code()
        code_from(consent(gw, client, ROLE, _pending(gw, client), code=code))
        replay = consent(gw, client, ROLE, _pending(gw, client), code=code)
        assert replay.status_code == 200
        assert "Code refused" in replay.text
    assert _codes(gw) == 1


def test_br_060_code_outside_one_step_tolerance_refused(gw: Gateway) -> None:
    _setup(gw)
    stale = keys.totp(gw.totp_secret, keys.totp_counter(gw.clock()) - 2)
    with gw.client() as client:
        r = consent(gw, client, ROLE, _pending(gw, client), code=stale)
    assert r.status_code == 200
    assert "Code refused" in r.text


def test_br_060_operator_refusal_redirects_access_denied(gw: Gateway) -> None:
    _setup(gw)
    with gw.client() as client:
        req = _pending(gw, client)
        r = consent(gw, client, ROLE, req, decision="deny")
        after = consent(gw, client, ROLE, req)  # the refusal consumed the request
    assert r.status_code == 302
    assert parse_qs(urlsplit(r.headers["location"]).query)["error"] == ["access_denied"]
    assert after.status_code == 400
    assert _codes(gw) == 0


def test_br_060_without_totp_secret_every_authorization_denied(gw: Gateway) -> None:
    _setup(gw)
    gw.totp_path.chmod(0o600)
    gw.totp_path.unlink()
    _, challenge = pkce()
    with gw.client() as client:
        r = start_authorization(client, ROLE, authorize_params(ROLE, challenge))
        assert client.get("/readyz").status_code == 200  # the gateway stays ready
    assert r.status_code == 302
    assert parse_qs(urlsplit(r.headers["location"]).query)["error"] == ["access_denied"]
    assert _codes(gw) == 0


def test_br_060_pending_request_expires_after_10_minutes(gw: Gateway) -> None:
    _setup(gw)
    with gw.client() as client:
        req = _pending(gw, client)
        gw.clock.advance(601)
        r = consent(gw, client, ROLE, req)
    assert r.status_code == 400
    assert _codes(gw) == 0


def test_br_060_totp_secret_and_codes_absent_from_responses_and_audit(gw: Gateway) -> None:
    _setup(gw)
    secret = gw.totp_path.read_text(encoding="ascii").strip()
    with gw.client() as client:
        req = _pending(gw, client)
        code = gw.totp_code()
        page = consent_page(client, ROLE, req)
        done = consent(gw, client, ROLE, req, code=code)
    assert secret not in page.text
    assert secret not in done.headers.get("location", "")
    dump = repr(gw.audit())
    assert secret not in dump
    assert code not in dump
    assert req not in dump


def test_br_060_one_code_per_time_step_and_step_kept_after_restart(gw: Gateway) -> None:
    """S17: accepted or earlier step refused; next step accepted; survives a restart."""
    _setup(gw)
    step = keys.totp_counter(gw.clock())
    same = keys.totp(gw.totp_secret, step)
    with gw.client() as client:
        code_from(consent(gw, client, ROLE, _pending(gw, client), code=same))
        again = consent(gw, client, ROLE, _pending(gw, client), code=same)
        earlier = keys.totp(gw.totp_secret, step - 1)
        older = consent(gw, client, ROLE, _pending(gw, client), code=earlier)
        pending = _pending(gw, client)
    assert "Code refused" in again.text
    assert "Code refused" in older.text
    from hullgate.db import Database

    gw.db.close()
    gw.db = Database(str(gw.path))
    assert gw.db.open()
    with gw.client() as client:
        replay = consent(gw, client, ROLE, pending, code=same)
        assert "Code refused" in replay.text
        nxt = keys.totp(gw.totp_secret, step + 1)
        code_from(consent(gw, client, ROLE, pending, code=nxt))


def test_br_060_without_totp_unknown_redirect_gets_error_page(gw: Gateway) -> None:
    """S18: without a TOTP secret, refusal at /authorize after the client and the redirect_uri."""
    _setup(gw)
    gw.totp_path.chmod(0o600)
    gw.totp_path.unlink()
    _, challenge = pkce()
    with gw.client() as client:
        bad = start_authorization(
            client, ROLE, authorize_params(ROLE, challenge, redirect="http://localhost:9/other")
        )
    assert bad.status_code == 400
    assert "location" not in bad.headers


def test_br_060_pending_reference_never_in_url_and_cookie_scoped(gw: Gateway) -> None:
    """m1 (review 411): the reference only travels in a cookie limited to the barrier page."""
    _setup(gw)
    _, challenge = pkce()
    with gw.client() as client:
        r = start_authorization(client, ROLE, authorize_params(ROLE, challenge))
        req = request_id(r)
        page = consent_page(client, ROLE, req)
        no_cookie = client.get(f"/o/{org()}/oauth/p/{ROLE}/consent")
        in_query = client.get(f"/o/{org()}/oauth/p/{ROLE}/consent", params={"req": req})
    cookie = r.headers["set-cookie"]
    assert req not in r.headers["location"]
    assert f"Path=/o/{org()}/oauth/p/{ROLE}/consent" in cookie
    assert "HttpOnly" in cookie
    assert "Secure" in cookie  # PUBLIC_URL en https
    assert "samesite=lax" in cookie.lower()
    assert page.status_code == 200
    assert no_cookie.status_code == 400
    assert in_query.status_code == 400  # the query is no longer read


def test_br_060_post_requires_field_equal_to_cookie(gw: Gateway) -> None:
    """m5 (review 413): double submit — field `req` present and equal to the cookie."""
    _setup(gw)
    with gw.client() as client:
        req = _pending(gw, client)
        other = _pending(gw, client)
        url = f"/o/{org()}/oauth/p/{ROLE}/consent"
        cookie_only = client.post(
            url, data={"decision": "deny"}, headers={"Cookie": f"hullgate_consent={req}"}
        )
        mismatch = client.post(
            url,
            data={"req": other, "decision": "deny"},
            headers={"Cookie": f"hullgate_consent={req}"},
        )
        field_only = client.post(url, data={"req": req, "decision": "deny"})
        still_pending = consent_page(client, ROLE, req)
    for response in (cookie_only, mismatch, field_only):
        assert response.status_code == 400
        assert "location" not in response.headers
    assert still_pending.status_code == 200  # none of the three consumed the request


# --- HULL-31: the barrier page allows the client's origin in `form-action` ----------------
# CSP 3 applies `form-action` to the navigation born from the submission, redirects included: with
# `form-action 'self'` alone, the browser blocks the 302 to the `redirect_uri` (code issued, never
# received). The admitted origin is the exact one of the request's validated `redirect_uri`.

CIMD_ID = "https://claude.example.test/oauth/mcp-oauth-client-metadata"
CIMD_REDIRECT = "https://claude.example.test/api/mcp/auth_callback"


class _CimdDocument(Fetcher):
    def document(self, client_id: str, key: str | None = None) -> dict[str, Any]:
        return {"client_id": client_id, "redirect_uris": [CIMD_REDIRECT], "client_name": "CIMD"}


def _form_action(response: httpx.Response) -> list[str]:
    policy = response.headers["content-security-policy"]
    directives = [d.strip().split() for d in policy.split(";")]
    found = [d[1:] for d in directives if d and d[0] == "form-action"]
    assert len(found) == 1, policy
    assert "frame-ancestors 'none'" in policy  # BR-060 unchanged
    return found[0]


def _consent_with(client: TestClient, client_id: str, redirect: str) -> str:
    _, challenge = pkce()
    first = start_authorization(
        client, ROLE, authorize_params(ROLE, challenge, client_id, redirect)
    )
    assert first.status_code == 302, first.text
    return request_id(first)


def test_br_060_consent_page_form_action_admits_preregistered_redirect_origin(
    gw: Gateway,
) -> None:
    _setup(gw)
    with gw.client() as client:
        req = _consent_with(client, CLIENT_ID, REDIRECT)
        page = consent_page(client, ROLE, req)
        wrong = consent(gw, client, ROLE, req, code="abcdef")  # page rendered again, same request
        invalid = client.post(
            f"/o/{org()}/oauth/p/{ROLE}/consent",
            data={"req": req, "decision": "other"},
            headers={"Cookie": f"hullgate_consent={req}"},
        )
    assert page.status_code == 200
    assert _form_action(page) == ["'self'", "http://localhost:4567"]
    assert wrong.status_code == 200
    assert _form_action(wrong) == ["'self'", "http://localhost:4567"]
    assert invalid.status_code == 400
    assert _form_action(invalid) == ["'self'", "http://localhost:4567"]


def test_br_060_consent_page_form_action_admits_cimd_redirect_origin(gw: Gateway) -> None:
    standard_tree(gw)
    gw.fetcher = _CimdDocument()
    with gw.client() as client:
        req = _consent_with(client, CIMD_ID, CIMD_REDIRECT)
        page = consent_page(client, ROLE, req)
        done = consent(gw, client, ROLE, req)
    assert _form_action(page) == ["'self'", "https://claude.example.test"]
    # the 302 of the submission does target the origin admitted by the page
    location = urlsplit(done.headers["location"])
    assert f"{location.scheme}://{location.netloc}" == "https://claude.example.test"


def test_br_060_other_pages_keep_form_action_self_only(gw: Gateway) -> None:
    _setup(gw)
    _, challenge = pkce()
    with gw.client() as client:
        unknown = start_authorization(
            client, ROLE, authorize_params(ROLE, challenge, "unknown", REDIRECT)
        )
        expired = client.get(f"/o/{org()}/oauth/p/{ROLE}/consent")
    assert unknown.status_code == 400
    assert _form_action(unknown) == ["'self'"]
    assert expired.status_code == 400
    assert _form_action(expired) == ["'self'"]


def test_br_060_resubmitted_consumed_request_gets_clear_page_and_no_second_code(
    gw: Gateway,
) -> None:
    _setup(gw)
    with gw.client() as client:
        req = _pending(gw, client)
        code_from(consent(gw, client, ROLE, req))
        again = consent(gw, client, ROLE, req)  # browser left on the page: resubmission
    assert again.status_code == 400
    assert "location" not in again.headers
    assert "already handled" in again.text
    assert "Start the connection again from the client" in again.text
    assert _codes(gw) == 1
