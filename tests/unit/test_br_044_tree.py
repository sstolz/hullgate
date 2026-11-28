"""BR-044 — the tree has five fixed levels and one URL per role."""

from __future__ import annotations

import pytest

from hullgate import tree
from tests.support import PUBLIC_URL, Gateway


def _counts(gw: Gateway) -> tuple[int, int]:
    conn = gw.connect()
    try:
        nodes = conn.execute("SELECT count(*) FROM node").fetchone()[0]
        entries = conn.execute("SELECT count(*) FROM audit").fetchone()[0]
    finally:
        conn.close()
    return int(nodes), int(entries)


@pytest.mark.parametrize(
    ("name", "segment"),
    [
        ("Prod", "prod"),
        ("Équipe Données", "equipe-donnees"),
        ("  API -- v2  ", "api-v2"),
        ("Ça_va?", "ca-va"),
    ],
)
def test_br_044_segment_derived_from_name(name: str, segment: str) -> None:
    assert tree.derive_segment(name) == segment


def test_br_044_example_duplicate_segment_refused(gw: Gateway) -> None:
    gw.build(("/", "client", "mpx"), ("mpx", "project", "cairn"), ("mpx/cairn", "env", "prod"))
    before = _counts(gw)
    result = gw.cli("tree", "add", "mpx/cairn", "environment", "Prod")
    assert result.code == 2
    assert "segment already used under mpx/cairn: prod" in result.err
    assert _counts(gw) == before


def test_br_044_segment_unique_between_siblings_only(gw: Gateway) -> None:
    gw.build(
        ("/", "client", "acme"),
        ("/", "client", "mpx"),
        ("acme", "project", "api"),
        ("mpx", "project", "api"),
    )
    conn = gw.connect()
    try:
        loaded = tree.Tree.load(conn)
    finally:
        conn.close()
    acme, mpx = loaded.find("acme/api"), loaded.find("mpx/api")
    assert acme is not None
    assert mpx is not None
    assert acme.id != mpx.id


@pytest.mark.parametrize("slug", ["Prod", "-prod", "prod-", "pr--od", "pr_od", "a" * 41, "", "é"])
def test_br_044_invalid_explicit_segment_refused(gw: Gateway, slug: str) -> None:
    before = _counts(gw)
    result = gw.cli("tree", "add", "/", "client", "Client", f"--slug={slug}")
    assert result.code == 2
    assert "invalid segment" in result.err
    assert _counts(gw) == before


def test_br_044_segment_of_forty_characters_accepted(gw: Gateway) -> None:
    gw.ok("tree", "add", "/", "client", "Client", "--slug", "a" * 40)


def test_br_044_empty_derived_segment_refused(gw: Gateway) -> None:
    result = gw.cli("tree", "add", "/", "client", "!!!")
    assert result.code == 2
    assert "empty derived segment" in result.err


@pytest.mark.parametrize("name", ["", "x" * 81, "a\x00b", "a\nb"])
def test_br_044_invalid_display_name_refused(gw: Gateway, name: str) -> None:
    result = gw.cli("tree", "add", "/", "client", name, "--slug", "ok")
    assert result.code == 2
    assert "invalid display name" in result.err


@pytest.mark.parametrize(
    ("parent", "level"),
    [
        ("/", "project"),
        ("/", "environment"),
        ("/", "role"),
        ("mpx", "client"),
        ("mpx", "environment"),
        ("mpx/cairn", "role"),
        ("mpx/cairn/prod/principal", "role"),
    ],
)
def test_br_044_level_must_follow_parent(gw: Gateway, parent: str, level: str) -> None:
    gw.build(("/", "client", "mpx"), ("mpx", "project", "cairn"), ("mpx/cairn", "env", "prod"))
    before = _counts(gw)
    result = gw.cli("tree", "add", parent, level, "X")
    assert result.code == 2
    assert "invalid level" in result.err
    assert _counts(gw) == before


def test_br_044_unknown_level_refused(gw: Gateway) -> None:
    result = gw.cli("tree", "add", "/", "organisation", "X")
    assert result.code == 2
    assert "unknown level" in result.err


def test_br_044_environment_creates_principal_role(gw: Gateway) -> None:
    gw.build(("/", "client", "mpx"), ("mpx", "project", "cairn"))
    out = gw.ok("tree", "add", "mpx/cairn", "environment", "Prod")
    assert "mpx/cairn/prod/principal" in out
    assert f"{PUBLIC_URL}/o/{gw.org_id}/p/mpx/cairn/prod/principal/mcp" in out
    gw.ok("tree", "rename", "mpx/cairn/prod/principal", "Agent", "--slug", "agent")
    gw.ok("tree", "delete", "mpx/cairn/prod/agent", "--confirm", "mpx/cairn/prod/agent")


def test_br_044_role_url_shown_by_tree_show(gw: Gateway) -> None:
    gw.build(
        ("/", "client", "mpx"),
        ("mpx", "project", "cairn"),
        ("mpx/cairn", "env", "prod"),
        ("mpx/cairn/prod", "role", "dev"),
    )
    show = gw.ok("tree", "show", "mpx/cairn/prod/dev")
    assert f"{PUBLIC_URL}/o/{gw.org_id}/p/mpx/cairn/prod/dev/mcp" in show


def test_br_044_unknown_parent_refused(gw: Gateway) -> None:
    result = gw.cli("tree", "add", "nope", "project", "X")
    assert result.code == 2
    assert "unknown path: nope" in result.err


def test_br_044_organisation_is_unique_and_not_deletable(gw: Gateway) -> None:
    result = gw.cli("tree", "delete", "/", "--confirm", "/")
    assert result.code == 2
    assert "the organisation cannot be deleted" in result.err
    gw.ok("tree", "rename", "/", "Monolith")
    assert "« Monolith »" in gw.ok("tree", "show")
    refused = gw.cli("tree", "rename", "/", "Monolith", "--slug", "mono")
    assert refused.code == 2


def test_br_044_reserved_words_are_ordinary_segments(gw: Gateway) -> None:
    for name in ("oauth", "well-known", "mcp", "admin", "p"):
        gw.ok("tree", "add", "/", "client", name)


@pytest.mark.parametrize("path", [" mpx", "mpx ", "mpx/cairn ", "MPX"])
def test_br_044_paths_are_not_silently_cleaned(gw: Gateway, path: str) -> None:
    gw.build(("/", "client", "mpx"), ("mpx", "project", "cairn"))
    result = gw.cli("tree", "show", path)
    assert result.code == 2
    assert "unknown path" in result.err


def test_br_044_move_to_parent_where_segment_taken_refused(gw: Gateway) -> None:
    """S20: uniqueness among siblings also holds on a move; nothing is written."""
    gw.build(
        ("/", "client", "MPX"),
        ("mpx", "project", "Cairn"),
        ("mpx", "project", "Other"),
        ("mpx/cairn", "environment", "Dev"),
        ("mpx/other", "environment", "Dev"),
    )
    before = gw.ok("export")
    r = gw.cli("tree", "move", "mpx/cairn/dev", "mpx/other", "--confirm", "mpx/cairn/dev")
    assert r.code == 2
    assert "segment already used under mpx/other: dev" in r.err
    assert gw.ok("export") == before
