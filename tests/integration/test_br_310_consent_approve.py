"""BR-310, BR-060 v3 (AC-12) — `hullgate consent approve <reference>` approves exactly one pending
request, which passes the barrier with no factor, closed or not, and gets one code in its cookie
holder's browser; an expired request cannot be approved; no route approves."""

from __future__ import annotations

import json
import re

import httpx2 as httpx
from starlette.testclient import TestClient

from hullgate import oauth
from tests.support import (
    CONSENT_COOKIE,
    Gateway,
    authorize_params,
    code_from,
    consent,
    consent_page,
    oauth_path,
    pkce,
    request_id,
    standard_tree,
    start_authorization,
)

ROLE = "mpx/cairn/staging/dev"


def _req(client: TestClient) -> str:
    _, challenge = pkce()
    return request_id(start_authorization(client, ROLE, authorize_params(ROLE, challenge)))


def _reference(client: TestClient, req: str) -> str:
    page = consent_page(client, ROLE, req)
    assert page.status_code == 200
    found = re.search(
        r"Request reference</dt><dd><code>([0-9a-f]{4}-[0-9a-f]{4})</code>", page.text
    )
    assert found is not None, page.text
    return found.group(1)


def _close(gw: Gateway, client: TestClient, req: str) -> None:
    for _ in range(5):
        assert consent(gw, client, ROLE, req, code="999999").status_code == 200
    assert consent(gw, client, ROLE, req).status_code == 429


def _allow(client: TestClient, req: str) -> httpx.Response:
    return client.post(
        oauth_path(ROLE, "consent"),
        data={"req": req, "decision": "allow", "totp": ""},
        headers={"Cookie": f"{CONSENT_COOKIE}={req}"},
    )


def test_br_310_approved_request_passes_a_closed_barrier(gw: Gateway) -> None:
    standard_tree(gw)
    gw.preregister("mpx")
    with gw.client() as client:
        mine, other = _req(client), _req(client)
        ref = _reference(client, mine)
        _close(gw, client, mine)
        closed = consent(gw, client, ROLE, mine)
        assert ref in closed.text  # the closed page shows the reference too
        listed = json.loads(gw.ok("consent", "list", "--json"))
        assert {p["reference"] for p in listed} == {ref, _reference(client, other)}
        mine_row = next(p for p in listed if p["reference"] == ref)
        assert mine_row["role"] == ROLE
        assert mine_row["redirect_host"] == "localhost"
        assert "approved" in gw.ok("consent", "approve", ref)
        response = _allow(client, mine)
        code_from(response)  # one code, no factor
        # no other request passes
        assert consent(gw, client, ROLE, other).status_code == 429
        # the request gave its one code: consumed
        assert _allow(client, mine).status_code == 400
    issue = [e for e in gw.audit() if e["action"] == "oauth.code.issue"]
    assert len(issue) == 1
    assert '"factor": "host_approval"' in issue[0]["detail"]
    approved = [e for e in gw.audit() if e["action"] == "consent.approved"]
    assert len(approved) == 1
    assert approved[0]["actor"] == "operator-cli"
    assert ref in approved[0]["detail"]
    assert "client-test" in approved[0]["detail"]


def test_br_310_code_goes_only_to_the_cookie_holder(gw: Gateway) -> None:
    standard_tree(gw)
    gw.preregister("mpx")
    with gw.client() as client:
        mine = _req(client)
        ref = _reference(client, mine)
        gw.ok("consent", "approve", ref)
        # without the request's cookie and hidden field, nothing is issued
        forged = client.post(
            oauth_path(ROLE, "consent"), data={"req": ref, "decision": "allow", "totp": ""}
        )
        assert forged.status_code == 400
        code_from(_allow(client, mine))


def test_br_310_expired_request_cannot_be_approved(gw: Gateway) -> None:
    standard_tree(gw)
    gw.preregister("mpx")
    with gw.client() as client:
        ref = _reference(client, _req(client))
    gw.clock.advance(oauth.PENDING_TTL + 1)
    refused = gw.cli("consent", "approve", ref)
    assert refused.code == 2
    assert "no pending request" in refused.err
    assert [e for e in gw.audit() if e["action"] == "consent.approved"] == []


def test_br_310_unknown_and_repeated_approvals_refused(gw: Gateway) -> None:
    standard_tree(gw)
    gw.preregister("mpx")
    with gw.client() as client:
        ref = _reference(client, _req(client))
    assert gw.cli("consent", "approve", "0000-0000").code == 2
    gw.ok("consent", "approve", ref)
    assert gw.cli("consent", "approve", ref).code == 2


def test_br_310_no_route_approves(gw: Gateway) -> None:
    """No public route writes `approved_at`; the admin listener has no consent route either."""
    from pathlib import Path

    import hullgate

    src = Path(hullgate.__file__).parent
    writers = [
        p.relative_to(src).as_posix()
        for p in src.rglob("*.py")
        if "approved_at = ?" in p.read_text(encoding="utf-8")
    ]
    assert writers == ["oauth.py"]  # `approve_request`, called by the CLI alone
    callers = [
        p.relative_to(src).as_posix()
        for p in src.rglob("*.py")
        if "approve_request(" in p.read_text(encoding="utf-8")
    ]
    assert sorted(callers) == ["cli.py", "oauth.py"]


def test_br_310_cli_names_the_button_the_page_shows(gw: Gateway) -> None:
    """Review 585, N3: the operator reads the approval line with the barrier page open in front
    of them; the button it names is the page's."""
    standard_tree(gw)
    gw.preregister("mpx")
    with gw.client() as client:
        req = _req(client)
        ref = _reference(client, req)
        page = consent_page(client, ROLE, req).text
        said = gw.ok("consent", "approve", ref)
    named = re.search(r"press (\w+) on the barrier page", said)
    assert named is not None, said
    assert f"<button name=decision value=allow>{named.group(1)}</button>" in page
