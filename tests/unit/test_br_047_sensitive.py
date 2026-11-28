"""BR-047 — only an environment is sensitive, and so is its whole subtree."""

from __future__ import annotations

import json

import pytest

from hullgate import tree
from tests.support import Gateway, standard_tree


def _loaded(gw: Gateway) -> tree.Tree:
    conn = gw.connect()
    try:
        return tree.Tree.load(conn)
    finally:
        conn.close()


@pytest.mark.parametrize("path", ["mpx", "mpx/cairn", "mpx/cairn/staging/dev", "/"])
def test_br_047_only_environment_can_be_marked(gw: Gateway, path: str) -> None:
    standard_tree(gw)
    result = gw.cli("tree", "sensitive", path, "on")
    assert result.code == 2
    assert "only an environment can be marked" in result.err


def test_br_047_role_inherits_environment_sensitivity(gw: Gateway) -> None:
    standard_tree(gw)
    t = _loaded(gw)
    for path, expected in [
        ("mpx/cairn/prod", True),
        ("mpx/cairn/prod/dev", True),
        ("mpx/cairn/prod/principal", True),
        ("mpx/cairn/staging", False),
        ("mpx/cairn/staging/dev", False),
        ("mpx/cairn", False),
        ("mpx", False),
    ]:
        assert t.is_sensitive(t.resolve(path)) is expected, path
    show = json.loads(gw.ok("tree", "show", "mpx/cairn/prod", "--json"))
    assert all(item["sensitive"] for item in show)


def test_br_047_role_added_later_is_sensitive(gw: Gateway) -> None:
    standard_tree(gw)
    gw.ok("tree", "add", "mpx/cairn/prod", "role", "Reviewer")
    t = _loaded(gw)
    assert t.is_sensitive(t.resolve("mpx/cairn/prod/reviewer"))


def test_br_047_unmark_counts_reopened_tokens_and_audits(gw: Gateway) -> None:
    standard_tree(gw)
    gw.issue("mpx/cairn", "project")
    gw.issue("mpx", "client")
    gw.issue("mpx/cairn/prod/dev", "role")  # placed below the environment: not reopened
    out = gw.ok("tree", "sensitive", "mpx/cairn/prod", "off")
    assert "accepted again: 2" in out
    entry = [e for e in gw.audit() if e["action"] == "tree.sensitive"][-1]
    assert json.loads(entry["detail"]) == {"reopened_tokens": 2}
    assert json.loads(entry["before"]) == {"sensitive": True}
    assert json.loads(entry["after"]) == {"sensitive": False}
    t = _loaded(gw)
    assert not t.is_sensitive(t.resolve("mpx/cairn/prod/dev"))


def test_br_047_marking_refused_under_lock(gw: Gateway) -> None:
    standard_tree(gw)
    gw.ok("tree", "lock", "mpx/cairn")
    result = gw.cli("tree", "sensitive", "mpx/cairn/staging", "on")
    assert result.code == 2
    assert "locked by mpx/cairn" in result.err


def test_br_047_marking_counts_env_and_long_role_tokens_without_revoking(gw: Gateway) -> None:
    """Operator's decision: marking shows and audits two counts; no revocation."""
    gw.build(
        ("/", "client", "MPX"), ("mpx", "project", "Cairn"), ("mpx/cairn", "environment", "Prod")
    )
    gw.issue("mpx/cairn/prod", "env")
    gw.issue("mpx/cairn/prod/principal", "long", "--duration", "30d")
    gw.issue("mpx/cairn/prod/principal", "short", "--duration", "2d")
    out = gw.ok("tree", "sensitive", "mpx/cairn/prod", "on")
    assert "which will no longer pass: 1" in out
    assert "more than 7 days (not revoked): 1" in out
    entry = [e for e in gw.audit() if e["action"] == "tree.sensitive"][-1]
    assert json.loads(entry["detail"]) == {"env_tokens": 1, "long_role_tokens": 1}
    states = [t["state"] for t in json.loads(gw.ok("token", "list", "--json"))]
    assert states == ["active", "active", "active"]


def test_br_047_marking_lists_connections_made_shared(gw: Gateway) -> None:
    """F-006 (BR-047 clarified, BR-079): the list shown before marking grows with the connections
    the change makes shared."""
    standard_tree(gw)
    gw.ok("tree", "sensitive", "mpx/cairn/prod", "off")
    gw.service()
    gw.static("mpx/gitea-dev", "gd")
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/staging/dev")
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/prod/dev")
    out = gw.ok("tree", "sensitive", "mpx/cairn/staging", "on")
    assert "connection mpx/gitea-dev becomes shared" in out
    assert "mpx/cairn/staging/dev (sensitive)" in out
