"""BR-076 — the tool prefix is short, unique, fixed once placed (AC-05)."""

from __future__ import annotations

import json

import pytest

from tests.support import Gateway, standard_tree


@pytest.mark.parametrize("prefix", ["g", "abcdefghi", "GR", "g-r", "g_r", ""])
def test_br_076_invalid_prefix_refused(gw: Gateway, prefix: str) -> None:
    standard_tree(gw)
    gw.service()
    result = gw.cli(
        "connection", "add", "mpx/gitea-dev", "--service", "gitea-dev", "--provided",
        "--file", "-", "--prefix", prefix, stdin="v\n",
    )  # fmt: skip
    assert result.code == 2
    assert json.loads(gw.ok("connection", "list", "--json")) == []


def test_br_076_prefix_unique_in_organisation(gw: Gateway) -> None:
    standard_tree(gw)
    gw.ok("tree", "add", "/", "client", "Acme")
    gw.service()
    gw.static("mpx/a", "gr")
    result = gw.cli(
        "connection", "add", "acme/b", "--service", "gitea-dev", "--provided", "--file",
        "-", "--prefix", "gr", stdin="v\n",
    )  # fmt: skip
    assert result.code == 2
    assert "prefix already taken" in result.err
    gw.static("acme/b", "g8")
    assert gw.cli("connection", "prefix", "acme/b", "gr").code == 2


def test_br_076_prefix_changes_before_placement_only(gw: Gateway) -> None:
    standard_tree(gw)
    gw.service()
    gw.static("mpx/gitea-dev", "gd")
    gw.ok("connection", "prefix", "mpx/gitea-dev", "gr")
    assert json.loads(gw.ok("connection", "status", "--json"))[0]["prefix"] == "gr"
    assert [e["action"] for e in gw.audit()].count("connection.prefix") == 1
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/staging/dev")
    result = gw.cli("connection", "prefix", "mpx/gitea-dev", "gx")
    assert result.code == 2
    assert "prefix fixed" in result.err
    gw.ok("connection", "unplace", "mpx/gitea-dev", "mpx/cairn/staging/dev")
    gw.ok("connection", "prefix", "mpx/gitea-dev", "gx")
