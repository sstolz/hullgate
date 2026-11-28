"""BR-306, BR-308, BR-300 on F-014's `/o/<id>/…` routes (review 585, raise 3): every route family
takes its client address and its limits, and the limiter is no oracle — a foreign well-formed
identifier and an unknown role under the instance's own answer byte for byte alike, under the
budget (BR-161's 404) and beyond it (BR-306's 429), and spend the same per-key budget."""

from __future__ import annotations

import json
from typing import Any

import httpx2 as httpx
import pytest
from starlette.testclient import TestClient

from hullgate.limits import Limits
from tests.support import Gateway, ap, mcp, standard_tree, token_request

ROLE = "mpx/cairn/staging/dev"
UNKNOWN_ROLE = "mpx/cairn/staging/nobody"
FOREIGN = "aaaaaaaaaaaa"  # well-formed (BR-160), never the instance's


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


# the BR-306 families: (name, method, path builder from (identifier, role path))
FAMILIES = [
    *(
        (action, method, lambda o, p, a=action: f"/o/{o}/oauth/p/{p}/{a}")
        for action, method in (
            ("authorize", "GET"),
            ("consent", "GET"),
            ("revoke", "POST"),
            ("register", "POST"),
        )
    ),
    ("rfc 9728", "GET", lambda o, p: f"/.well-known/oauth-protected-resource/o/{o}/p/{p}/mcp"),
    ("rfc 8414", "GET", lambda o, p: f"/.well-known/oauth-authorization-server/o/{o}/oauth/p/{p}"),
]


def _send(client: TestClient, method: str, path: str) -> httpx.Response:
    if method == "GET":
        return client.get(path)
    return client.post(path, content=b"", headers={"Content-Type": "application/json"})


def _same(a: httpx.Response, b: httpx.Response) -> None:
    assert a.status_code == b.status_code
    assert a.content == b.content
    assert a.headers.multi_items() == b.headers.multi_items()
    assert "location" not in a.headers


def _limited_entries(gw: Gateway) -> list[dict[str, Any]]:
    return [e for e in gw.audit() if e["action"] == "rate_limited.refused"]


@pytest.mark.parametrize(("name", "method", "url"), FAMILIES, ids=[f[0] for f in FAMILIES])
def test_br_306_foreign_identifier_and_unknown_role_spend_the_same_budget(
    gw: Gateway, name: str, method: str, url: Any
) -> None:
    standard_tree(gw)
    clock = Clock()
    with gw.client(limits=Limits(clock=clock)) as client:
        for _ in range(30):  # under the budget: BR-161's 404, byte for byte alike
            foreign = _send(client, method, url(FOREIGN, ROLE))
            unknown = _send(client, method, url(gw.org_id, UNKNOWN_ROLE))
            assert foreign.status_code == 404, (name, foreign.text)
            _same(foreign, unknown)
        # the 60 requests spent on refused forms are the key's whole budget: the instance's own
        # route is refused too, so a 429 tells nothing of which identifier is the instance's
        assert _send(client, method, url(gw.org_id, ROLE)).status_code == 429
        foreign = _send(client, method, url(FOREIGN, ROLE))
        unknown = _send(client, method, url(gw.org_id, UNKNOWN_ROLE))
        assert foreign.status_code == 429
        _same(foreign, unknown)
    entries = _limited_entries(gw)
    assert len(entries) == 1  # BR-312: one coalesced entry for the key's minute
    detail = json.loads(entries[0]["detail"])
    assert detail["surface"] == "oauth"
    assert detail["address"] == "testclient"  # BR-300's reader ran for this family (BR-313)
    assert all(FOREIGN not in json.dumps(e) for e in gw.audit())


def test_br_306_does_not_apply_to_token_and_its_404s_count_nothing(gw: Gateway) -> None:
    """`token` is outside BR-306; at a foreign identifier or an unknown role it answers BR-161's
    404, alike, and a 404 is no unknown credential (BR-308): the key's 30 still write."""
    standard_tree(gw)
    gw.preregister("mpx")
    form = {"grant_type": "refresh_token", "refresh_token": "unknown", "client_id": "client-test"}
    with gw.client(limits=Limits(clock=Clock())) as client:
        for _ in range(70):
            foreign = client.post(
                f"/o/{FOREIGN}/oauth/p/{ROLE}/token",
                content="grant_type=refresh_token&refresh_token=unknown&client_id=client-test",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            unknown = token_request(client, UNKNOWN_ROLE, dict(form))
            assert foreign.status_code == 404
            _same(foreign, unknown)
        for _ in range(35):
            assert token_request(client, ROLE, dict(form)).status_code == 400
    refused = [e for e in gw.audit() if e["action"] == "oauth.token" and e["result"] == "refused"]
    assert len(refused) == 30
    assert all(json.loads(e["detail"])["address"] == "testclient" for e in refused)


def test_br_308_access_point_404s_count_nothing_and_are_alike(gw: Gateway) -> None:
    """The access point family: a foreign identifier and an unknown role answer the same 404 and
    spend no unknown-credential budget; at the instance's own role the key's 30 are written, with
    the address (BR-300, BR-313)."""
    standard_tree(gw)
    with gw.client(limits=Limits(clock=Clock())) as client:
        headers = {"Authorization": "Bearer hg_bad"}
        for _ in range(40):
            foreign = client.post(f"/o/{FOREIGN}/p/{ROLE}/mcp", headers=headers, json={})
            unknown = client.post(ap(UNKNOWN_ROLE), headers=headers, json={})
            assert foreign.status_code == 404
            _same(foreign, unknown)
        for _ in range(35):
            assert mcp(client, ROLE, "hg_bad").status_code == 401
    refused = [e for e in gw.audit() if e["action"] == "access.refused"]
    assert len(refused) == 30
    assert all(json.loads(e["detail"])["address"] == "testclient" for e in refused)
