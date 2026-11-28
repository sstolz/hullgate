"""BR-075 — a deleted connection never lives again, nor do its tokens (AC-04)."""

from __future__ import annotations

import json

from tests.support import Gateway, standard_tree


def _ciphertext(gw: Gateway) -> bytes:
    conn = gw.connect()
    try:
        row = conn.execute("SELECT tokens_enc FROM connection WHERE removed = 0").fetchone()
    finally:
        conn.close()
    return bytes(row[0])


def test_br_075_remove_erases_tokens_and_placements_then_recreate_is_fresh(gw: Gateway) -> None:
    standard_tree(gw)
    gw.service()
    gw.static("mpx/gitea-dev", "gd")
    old_id = gw.connection_id("mpx/gitea-dev")
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/staging/dev")
    sealed = _ciphertext(gw)
    assert gw.cli("connection", "remove", "mpx/gitea-dev").code == 2  # confirmation required
    out = gw.ok("connection", "remove", "mpx/gitea-dev", "--confirm", "mpx/gitea-dev")
    assert "placements removed: 1" in out
    conn = gw.connect()
    try:
        assert conn.execute("SELECT count(*) FROM placement").fetchone() == (0,)
        row = conn.execute(
            "SELECT removed, tokens_enc, service_id FROM connection WHERE id = ?", (old_id,)
        ).fetchone()
        assert row == (1, None, None)  # id reserved, token erased
    finally:
        conn.close()
    files = [gw.path, gw.path.with_name(gw.path.name + "-wal")]
    for f in files:
        if f.exists():
            assert sealed not in f.read_bytes(), f
    # recreated with the same name and prefix (freed): another connection, no old token
    gw.ok(
        "connection", "add", "mpx/gitea-dev", "--service", "gitea-dev", "--provided",
        "--file", "-", "--prefix", "gd", stdin="other-value\n",
    )  # fmt: skip
    assert gw.connection_id("mpx/gitea-dev") != old_id
    state = json.loads(gw.ok("connection", "status", "mpx/gitea-dev", "--json"))[0]
    assert state["placements"] == []
    assert _ciphertext(gw) != sealed


def test_br_075_removed_connection_is_invisible(gw: Gateway) -> None:
    standard_tree(gw)
    gw.service()
    gw.static("mpx/gitea-dev", "gd")
    gw.ok("connection", "remove", "mpx/gitea-dev", "--confirm", "mpx/gitea-dev")
    assert json.loads(gw.ok("connection", "list", "--json")) == []
    assert gw.cli("connection", "status", "mpx/gitea-dev").code == 2
    assert gw.cli("connection", "place", "mpx/gitea-dev", "mpx/cairn/staging/dev").code == 2


def test_br_075_removal_audited_upstream_revocation_skipped(gw: Gateway) -> None:
    standard_tree(gw)
    gw.service()
    gw.static("mpx/gitea-dev", "gd")
    out = gw.ok("connection", "remove", "mpx/gitea-dev", "--confirm", "mpx/gitea-dev")
    assert "revocation at the provider: unsupported" in out
    actions = {e["action"]: e for e in gw.audit()}
    assert actions["connection.remove"]["path"] == "mpx/gitea-dev"
    assert actions["connection.upstream_revoke"]["result"] == "skipped"
    assert actions["connection.upstream_revoke"]["reason"] == "unsupported"


def test_br_075_remove_refused_while_placed_under_lock(gw: Gateway) -> None:
    standard_tree(gw)
    gw.service()
    gw.static("mpx/gitea-dev", "gd")
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/staging/dev")
    gw.ok("tree", "lock", "mpx/cairn")
    result = gw.cli("connection", "remove", "mpx/gitea-dev", "--confirm", "mpx/gitea-dev")
    assert result.code == 2
    assert "locked by mpx/cairn" in result.err
    gw.ok("tree", "unlock", "mpx/cairn")
    gw.ok("connection", "remove", "mpx/gitea-dev", "--confirm", "mpx/gitea-dev")
