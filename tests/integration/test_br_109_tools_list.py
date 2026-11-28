"""BR-109, F-008 §3.2 (lot A, AC-35): `connection tools <c> --role <path>`, diagnostic of a
placed connection. Every database check comes before any exchange: without `--role`, connection
not placed on this role or not `active` → code 2, 0 exchange, nothing written; a connection placed
on a sensitive role is listed (F-015 AC-8, test_br_175_*). Otherwise the token comes from
`get_access(connection, role)`, the list is read bypassing the cache, an audit entry carries the
actor `operator-cli`, no secret in the output. (The endpoint's `tools/list` is lot B.)"""

from __future__ import annotations

import json
from typing import Any

import pytest

from hullgate import lifecycle
from tests.support import Gateway, standard_tree

NAME = "mpx/gitea-dev"
ROLE = "mpx/cairn/staging/dev"
URL = "https://tl.mcp.test/mcp"


@pytest.fixture
def placed(gw: Gateway) -> tuple[Gateway, str]:
    standard_tree(gw)
    gw.service()
    gw.upnet.upstream("tl.mcp.test", "modern")
    gw.ok("service", "upstream", "gitea-dev", "--type", "mcp-public", "--mcp-url", URL)
    value = gw.static(NAME, "gd")
    gw.ok("connection", "place", NAME, ROLE)
    return gw, value


def _snapshot(gw: Gateway) -> tuple[Any, ...]:
    conn = gw.connect()
    try:
        return (
            conn.execute("SELECT COUNT(*) FROM tool_name").fetchone()[0],
            conn.execute("SELECT state, generation FROM connection").fetchall(),
            conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0],  # review 448, m4
        )
    finally:
        conn.close()


@pytest.mark.parametrize("case", ["no-role", "not-placed", "not-active"])
def test_br_109_refusals_before_any_exchange(placed: tuple[Gateway, str], case: str) -> None:
    gw, _ = placed
    args = ["connection", "tools", NAME, "--role", ROLE]
    if case == "no-role":
        args = ["connection", "tools", NAME]
    elif case == "not-placed":
        args[-1] = "mpx/cairn/prod/dev"
    elif case == "not-active":
        conn = gw.connect()
        conn.execute("UPDATE connection SET state = 'reauth_required'")
        conn.commit()
        conn.close()
    before = _snapshot(gw)
    result = gw.cli(*args)
    assert result.code == 2, result
    assert gw.upnet.upstreams["tl.mcp.test"].calls() == []
    assert gw.upnet.dialed == []
    assert _snapshot(gw) == before


def test_br_109_placed_connection_lists_with_role_token(
    placed: tuple[Gateway, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    gw, value = placed
    seen: list[str | None] = []
    original = lifecycle.access

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(args[4])  # role_id
        return original(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "access", spy)
    result = gw.cli("connection", "tools", NAME, "--role", ROLE, "--json")
    assert result.code == 0, result
    data = json.loads(result.out)
    assert [t["name"] for t in data["tools"]] == ["gd__read", "gd__write"]
    assert data["version"] == "2026-07-28"
    assert seen == [gw.node_id(ROLE)]  # never the form without a role (forced renewal)
    up = gw.upnet.upstreams["tl.mcp.test"]
    assert {r.headers.get("authorization") for r in up.calls()} == {f"Bearer {value}"}
    # bypassing the cache: a second command reads the upstream again
    lists = len(up.calls("tools/list"))
    gw.ok("connection", "tools", NAME, "--role", ROLE)
    assert len(up.calls("tools/list")) == lists + 1
    entry = [a for a in gw.audit() if a["action"] == "connection.tools_list"][-1]
    assert entry["actor"] == "operator-cli"
    assert json.loads(entry["detail"])["tools"] == 2
    assert value not in result.out + result.err
    for a in gw.audit():
        assert value not in json.dumps(a)


def test_br_109_pagination_is_followed_within_ceilings(gw: Gateway) -> None:
    """Library (AC-10 in lot B): three pages joined; 11 pages or 501 tools refused."""
    from hullgate.upstream_mcp import Target, UpstreamError
    from tests.fake_upstream import tool

    up = gw.upnet.upstream("pg.mcp.test", "modern")
    target = Target("https://pg.mcp.test/mcp")
    up.tools = [tool(f"t{i}") for i in range(6)]
    up.page_size = 2
    listed = gw.upnet.client().list_tools(("r", "c"), target, "Bearer x")
    assert [t["name"] for t in listed.tools] == [f"t{i}" for i in range(6)]
    assert len(up.calls("tools/list")) == 3
    for count, size in ((11, 1), (501, 100)):
        up.tools = [tool(f"t{i}") for i in range(count)]
        up.page_size = size
        with pytest.raises(UpstreamError) as exc:
            gw.upnet.client().list_tools(("r", "c"), target, "Bearer x")
        assert exc.value.reason == "pagination"


def test_br_109_upstream_rpc_error_is_code_1_without_its_message(
    placed: tuple[Gateway, str],
) -> None:
    """Review 448, m2: an upstream JSON-RPC error returns code 1, audited, without its message."""
    from tests.fake_upstream import Fault

    gw, _ = placed
    gw.upnet.upstreams["tl.mcp.test"].faults.append(Fault("rpc_error", on="tools/list"))
    result = gw.cli("connection", "tools", NAME, "--role", ROLE)
    assert result.code == 1, result
    assert "JSON-RPC error -32603" in result.err
    assert "UPSTREAM-SECRET-MESSAGE" not in result.out + result.err
    entry = [a for a in gw.audit() if a["action"] == "connection.tools_refused"][-1]
    assert entry["reason"] == "JSON-RPC error -32603"
    assert "UPSTREAM-SECRET-MESSAGE" not in json.dumps(gw.audit())


def test_br_109_cli_session_end_is_sent_before_exit(gw: Gateway) -> None:
    """Review 448, m1: the CLI holds no session; the explicit end (`DELETE`) has been sent when
    the command returns."""
    standard_tree(gw)
    gw.service()
    up = gw.upnet.upstream("int-ses.mcp.test", "session")
    gw.ok("service", "upstream", "gitea-dev", "--type", "mcp-public", "--mcp-url",
          "http://int-ses.mcp.test/mcp", "--mcp-internal-host")  # fmt: skip
    gw.static(NAME, "gd")
    gw.ok("connection", "place", NAME, ROLE)
    gw.ok("connection", "tools", NAME, "--role", ROLE)
    deletes = [r for r in up.calls() if r.http == "DELETE"]
    assert len(deletes) == 1
    assert (
        deletes[0].headers["mcp-session-id"] == up.calls("tools/list")[-1].headers["mcp-session-id"]
    )
