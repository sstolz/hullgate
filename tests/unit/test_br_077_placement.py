"""BR-077 — a placement puts a connection on a role, once (AC-06)."""

from __future__ import annotations

import json

import pytest

from tests.support import Gateway, standard_tree


@pytest.mark.parametrize("target", ["/", "mpx", "mpx/cairn", "mpx/cairn/staging"])
def test_br_077_only_a_role_accepts_a_placement(gw: Gateway, target: str) -> None:
    standard_tree(gw)
    gw.service()
    gw.static("mpx/gitea-dev", "gd")
    result = gw.cli("connection", "place", "mpx/gitea-dev", target)
    assert result.code == 2
    assert "is placed on a role" in result.err


def test_br_077_role_accepts_once_and_several_connections(gw: Gateway) -> None:
    standard_tree(gw)
    gw.service()
    gw.static("mpx/gitea-dev", "gd")
    gw.static("mpx/gitea-rev", "gr")
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/staging/dev")
    assert gw.cli("connection", "place", "mpx/gitea-dev", "mpx/cairn/staging/dev").code == 2
    gw.ok("connection", "place", "mpx/gitea-rev", "mpx/cairn/staging/dev")
    assert gw.cli("connection", "place", "mpx/absent", "mpx/cairn/staging/dev").code == 2
    assert gw.cli("connection", "place", "mpx/gitea-dev", "mpx/cairn/no/where").code == 2
    conn = gw.connect()
    try:
        assert conn.execute("SELECT count(*) FROM placement").fetchone() == (2,)
        # a single token set, whatever the number of placements
        assert conn.execute(
            "SELECT count(*) FROM connection WHERE tokens_enc IS NOT NULL"
        ).fetchone() == (2,)
    finally:
        conn.close()


def test_br_077_place_and_unplace_are_audited(gw: Gateway) -> None:
    standard_tree(gw)
    gw.service()
    gw.static("mpx/gitea-dev", "gd")
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/staging/dev")
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/staging/principal")
    gw.ok("connection", "unplace", "mpx/gitea-dev", "mpx/cairn/staging/dev")
    assert gw.cli("connection", "unplace", "mpx/gitea-dev", "mpx/cairn/staging/dev").code
    entries = [e for e in gw.audit() if e["action"].startswith("connection.")]
    placed = [e for e in entries if e["action"] == "connection.place"]
    assert [e["path"] for e in placed] == ["mpx/cairn/staging/dev", "mpx/cairn/staging/principal"]
    assert json.loads(placed[0]["detail"])["connection"] == "mpx/gitea-dev"
    unplaced = [e for e in entries if e["action"] == "connection.unplace"]
    assert [e["path"] for e in unplaced] == ["mpx/cairn/staging/dev"]
    state = json.loads(gw.ok("connection", "status", "--json"))[0]
    assert state["placements"] == [{"role": "mpx/cairn/staging/principal", "sensitive": False}]
