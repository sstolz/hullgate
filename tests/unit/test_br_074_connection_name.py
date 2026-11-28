"""BR-074 — a connection is named `<client>/<name>` and carries its origin client (AC-03)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from tests.support import Gateway, standard_tree


def _counts(gw: Gateway) -> dict[str, int]:
    conn = gw.connect()
    try:
        return {
            t: int(conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0])  # noqa: S608
            for t in ("connection", "placement", "audit", "vault_meta")
        }
    finally:
        conn.close()


def test_br_074_add_then_refusals_leave_base_unchanged(gw: Gateway) -> None:
    standard_tree(gw)
    gw.service()
    gw.static("mpx/gitea-dev", "gd")
    before = _counts(gw)
    refusals = [
        ("mpx/gitea-dev", "gitea-dev", "g2"),  # doublon
        ("unknown/x", "gitea-dev", "g3"),  # unknown client
        ("mpx/other", "absent", "g4"),  # non-existent service
        ("mpx/Invalid_Name", "gitea-dev", "g5"),  # invalid name
        ("gitea-dev", "gitea-dev", "g6"),  # no client
        ("mpx/a/b", "gitea-dev", "g7"),
    ]
    for name, service, prefix in refusals:
        result = gw.cli(
            "connection", "add", name, "--service", service, "--provided", "--file", "-",
            "--prefix", prefix, stdin="any-value\n",
        )  # fmt: skip
        assert result.code == 2, (name, result)
    assert _counts(gw) == before


def test_br_074_client_referenced_by_id_rename_changes_display_only(gw: Gateway) -> None:
    standard_tree(gw)
    gw.service()
    gw.static("mpx/gitea-dev", "gd")
    cid = gw.connection_id("mpx/gitea-dev")
    gw.ok("tree", "rename", "mpx", "Monolith", "--slug", "mono")
    state = json.loads(gw.ok("connection", "status", "--json"))
    assert [s["name"] for s in state] == ["mono/gitea-dev"]
    assert gw.connection_id("mono/gitea-dev") == cid


def test_br_074_same_name_under_two_clients(gw: Gateway) -> None:
    standard_tree(gw)
    gw.ok("tree", "add", "/", "client", "Acme")
    gw.service()
    gw.static("mpx/gitea", "g1")
    gw.static("acme/gitea", "g2")
    names = [s["name"] for s in json.loads(gw.ok("connection", "list", "--json"))]
    assert names == ["acme/gitea", "mpx/gitea"]


def test_br_074_name_must_be_unique_in_schema(gw: Gateway) -> None:
    """Schema guard: the unique index refuses a live duplicate even outside the CLI."""
    standard_tree(gw)
    gw.service()
    gw.static("mpx/gitea-dev", "gd")
    conn = gw.connect()
    try:
        row = conn.execute("SELECT client_node_id FROM connection").fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO connection (id, client_node_id, name, prefix, service_id, kind,"
                " state, created_at) VALUES ('x', ?, 'gitea-dev', 'zz', 'gitea-dev', 'provided',"
                " 'active', 0)",
                (row[0],),
            )
    finally:
        conn.close()


def test_br_074_service_model_and_url(gw: Gateway) -> None:
    gw.service("gitea-dev", "gitea")
    listed = {s["id"]: s for s in json.loads(gw.ok("service", "list", "--json"))}
    assert listed["gitea-dev"]["model"] == "gitea"
    assert "openid" in listed["gitea-dev"]["scopes"]  # §3.1: the model adds openid
    for url in ("http://x.example.test", "https://", "https://u:p@x.example.test"):
        assert gw.cli("service", "declare", "s2", "--server", url).code == 2
    assert gw.cli("service", "declare", "s2", "--server", "https://x.test", "--model", "y").code
    assert gw.cli("service", "declare", "gitea-dev", "--server", "https://x.test").code == 2
