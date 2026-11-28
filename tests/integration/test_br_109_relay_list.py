"""BR-109 (F-008 lot B, AC-09, AC-10, AC-04 and AC-03 at the endpoint): `tools/list` of a role
announces only the tools of the connections placed and active on the URL's role; a connection
not placed, pending, to reauthorize, revoked or removed brings nothing and receives no exchange;
the upstream pagination is followed within its ceilings and the client's list fits in one
page."""

from __future__ import annotations

import pytest

from tests.fake_upstream import tool
from tests.relay_bed import A, B, Bed, names
from tests.support import Gateway


@pytest.fixture
def bed(gw: Gateway) -> Bed:
    return Bed(gw)


def test_br_109_each_role_lists_only_its_placed_connections(bed: Bed) -> None:
    bed.connection("ca", A)
    bed.connection("cb", B)
    bed.connection("cn")  # placed nowhere
    with bed.serve() as client:
        assert names(client, A, bed.token(A)) == ["ca__read", "ca__write"]
        assert names(client, B, bed.token(B)) == ["cb__read", "cb__write"]
    assert bed.upstream("cn").calls() == []
    assert bed.upstream("cb").calls("tools/call") == []


@pytest.mark.parametrize("state", ["pending", "reauth_required", "revoked", "removed"])
def test_br_109_inactive_connection_brings_nothing(bed: Bed, state: str) -> None:
    gw = bed.gw
    bed.connection("ca", A)
    up = bed.connection("cx", A)
    if state == "revoked":
        gw.ok("connection", "revoke", "mpx/cx")
    elif state == "removed":
        gw.ok("connection", "remove", "mpx/cx", "--confirm", "mpx/cx")
    else:
        conn = gw.connect()
        conn.execute("UPDATE connection SET state = ? WHERE prefix = 'cx'", (state,))
        conn.commit()
        conn.close()
    with bed.serve() as client:
        assert names(client, A, bed.token(A)) == ["ca__read", "ca__write"]
    assert [r for r in up.calls() if r.http == "POST"] == []


def test_br_109_upstream_pages_are_merged_into_one_page(bed: Bed) -> None:
    up = bed.connection("pg", A)
    up.tools = [tool(f"t{i}") for i in range(6)]
    up.page_size = 2
    with bed.serve() as client:
        listed = names(client, A, bed.token(A))
    assert listed == [f"pg__t{i}" for i in range(6)]
    assert len(up.calls("tools/list")) == 3


@pytest.mark.parametrize(("count", "size"), [(11, 1), (501, 100)])
def test_br_109_pagination_beyond_ceilings_omits_the_connection(
    bed: Bed, count: int, size: int
) -> None:
    bed.connection("ok", A)
    up = bed.connection("pg", A)
    up.tools = [tool(f"t{i}") for i in range(count)]
    up.page_size = size
    with bed.serve() as client:
        assert names(client, A, bed.token(A)) == ["ok__read", "ok__write"]
    omitted = [a for a in bed.gw.audit() if a["action"] == "relay.list_omitted"]
    assert [a["reason"] for a in omitted] == ["pagination"]


def test_br_109_client_cursor_is_ignored(bed: Bed) -> None:
    from tests.support import mcp

    bed.connection("ca", A)
    with bed.serve() as client:
        r = mcp(client, A, bed.token(A), "tools/list", {"cursor": "999"}, version="2025-06-18")
    assert [t["name"] for t in r.json()["result"]["tools"]] == ["ca__read", "ca__write"]


def test_br_106_undeclared_upstream_brings_no_tool(bed: Bed) -> None:
    """AC-04 at the endpoint: a service with no declared upstream, 0 exchange."""
    gw = bed.gw
    up = bed.connection("ca", A)
    gw.service("snd")
    gw.static("mpx/nd", "nd", "snd")
    gw.ok("connection", "place", "mpx/nd", A)
    with bed.serve() as client:
        assert names(client, A, bed.token(A)) == ["ca__read", "ca__write"]
    # each connection dialled was for the upstream of `ca`: none for the service with no upstream
    assert len(gw.upnet.dialed) == len(up.calls())
    assert up.calls("tools/call") == []


def test_br_106_removed_upstream_withdraws_tools_at_the_next_request(bed: Bed) -> None:
    """AC-03 at the endpoint: `service upstream --remove` confirmed, the list no longer carries
    its tools, warm cache included (the CLI is another process: checked against the database)."""
    gw = bed.gw
    bed.connection("ca", A)
    with bed.serve() as client:
        assert names(client, A, bed.token(A)) == ["ca__read", "ca__write"]
        gw.ok("service", "upstream", "sca", "--remove", "--confirm", "sca")
        assert names(client, A, bed.token(A)) == []
