"""BR-046 — changing a path revokes the OAuth lineages of the subtree, and only those."""

from __future__ import annotations

import json

from tests.support import Gateway, add_lineage, mcp, revoked_lineages, standard_tree


def _states(gw: Gateway) -> dict[str, str]:
    return {t["id"]: t["state"] for t in json.loads(gw.ok("token", "list", "--json"))}


def test_br_046_segment_change_revokes_exactly_subtree_lineages(gw: Gateway) -> None:
    standard_tree(gw)
    inside = {
        add_lineage(gw, "mpx/cairn/staging/dev"),
        add_lineage(gw, "mpx/cairn/staging/principal", "other-client"),
    }
    outside = add_lineage(gw, "mpx/cairn/prod/dev")
    tokens = [gw.issue("mpx/cairn/staging")[0], gw.issue("mpx/cairn/staging/dev")[0]]
    out = gw.ok("tree", "rename", "mpx/cairn/staging", "Préprod", "--slug", "preprod")
    assert "mpx/cairn/staging -> mpx/cairn/preprod" in out
    assert "OAuth lineages revoked: 2" in out
    assert revoked_lineages(gw) == inside
    assert outside not in revoked_lineages(gw)
    assert all(_states(gw)[t] == "active" for t in tokens)


def test_br_046_operator_token_still_valid_at_new_url(gw: Gateway) -> None:
    standard_tree(gw)
    _, value = gw.issue("mpx/cairn/staging/dev")
    gw.ok("tree", "rename", "mpx/cairn", "Cairn 2", "--slug", "cairn2")
    with gw.client() as client:
        assert mcp(client, "mpx/cairn/staging/dev", value).status_code == 404
        assert mcp(client, "mpx/cairn2/staging/dev", value).status_code == 200


def test_br_046_display_name_only_revokes_nothing(gw: Gateway) -> None:
    standard_tree(gw)
    lineage = add_lineage(gw, "mpx/cairn/staging/dev")
    gw.ok("tree", "rename", "mpx/cairn/staging", "Pré-production")
    gw.ok("tree", "rename", "mpx/cairn/staging", "Staging", "--slug", "staging")
    assert lineage not in revoked_lineages(gw)
    with gw.client() as client:
        assert mcp(client, "mpx/cairn/staging/dev").status_code == 401  # path unchanged


def test_br_046_move_revokes_lineages_and_changes_coverage(gw: Gateway) -> None:
    standard_tree(gw)
    gw.build(("/", "client", "Acme"), ("acme", "project", "Api"))
    lineage = add_lineage(gw, "mpx/cairn/staging/dev")
    _, old_ancestor = gw.issue("mpx/cairn", "former")
    _, new_ancestor = gw.issue("acme", "new")
    _, own = gw.issue("mpx/cairn/staging/dev", "own")
    refused = gw.cli("tree", "move", "mpx/cairn/staging", "acme/api")
    assert refused.code == 2
    assert "active tokens of former ancestors that stop covering: 1" in refused.out
    assert "active tokens of new ancestors that cover: 1" in refused.out
    assert "active OAuth lineages revoked: 1" in refused.out
    gw.ok("tree", "move", "mpx/cairn/staging", "acme/api", "--confirm", "mpx/cairn/staging")
    assert revoked_lineages(gw) == {lineage}
    with gw.client() as client:
        assert mcp(client, "mpx/cairn/staging/dev", own).status_code == 404
        assert mcp(client, "acme/api/staging/dev", own).status_code == 200
        assert mcp(client, "acme/api/staging/dev", new_ancestor).status_code == 200
        assert mcp(client, "acme/api/staging/dev", old_ancestor).status_code == 401
    entry = [e for e in gw.audit() if e["action"] == "tree.move"][-1]
    assert json.loads(entry["before"]) == {"path": "mpx/cairn/staging"}
    assert json.loads(entry["after"]) == {"path": "acme/api/staging"}


def test_br_046_move_keeps_level(gw: Gateway) -> None:
    standard_tree(gw)
    gw.build(("/", "client", "Acme"))
    result = gw.cli("tree", "move", "mpx/cairn/staging", "acme", "--confirm", "mpx/cairn/staging")
    assert result.code == 2
    assert "invalid level" in result.err


def test_br_046_move_refuses_duplicate_segment(gw: Gateway) -> None:
    standard_tree(gw)
    gw.build(("/", "client", "Acme"), ("acme", "project", "Api"), ("acme/api", "env", "Staging"))
    result = gw.cli(
        "tree", "move", "mpx/cairn/staging", "acme/api", "--confirm", "mpx/cairn/staging"
    )
    assert result.code == 2
    assert "segment already used under acme/api: staging" in result.err


def test_br_046_path_change_audits_each_revoked_lineage(gw: Gateway) -> None:
    """BR-066: renaming then moving leave one `lineage.revoke` entry per lineage."""
    standard_tree(gw)
    gw.build(("/", "client", "Acme"), ("acme", "project", "Api"))
    renamed = {add_lineage(gw, "mpx/cairn/staging/dev"), add_lineage(gw, "mpx/cairn/staging/dev")}
    gw.ok("tree", "rename", "mpx/cairn/staging", "Préprod", "--slug", "preprod")
    moved = add_lineage(gw, "mpx/cairn/preprod/principal")
    gw.ok("tree", "move", "mpx/cairn/preprod", "acme/api", "--confirm", "mpx/cairn/preprod")
    entries = [e for e in gw.audit() if e["action"] == "lineage.revoke"]
    assert [json.loads(e["after"])["token_id"] for e in entries] == [*sorted(renamed), moved]
    assert {e["reason"] for e in entries} == {"path_changed"}
    assert not [e for e in gw.audit() if e["action"] == "token.revoke"]


def test_br_046_rename_revokes_real_oauth_lineage_not_operator_token(gw: Gateway) -> None:
    from tests.support import grant, refresh

    standard_tree(gw)
    gw.preregister("mpx")
    _, token = gw.issue("mpx/cairn/staging")
    with gw.client() as client:
        g = grant(gw, client, "mpx/cairn/staging/dev")
        gw.ok("tree", "rename", "mpx/cairn/staging", "Acceptance", "--slug", "acceptance")
        assert mcp(client, "mpx/cairn/acceptance/dev", g["access_token"]).status_code == 401
        assert mcp(client, "mpx/cairn/staging/dev", g["access_token"]).status_code == 404
        r = refresh(client, "mpx/cairn/acceptance/dev", g["refresh_token"])
        assert r.json()["error"] == "invalid_grant"
        assert mcp(client, "mpx/cairn/acceptance/dev", token).status_code == 200


def test_br_046_locked_descendant_blocks_rename_and_move(gw: Gateway) -> None:
    """Operator's arbitration of 2026-10-28 (R1, review 407): the deletion's guard."""
    standard_tree(gw)
    gw.ok("tree", "add", "/", "client", "Acme")
    gw.ok("tree", "lock", "mpx/cairn/prod")
    for args in (
        ("tree", "rename", "mpx", "MPX2", "--slug", "mpx2"),
        ("tree", "rename", "mpx/cairn", "Other name"),
        ("tree", "move", "mpx/cairn", "acme", "--confirm", "mpx/cairn"),
    ):
        result = gw.cli(*args)
        assert result.code == 2, args
        assert "locked by mpx/cairn/prod" in result.err, args
    tree = json.loads(gw.ok("tree", "show", "--json"))
    assert "mpx/cairn/prod/dev" in [n["path"] for n in tree]
    # an unlocked sibling still moves; once the lock is lifted, the rename passes
    gw.ok("tree", "rename", "mpx/cairn/staging", "Acceptance", "--slug", "acceptance")
    gw.ok("tree", "unlock", "mpx/cairn/prod")
    gw.ok("tree", "rename", "mpx", "MPX2", "--slug", "mpx2")
