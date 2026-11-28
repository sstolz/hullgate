"""BR-049 — a node's lock freezes its subtree; only that node can be unlocked."""

from __future__ import annotations

import json

import pytest

from tests.support import Gateway, org, standard_tree


def _snapshot(gw: Gateway) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    conn = gw.connect()
    try:
        nodes = conn.execute("SELECT * FROM node ORDER BY id").fetchall()
        settings = conn.execute("SELECT * FROM setting ORDER BY node_id, key").fetchall()
    finally:
        conn.close()
    return nodes, settings


CHANGES_UNDER_LOCK = [
    ("tree", "add", "mpx/cairn", "environment", "Qa"),
    ("tree", "add", "mpx/cairn/staging", "role", "Ops"),
    ("tree", "rename", "mpx/cairn/staging", "Préprod"),
    ("tree", "rename", "mpx/cairn/staging/dev", "Dev", "--slug", "developer"),
    ("tree", "delete", "mpx/cairn/staging/dev", "--confirm", "mpx/cairn/staging/dev"),
    ("tree", "sensitive", "mpx/cairn/staging", "on"),
    ("settings", "set", "mpx/cairn/staging", "token_duration", "3d"),
    ("settings", "set", "mpx/cairn", "token_duration", "3d"),
    ("tree", "lock", "mpx/cairn/staging"),
    ("tree", "move", "mpx/cairn/staging", "acme/api", "--confirm", "mpx/cairn/staging"),
]


@pytest.mark.parametrize("command", CHANGES_UNDER_LOCK)
def test_br_049_change_on_locked_subtree_refused_naming_locker(
    gw: Gateway, command: tuple[str, ...]
) -> None:
    standard_tree(gw)
    gw.build(("/", "client", "Acme"), ("acme", "project", "Api"))
    gw.ok("tree", "lock", "mpx/cairn")
    before = _snapshot(gw)
    result = gw.cli(*command)
    assert result.code == 2, result
    assert "locked by mpx/cairn" in result.err
    assert _snapshot(gw) == before


def test_br_049_move_into_locked_parent_refused(gw: Gateway) -> None:
    standard_tree(gw)
    gw.build(("/", "client", "Acme"), ("acme", "project", "Api"))
    gw.ok("tree", "lock", "acme")
    result = gw.cli("tree", "move", "mpx/cairn", "acme", "--confirm", "mpx/cairn")
    assert result.code == 2
    assert "locked by acme" in result.err


def test_br_049_delete_of_ancestor_of_locked_node_refused(gw: Gateway) -> None:
    standard_tree(gw)
    gw.ok("tree", "lock", "mpx/cairn/prod")
    result = gw.cli("tree", "delete", "mpx", "--confirm", "mpx")
    assert result.code == 2
    assert "locked by mpx/cairn/prod" in result.err


def test_br_049_only_locking_node_unlocks(gw: Gateway) -> None:
    standard_tree(gw)
    gw.ok("tree", "lock", "mpx/cairn")
    refused = gw.cli("tree", "unlock", "mpx/cairn/prod")
    assert refused.code == 2
    assert "locked by mpx/cairn" in refused.err
    gw.ok("tree", "lock", "mpx")  # a lock above, set afterwards
    refused = gw.cli("tree", "unlock", "mpx/cairn")
    assert refused.code == 2
    assert "locked by mpx" in refused.err
    gw.ok("tree", "unlock", "mpx")
    gw.ok("tree", "unlock", "mpx/cairn")
    gw.ok("tree", "add", "mpx/cairn", "environment", "Qa")


def test_br_049_lock_through_settings_and_effective_origin(gw: Gateway) -> None:
    standard_tree(gw)
    gw.ok("settings", "set", "mpx/cairn", "lock", "true")
    data = json.loads(gw.ok("settings", "get", "mpx/cairn/prod/dev", "--json"))
    assert {"key": "lock", "value": "true", "origin": "mpx/cairn"} in data
    show = json.loads(gw.ok("tree", "show", "--json"))
    locked = {item["path"]: item["locked_by"] for item in show}
    assert locked["mpx/cairn/prod/dev"] == "mpx/cairn"
    assert locked["mpx"] is None
    gw.ok("settings", "set", "mpx/cairn", "lock", "false")
    gw.ok("tree", "add", "mpx/cairn", "environment", "Qa")


def test_br_049_explicit_false_below_cannot_mask_a_later_lock(gw: Gateway) -> None:
    standard_tree(gw)
    gw.ok("settings", "set", "mpx/cairn/prod", "lock", "false")  # no override written
    gw.ok("tree", "lock", "mpx")
    result = gw.cli("tree", "rename", "mpx/cairn/prod", "Production")
    assert result.code == 2
    assert "locked by mpx" in result.err


def test_br_049_organisation_lock(gw: Gateway) -> None:
    standard_tree(gw)
    gw.ok("tree", "lock", "/")
    assert gw.cli("tree", "add", "/", "client", "Acme").code == 2
    gw.ok("tree", "unlock", "/")
    gw.ok("tree", "add", "/", "client", "Acme")


def test_br_049_tokens_issue_and_revoke_ignore_lock(gw: Gateway) -> None:
    standard_tree(gw)
    gw.ok("tree", "lock", "/")
    token_id, _ = gw.issue("mpx/cairn/staging/dev")
    gw.ok("token", "revoke", token_id)


def test_br_049_locked_descendant_blocks_ancestor_rename_and_move(gw: Gateway) -> None:
    """S2: guard of the locked descendants (BR-046 amended)."""
    standard_tree(gw)
    gw.ok("tree", "add", "/", "client", "Acme")
    gw.ok("tree", "lock", "mpx/cairn/prod/dev")
    for args in (
        ("tree", "rename", "mpx/cairn", "Other", "--slug", "other"),
        ("tree", "move", "mpx/cairn", "acme", "--confirm", "mpx/cairn"),
    ):
        r = gw.cli(*args)
        assert r.code == 2
        assert "locked by mpx/cairn/prod/dev" in r.err


def test_br_049_clients_are_outside_the_lock(gw: Gateway) -> None:
    """S5: `client add` / `remove` and RFC 7591 registration stay possible under a lock."""
    standard_tree(gw)
    gw.ok("tree", "lock", "mpx")
    gw.ok("client", "add", "c1", "--path", "mpx/cairn", "--redirect-uri", "http://localhost:1/cb")
    gw.ok("client", "remove", "c1")
    gw.ok("client", "dcr", "on")
    with gw.client() as client:
        r = client.post(
            f"/o/{org()}/oauth/p/mpx/cairn/staging/dev/register",
            json={"redirect_uris": ["http://localhost:1/cb"]},
        )
    assert r.status_code == 201


def test_br_049_unlock_removes_the_override(gw: Gateway) -> None:
    """S12: unlocking removes the override; only the organisation carries `false`."""
    standard_tree(gw)
    gw.ok("tree", "lock", "mpx/cairn")
    gw.ok("tree", "unlock", "mpx/cairn")
    conn = gw.connect()
    try:
        rows = conn.execute("SELECT node_id, value FROM setting WHERE key = 'lock'").fetchall()
    finally:
        conn.close()
    assert rows == [("org", "false")]


def test_br_049_placement_is_a_setting_of_the_role(gw: Gateway) -> None:
    """F-006 (BR-049 clarified, H2): placing and removing a placement are refused under a locked
    node, the refusal names the node; so is `connection remove` while a placement is there."""
    standard_tree(gw)
    gw.service()
    gw.static("mpx/gitea-dev", "gd")
    gw.static("mpx/gitea-rev", "gr")
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/staging/dev")
    gw.ok("tree", "lock", "mpx/cairn/staging")
    for args in (
        ("connection", "place", "mpx/gitea-rev", "mpx/cairn/staging/dev"),
        ("connection", "unplace", "mpx/gitea-dev", "mpx/cairn/staging/dev"),
        ("connection", "remove", "mpx/gitea-dev", "--confirm", "mpx/gitea-dev"),
    ):
        result = gw.cli(*args)
        assert result.code == 2, args
        assert "locked by mpx/cairn/staging" in result.err
    # outside the locked subtree: allowed; a connection not placed under the lock can be removed
    gw.ok("connection", "place", "mpx/gitea-rev", "mpx/cairn/prod/dev")
    gw.ok("connection", "remove", "mpx/gitea-rev", "--confirm", "mpx/gitea-rev")
