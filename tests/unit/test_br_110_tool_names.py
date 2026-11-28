"""BR-110 (F-008 lot A, AC-36; algorithm of AC-11 at table level): public name
`<prefix>__<tool>`, 64 characters at most, recorded at the first announcement and never
recomputed; at most 1,000 names per connection; `--purge-names --confirm` deletes the names absent
from the current list."""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from hullgate import relay
from hullgate.upstream_mcp import UpstreamError
from tests.fake_upstream import Fault, tool
from tests.support import Gateway, standard_tree

NAME = "mpx/gitea-dev"
ROLE = "mpx/cairn/staging/dev"
URL = "https://tn.mcp.test/mcp"


def _table_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE tool_name (connection_id TEXT, upstream_name TEXT, public_name TEXT,"
        " created_at INTEGER, PRIMARY KEY (connection_id, upstream_name))"
    )
    return conn


def _names(prefix: str, upstream: list[str]) -> dict[str, str]:
    conn = _table_db()
    try:
        return relay.assign(conn, "c", prefix, upstream, 0).names
    finally:
        conn.close()


def test_br_110_format_cleaning_and_length() -> None:
    long = "x" * 100
    names = _names("abcdefgh", ["a.b/c", long, "y" * 40])
    assert names["a.b/c"] == "abcdefgh__a_b_c"
    assert len(names[long]) == 64
    digest = hashlib.sha256(long.encode()).hexdigest()[:8]
    assert names[long] == f"abcdefgh__{'x' * 45}_{digest}"
    assert names["y" * 40] == f"abcdefgh__{'y' * 40}"
    assert len(names["y" * 40]) == 50


def test_br_110_collision_gets_the_hash_suffix() -> None:
    names = _names("gd", ["a.b", "a/b", "a.b"])  # the duplicate counts once
    assert names["a.b"] == "gd__a_b"
    assert names["a/b"] == f"gd__a_b{relay.suffix('a/b')}"
    assert len(names) == 2


def test_br_110_invalid_names_are_ignored() -> None:
    conn = _table_db()
    got = relay.assign(conn, "c", "gd", ["", "___", "///", "ok"], 0)
    assert got.names == {"ok": "gd__ok"}
    assert got.invalid == ("", "___", "///")
    conn.close()


def test_br_110_more_than_1000_names_is_unavailable() -> None:
    conn = _table_db()
    with pytest.raises(UpstreamError) as exc:
        relay.assign(conn, "c", "gd", [f"t{i}" for i in range(1001)], 0)
    assert exc.value.reason == "too_many_names"
    assert conn.execute("SELECT COUNT(*) FROM tool_name").fetchone()[0] == 0
    conn.close()


@pytest.fixture
def placed(gw: Gateway) -> Gateway:
    standard_tree(gw)
    gw.service()
    gw.upnet.upstream("tn.mcp.test", "modern")
    gw.ok("service", "upstream", "gitea-dev", "--type", "mcp-public", "--mcp-url", URL)
    gw.static(NAME, "gd")
    gw.ok("connection", "place", NAME, ROLE)
    return gw


def _listed(gw: Gateway) -> dict[str, str]:
    out = json.loads(gw.ok("connection", "tools", NAME, "--role", ROLE, "--json"))
    return {t["upstream_name"]: t["name"] for t in out["tools"]}


def _table(gw: Gateway) -> dict[str, str]:
    conn = gw.connect()
    try:
        return relay.names_of(conn, gw.connection_id(NAME))
    finally:
        conn.close()


def test_br_110_names_are_stable_across_lists_restart_and_absence(placed: Gateway) -> None:
    up = placed.upnet.upstreams["tn.mcp.test"]
    first = _listed(placed)
    assert _listed(placed) == first  # a second list (new instance: same database)
    up.tools = [t for t in up.tools if t["name"] != "write"]
    assert "write" not in _listed(placed)
    up.tools.append(tool("write"))
    assert _listed(placed)["write"] == first["write"]  # absent then back: same name
    assert _table(placed) == first


def test_br_110_two_connections_same_upstream_name_no_suffix(placed: Gateway) -> None:
    placed.static("mpx/gitea-two", "gt")
    placed.ok("connection", "place", "mpx/gitea-two", ROLE)
    _listed(placed)
    out = json.loads(placed.ok("connection", "tools", "mpx/gitea-two", "--role", ROLE, "--json"))
    assert [t["name"] for t in out["tools"]] == ["gt__read", "gt__write"]


def test_br_110_invalid_upstream_name_is_audited(placed: Gateway) -> None:
    placed.upnet.upstreams["tn.mcp.test"].tools.append(tool("..."))
    _listed(placed)
    entry = [a for a in placed.audit() if a["action"] == "connection.tool_name_invalid"][-1]
    assert entry["reason"] == "invalid_name"


def _fill(gw: Gateway, n: int) -> None:
    conn = gw.connect()
    cid = gw.connection_id(NAME)
    conn.executemany(
        "INSERT INTO tool_name (connection_id, upstream_name, public_name, created_at)"
        " VALUES (?, ?, ?, 0)",
        [(cid, f"old{i}", f"gd__old{i}") for i in range(n)],
    )
    conn.commit()
    conn.close()


def test_br_110_purge_needs_confirm(placed: Gateway) -> None:
    _fill(placed, 10)
    before = _table(placed)
    result = placed.cli("connection", "tools", NAME, "--role", ROLE, "--purge-names")
    assert result.code == 2
    assert _table(placed) == before
    assert placed.upnet.upstreams["tn.mcp.test"].calls() == []


def test_br_110_purge_restores_a_connection_over_the_cap(placed: Gateway) -> None:
    """AC-36: 1,001 names of which 3 are in the current list: the 3 keep their name."""
    up = placed.upnet.upstreams["tn.mcp.test"]
    up.tools = [tool("old0"), tool("old1"), tool("old2")]
    _fill(placed, 1001)
    over = placed.cli("connection", "tools", NAME, "--role", ROLE)
    assert over.code == 2
    assert "1,000 names" in over.err
    result = placed.cli(
        "connection", "tools", NAME, "--role", ROLE, "--purge-names", "--confirm", NAME
    )
    assert result.code == 0, result
    assert _table(placed) == {"old0": "gd__old0", "old1": "gd__old1", "old2": "gd__old2"}
    entry = [a for a in placed.audit() if a["action"] == "connection.tools_purge"][-1]
    assert entry["actor"] == "operator-cli"
    assert json.loads(entry["detail"])["purged"] == 998
    assert placed.cli("connection", "tools", NAME, "--role", ROLE).code == 0


def test_br_110_purge_refused_when_upstream_is_down(placed: Gateway) -> None:
    _fill(placed, 5)
    placed.upnet.upstreams["tn.mcp.test"].faults.append(Fault("status", status=503))
    before = _table(placed)
    result = placed.cli(
        "connection", "tools", NAME, "--role", ROLE, "--purge-names", "--confirm", NAME
    )
    assert result.code == 2
    assert _table(placed) == before
