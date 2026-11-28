"""BR-106 (F-008 lot A, AC-03, AC-04): `service upstream` sets the upstream type and the MCP URL
of a service; a declaration outside the policy is refused (code 2) and nothing is written; a
service with no declared upstream brings no tool and receives no exchange."""

from __future__ import annotations

import json
import sqlite3

import pytest

from hullgate import lifecycle, relay
from tests.support import Gateway, standard_tree

SID = "gitea-dev"
ROLE = "mpx/cairn/staging/dev"


def _state(gw: Gateway) -> tuple[object, ...]:
    conn = gw.connect()
    try:
        service = conn.execute(
            "SELECT upstream_type, mcp_url, mcp_internal FROM service WHERE id = ?", (SID,)
        ).fetchone()
        audits = conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        return service, audits
    finally:
        conn.close()


def _upstream(gw: Gateway, *args: str) -> tuple[int, str]:
    result = gw.cli("service", "upstream", SID, *args)
    return result.code, result.out + result.err


@pytest.fixture
def svc(gw: Gateway) -> Gateway:
    gw.service(SID)
    gw.upnet.addresses.update(
        {
            # Mocked routable address; no external connection is made.
            "pub.mcp.test": ["11.5.5.5"],
            "priv.mcp.test": ["10.5.5.5", "192.168.1.9"],
            "loop.mcp.test": ["127.0.0.1"],
            "ll.mcp.test": ["169.254.3.3"],
            "cgnat.mcp.test": ["100.64.1.1"],
            # Mocked routable address; no external connection is made.
            "mixed.mcp.test": ["10.5.5.5", "11.5.5.5"],
        }
    )
    return gw


REFUSALS = [
    ("--type", "mcp-public", "--mcp-url", "http://pub.mcp.test/mcp"),  # http without exception
    ("--type", "mcp-public", "--mcp-url", "https://u:SECRET-X@pub.mcp.test/mcp"),  # credentials
    ("--type", "mcp-public", "--mcp-url", "https://pub.mcp.test/mcp#frag"),  # fragment
    ("--type", "mcp-public", "--mcp-url", "https://pub.mcp.test/mcp?a=1"),  # query
    ("--type", "mcp-public", "--mcp-url", "https://priv.mcp.test/mcp"),  # private without exception
    ("--type", "mcp-public", "--mcp-url", "https://loop.mcp.test/mcp"),  # loopback
    ("--type", "mcp-public", "--mcp-url", "https://ll.mcp.test/mcp"),  # link-local
    ("--type", "mcp-public", "--mcp-url", "http://pub.mcp.test/mcp", "--mcp-internal-host"),
    ("--type", "mcp-public", "--mcp-url", "http://cgnat.mcp.test/mcp", "--mcp-internal-host"),
    ("--type", "mcp-public", "--mcp-url", "http://mixed.mcp.test/mcp", "--mcp-internal-host"),
    ("--type", "mcp-public", "--mcp-url", "http://loop.mcp.test/mcp", "--mcp-internal-host"),
    ("--type", "rest", "--mcp-url", "https://pub.mcp.test/mcp"),  # unknown type
    (
        "--type",
        "mcp-internal",
        "--mcp-url",
        "https://pub.mcp.test/mcp",
    ),  # mcp-internal without exception
    ("--remove",),  # without --confirm (and without upstream)
]


@pytest.mark.parametrize("args", REFUSALS)
def test_br_106_refusals_write_nothing(svc: Gateway, args: tuple[str, ...]) -> None:
    before = _state(svc)
    code, text = _upstream(svc, *args)
    assert code == 2, text
    assert "SECRET-X" not in text
    assert _state(svc) == before
    assert svc.upnet.dialed == []


def test_br_106_public_https_is_accepted(svc: Gateway) -> None:
    code, text = _upstream(svc, "--type", "mcp-public", "--mcp-url", "https://pub.mcp.test/mcp")
    assert code == 0, text
    service, _ = _state(svc)
    assert service == ("mcp-public", "https://pub.mcp.test/mcp", 0)


def test_br_106_service_list_shows_the_upstream_line(svc: Gateway) -> None:
    """`service list` carries the upstream line of `connection status` (§3.2), reading of lot B's
    AC-OP: "upstream: <type> <host>" once declared, "no MCP upstream declared" before."""
    before = svc.ok("service", "list")
    assert "  no MCP upstream declared" in before.splitlines()
    code, text = _upstream(
        svc, "--type", "mcp-public", "--mcp-url", "http://priv.mcp.test:8080/mcp",
        "--mcp-internal-host",
    )  # fmt: skip
    assert code == 0, text
    after = svc.ok("service", "list").splitlines()
    assert "  upstream: mcp-public priv.mcp.test  internal host" in after
    assert "no MCP upstream declared" not in "\n".join(after)


def test_br_106_internal_host_shows_and_audits_admitted_addresses(svc: Gateway) -> None:
    code, text = _upstream(
        svc, "--type", "mcp-internal", "--mcp-url", "http://priv.mcp.test:8080/mcp",
        "--mcp-internal-host",
    )  # fmt: skip
    assert code == 0, text
    assert "10.5.5.5, 192.168.1.9" in text
    entry = [a for a in svc.audit() if a["action"] == "service.upstream"][-1]
    assert json.loads(entry["after"])["admitted_addresses"] == ["10.5.5.5", "192.168.1.9"]
    assert entry["actor"] == "operator-cli"


def test_br_106_change_needs_confirm_and_remove_empties(svc: Gateway) -> None:
    svc.upnet.upstream("pub.mcp.test", "modern")
    # Mocked routable address; no external connection is made.
    svc.upnet.addresses["pub2.mcp.test"] = ["11.6.6.6"]
    assert _upstream(svc, "--type", "mcp-public", "--mcp-url", "https://pub.mcp.test/mcp")[0] == 0
    before = _state(svc)
    code, _ = _upstream(svc, "--type", "mcp-public", "--mcp-url", "https://pub2.mcp.test/mcp")
    assert code == 2
    assert _state(svc) == before
    code, _ = _upstream(
        svc, "--type", "mcp-public", "--mcp-url", "https://pub2.mcp.test/mcp", "--confirm", SID
    )
    assert code == 0
    entry = [a for a in svc.audit() if a["action"] == "service.upstream"][-1]
    assert json.loads(entry["before"])["mcp_url"] == "https://pub.mcp.test/mcp"
    assert json.loads(entry["after"])["mcp_url"] == "https://pub2.mcp.test/mcp"
    # --remove confirmed: upstream NULL, sessions of its connections closed
    standard_tree(svc)
    svc.static("mpx/gitea-dev", "gd")
    svc.ok("connection", "place", "mpx/gitea-dev", ROLE)
    svc.upnet.upstream("pub2.mcp.test", "modern")
    client = svc.upnet.client()
    from hullgate.upstream_mcp import Target

    key = (svc.node_id(ROLE), svc.connection_id("mpx/gitea-dev"))
    client.list_tools(key, Target("https://pub2.mcp.test/mcp"), "Bearer x")
    assert _upstream(svc, "--remove")[0] == 2
    assert _upstream(svc, "--remove", "--confirm", SID)[0] == 0
    assert _state(svc)[0] == (None, None, 0)
    conn = svc.connect()
    try:
        assert relay.reconcile(lifecycle.Direct(conn), client) == [key]
    finally:
        conn.close()
    result = svc.cli("connection", "tools", "mpx/gitea-dev", "--role", ROLE)
    assert result.code == 2
    assert "no MCP upstream declared" in result.err


def test_br_106_service_without_upstream_gives_no_tool_and_no_exchange(gw: Gateway) -> None:
    """AC-04: active connection of a service without upstream: no exchange, `status` says so."""
    standard_tree(gw)
    gw.service(SID)
    gw.static("mpx/gitea-dev", "gd")
    gw.ok("connection", "place", "mpx/gitea-dev", ROLE)
    out = gw.ok("connection", "status", "mpx/gitea-dev")
    assert "no MCP upstream declared" in out
    view = json.loads(gw.ok("connection", "status", "mpx/gitea-dev", "--json"))[0]
    assert view["upstream"] == "no MCP upstream declared"
    result = gw.cli("connection", "tools", "mpx/gitea-dev", "--role", ROLE)
    assert result.code == 2
    assert gw.upnet.dialed == []
    # Mocked routable address; no external connection is made.
    gw.upnet.addresses["pub.mcp.test"] = ["11.5.5.5"]
    gw.ok(
        "service", "upstream", SID, "--type", "mcp-public", "--mcp-url", "https://pub.mcp.test/mcp"
    )
    assert "upstream: mcp-public pub.mcp.test" in gw.ok("connection", "status", "mpx/gitea-dev")


def test_br_106_service_declared_before_f008_has_null_upstream(gw: Gateway) -> None:
    gw.service(SID)
    conn = sqlite3.connect(gw.path)
    try:
        assert conn.execute("SELECT upstream_type, mcp_url FROM service").fetchall() == [
            (None, None)
        ]
    finally:
        conn.close()


def test_br_106_mcp_local_admits_static_connections_only(svc: Gateway) -> None:
    svc.application("app", SID)
    code, _ = _upstream(
        svc,
        "--type",
        "mcp-internal",
        "--mcp-url",
        "http://priv.mcp.test/mcp",
        "--mcp-internal-host",
    )
    assert code == 0
    standard_tree(svc)
    result = svc.cli(
        "connection", "add", "mpx/oa", "--service", SID, "--application", "app",
        "--prefix", "oa",
    )  # fmt: skip
    assert result.code == 2
    assert "provided-token" in result.err
