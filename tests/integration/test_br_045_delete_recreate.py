"""BR-045 — a deleted node never comes back, nor do its tokens (N4)."""

from __future__ import annotations

import json
import sqlite3

from tests.support import Gateway, add_lineage, mcp, org, revoked_lineages, standard_tree


def _node_id(gw: Gateway, path: str) -> str:
    data = json.loads(gw.ok("tree", "show", path, "--json"))
    conn = gw.connect()
    try:
        segments = data[0]["path"].split("/")
        parent = "org"
        for segment in segments:
            row = conn.execute(
                "SELECT id FROM node WHERE parent_id = ? AND slug = ? AND deleted = 0",
                (parent, segment),
            ).fetchone()
            parent = row[0]
    finally:
        conn.close()
    return parent


def test_br_045_delete_then_recreate_revives_nothing(gw: Gateway) -> None:
    standard_tree(gw)
    gw.ok("tree", "add", "mpx/cairn/staging", "role", "Agent")
    old_id = _node_id(gw, "mpx/cairn/staging/agent")
    _, role_token = gw.issue("mpx/cairn/staging/agent")
    _, env_token = gw.issue("mpx/cairn/staging")
    lineage = add_lineage(gw, "mpx/cairn/staging/agent")
    gw.ok("tree", "delete", "mpx/cairn/staging/agent", "--confirm", "mpx/cairn/staging/agent")
    with gw.client() as client:
        assert mcp(client, "mpx/cairn/staging/agent", role_token).status_code == 404
        gw.ok("tree", "add", "mpx/cairn/staging", "role", "Agent")
        assert _node_id(gw, "mpx/cairn/staging/agent") != old_id
        refused = mcp(client, "mpx/cairn/staging/agent", role_token)
        assert refused.status_code == 401
        assert refused.json() == {"error": "invalid_token"}
        # a token placed above, not deleted, covers the new role
        assert mcp(client, "mpx/cairn/staging/agent", env_token).status_code == 200
        # a fresh token works
        _, fresh = gw.issue("mpx/cairn/staging/agent")
        assert mcp(client, "mpx/cairn/staging/agent", fresh).status_code == 200
    assert lineage in revoked_lineages(gw)
    reasons = [e["reason"] for e in gw.audit() if e["action"] == "access.refused"]
    assert reasons == ["node_deleted"]


def test_br_045_delete_revokes_subtree_in_same_transaction(gw: Gateway) -> None:
    standard_tree(gw)
    inside = [gw.issue("mpx/cairn/staging")[0], gw.issue("mpx/cairn/staging/dev")[0]]
    outside = [gw.issue("mpx/cairn")[0], gw.issue("mpx/cairn/prod/dev")[0]]
    lin_in = add_lineage(gw, "mpx/cairn/staging/principal")
    lin_out = add_lineage(gw, "mpx/cairn/prod/dev")
    out = gw.ok("tree", "delete", "mpx/cairn/staging", "--confirm", "mpx/cairn/staging")
    assert "descendants: 2" in out
    assert "active tokens revoked: 3" in out
    assert "URLs that stop answering: 2" in out
    states = {
        t["id"]: (t["state"], t["path"]) for t in json.loads(gw.ok("token", "list", "--json"))
    }
    for tid in inside:
        assert states[tid] == ("revoked", "(deleted)")
    for tid in outside:
        assert states[tid][0] == "active"
    assert revoked_lineages(gw) == {lin_in}
    assert lin_out not in revoked_lineages(gw)
    entry = [e for e in gw.audit() if e["action"] == "tree.delete"][-1]
    assert json.loads(entry["detail"]) == {"lineages_revoked": 1, "tokens_revoked": 2, "urls": 2}


def test_br_045_delete_requires_confirmation(gw: Gateway) -> None:
    standard_tree(gw)
    for confirm in ([], ["--confirm", "mpx/cairn"]):
        result = gw.cli("tree", "delete", "mpx/cairn/staging", *confirm)
        assert result.code == 2
        assert "confirmation required: --confirm mpx/cairn/staging" in result.err
        assert "descendants: 2" in result.out
    assert "staging" in gw.ok("tree", "show")


def test_br_045_identifier_never_reused(gw: Gateway) -> None:
    standard_tree(gw)
    seen = set()
    for _ in range(3):
        gw.ok("tree", "add", "mpx/cairn/staging", "role", "Tmp")
        seen.add(_node_id(gw, "mpx/cairn/staging/tmp"))
        gw.ok("tree", "delete", "mpx/cairn/staging/tmp", "--confirm", "mpx/cairn/staging/tmp")
    assert len(seen) == 3
    conn = sqlite3.connect(gw.path)
    deleted = conn.execute("SELECT count(*) FROM node WHERE slug = 'tmp' AND deleted = 1")
    assert deleted.fetchone() == (3,)
    conn.close()


def test_br_045_cascade_revocations_audited_one_entry_each(gw: Gateway) -> None:
    """BR-066: each token and each lineage revoked by the deletion has its entry."""
    standard_tree(gw)
    tokens = {gw.issue("mpx/cairn/staging")[0], gw.issue("mpx/cairn/staging/dev")[0]}
    lineage = add_lineage(gw, "mpx/cairn/staging/principal")
    gw.issue("mpx/cairn/prod/dev")  # outside the subtree: no entry
    gw.ok("tree", "delete", "mpx/cairn/staging", "--confirm", "mpx/cairn/staging")
    entries = [e for e in gw.audit() if e["action"] in ("token.revoke", "lineage.revoke")]
    revoked = {(e["action"], json.loads(e["after"])["token_id"]) for e in entries}
    assert revoked == {("token.revoke", t) for t in tokens} | {("lineage.revoke", lineage)}
    assert len(entries) == 3
    assert {e["reason"] for e in entries} == {"node_deleted"}
    assert {e["actor"] for e in entries} == {"operator-cli"}
    paths = {json.loads(e["after"])["token_id"]: e["path"] for e in entries}
    assert paths[lineage] == "mpx/cairn/staging/principal"


def test_br_045_oauth_tokens_die_with_the_node_and_never_revive(gw: Gateway) -> None:
    """Example of BR-045: real OAuth tokens, role deleted then recreated at the same path."""
    from tests.support import grant, refresh

    standard_tree(gw)
    gw.preregister("mpx")
    path = "mpx/cairn/staging/dev"
    with gw.client() as client:
        g = grant(gw, client, path)
        assert mcp(client, path, g["access_token"]).status_code == 200
        gw.ok("tree", "delete", path, "--confirm", path)
        assert mcp(client, path, g["access_token"]).status_code == 404
        assert (
            client.get(f"/.well-known/oauth-protected-resource/o/{org()}/p/{path}/mcp").status_code
            == 404
        )
        gw.ok("tree", "add", "mpx/cairn/staging", "role", "Dev")
        assert mcp(client, path, g["access_token"]).status_code == 401
        r = refresh(client, path, g["refresh_token"])
        assert (r.status_code, r.json()["error"]) == (400, "invalid_grant")
        fresh = grant(gw, client, path)
        assert mcp(client, path, fresh["access_token"]).status_code == 200


def test_br_045_client_carrying_connections_cannot_be_deleted(gw: Gateway) -> None:
    """F-006 (BR-045 clarified): the refusal names the connections; nothing is deleted."""
    standard_tree(gw)
    gw.service()
    gw.static("mpx/gitea-dev", "gd")
    gw.static("mpx/gitea-rev", "gr")
    result = gw.cli("tree", "delete", "mpx", "--confirm", "mpx")
    assert result.code == 2
    assert "mpx/gitea-dev, mpx/gitea-rev" in result.err
    assert gw.cli("tree", "show", "mpx").code == 0
    gw.ok("connection", "remove", "mpx/gitea-dev", "--confirm", "mpx/gitea-dev")
    gw.ok("connection", "remove", "mpx/gitea-rev", "--confirm", "mpx/gitea-rev")
    gw.ok("tree", "delete", "mpx", "--confirm", "mpx")


def test_br_045_role_deletion_removes_its_placements_in_same_transaction(gw: Gateway) -> None:
    standard_tree(gw)
    gw.ok("tree", "add", "/", "client", "Acme")
    gw.ok("tree", "add", "acme", "project", "Site")
    gw.ok("tree", "add", "acme/site", "environment", "Dev")
    gw.service()
    gw.static("mpx/gitea-dev", "gd")
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/staging/dev")
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/staging/principal")
    gw.ok("connection", "place", "mpx/gitea-dev", "acme/site/dev/principal", "--confirm-share")
    out = gw.ok("tree", "delete", "mpx/cairn/staging/dev", "--confirm", "mpx/cairn/staging/dev")
    assert "connection placements removed: 1" in out
    state = json.loads(gw.ok("connection", "status", "--json"))[0]
    assert [p["role"] for p in state["placements"]] == [
        "acme/site/dev/principal",
        "mpx/cairn/staging/principal",
    ]
    # a client without a connection of its own, carrying a placement of another one: deleted, the
    # placement removed with it
    gw.ok("tree", "delete", "acme", "--confirm", "acme")
    state = json.loads(gw.ok("connection", "status", "--json"))[0]
    assert [p["role"] for p in state["placements"]] == ["mpx/cairn/staging/principal"]
    unplaced = [e for e in gw.audit() if e["action"] == "connection.unplace"]
    assert [e["reason"] for e in unplaced] == ["node_deleted", "node_deleted"]
    delete = next(e for e in gw.audit() if e["action"] == "tree.delete")
    assert json.loads(delete["detail"])["placements_removed"] == 1
