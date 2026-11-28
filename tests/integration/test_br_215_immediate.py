"""BR-215 (AC-14) — a change made from the console binds the next authorisation and the next
`tools/list` with no restart; the gateway pushes no `list_changed`, and the API's answer carries
the notice the console shows.

The public listener and the admin listener share one database, as in the process; nothing is
restarted between the console's write and the client's next request.
"""

from __future__ import annotations

from tests.admin_support import Console
from tests.relay_bed import A, Bed, call, names, unknown
from tests.support import Gateway, mcp


def test_br_215_unplacement_binds_the_next_list_and_call(gw: Gateway) -> None:
    bed = Bed(gw)
    bed.connection("ab", A)
    token = bed.token(A)
    console = Console(gw)
    with bed.serve() as public, console.client() as admin:
        console.admin(admin)
        before = names(public, A, token)
        assert before
        assert all(n.startswith("ab_") for n in before)
        removed = console.call(admin, "POST", "placements/remove",
                               {"connection": "mpx/ab", "role": A})  # fmt: skip
        assert removed.status_code == 200, removed.text
        assert removed.json()["tools_notice"] is True
        assert names(public, A, token) == []
        assert unknown(call(public, A, token, before[0]))
        placed = console.call(admin, "POST", "placements", {"connection": "mpx/ab", "role": A})
        assert placed.status_code == 201, placed.text
        assert placed.json()["tools_notice"] is True
        assert names(public, A, token) == before


def test_br_215_a_revocation_binds_the_next_authorisation(gw: Gateway) -> None:
    bed = Bed(gw)
    bed.connection("ab", A)
    token = bed.token(A)
    console = Console(gw)
    with bed.serve() as public, console.client() as admin:
        console.admin(admin)
        assert mcp(public, A, token).status_code == 200
        listed = console.call(admin, "GET", "tokens").json()["tokens"]
        token_id = next(t["id"] for t in listed if t["type"] == "operator" and t["path"] == A)
        revoked = console.call(admin, "POST", "tokens/revoke", {"id": token_id})
        assert revoked.status_code == 200, revoked.text
        assert mcp(public, A, token).status_code == 401


def test_br_215_no_list_changed_is_pushed(gw: Gateway) -> None:
    """The access point is stateless (F-008 Q1 = a): it announces no `listChanged` capability."""
    bed = Bed(gw)
    bed.connection("ab", A)
    token = bed.token(A)
    with bed.serve() as public:
        response = mcp(public, A, token, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        })  # fmt: skip
    assert response.status_code == 200, response.text
    tools = response.json()["result"]["capabilities"].get("tools", {})
    assert not tools.get("listChanged")
