"""BR-079 — a tree change that creates a sharing is signalled (AC-08), without blocking.

BR-047 clarified: the CLI says, before marking or unmarking, the connections made shared.
"""

from __future__ import annotations

import json

from tests.support import Gateway


def _tree(gw: Gateway) -> None:
    gw.build(
        ("/", "client", "MPX"),
        ("/", "client", "Acme"),
        ("mpx", "project", "Cairn"),
        ("mpx/cairn", "environment", "Dev"),
        ("mpx/cairn", "environment", "Prod"),
        ("mpx/cairn/dev", "role", "Agent"),
        ("mpx/cairn/prod", "role", "Review"),
    )
    gw.service()
    gw.static("mpx/gitea-dev", "gd")


def _signals(gw: Gateway) -> list[dict[str, object]]:
    return [e for e in gw.audit() if e["action"] == "connection.share_signal"]


def test_br_079_marking_sensitive_signals_without_blocking(gw: Gateway) -> None:
    _tree(gw)
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/dev/agent")
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/prod/review")  # two non-sensitive
    result = gw.cli("tree", "sensitive", "mpx/cairn/prod", "on")
    assert result.code == 0
    lines = result.out.splitlines()
    warning = next(i for i, line in enumerate(lines) if "becomes shared" in line)
    done = next(i for i, line in enumerate(lines) if line.startswith("mpx/cairn/prod: sensitive"))
    assert warning < done  # said before acting
    assert "mpx/gitea-dev" in lines[warning]
    assert "mpx/cairn/prod/review (sensitive)" in lines[warning]
    [entry] = _signals(gw)
    assert entry["result"] == "warning"
    assert entry["reason"] == "tree.sensitive"
    assert json.loads(str(entry["detail"]))["reasons"] == ["sensitivity"]
    # unmarking removes the sharing: nothing to signal
    out = gw.ok("tree", "sensitive", "mpx/cairn/prod", "off")
    assert "becomes shared" not in out
    assert len(_signals(gw)) == 1


def test_br_079_unmarking_can_create_a_share(gw: Gateway) -> None:
    _tree(gw)
    gw.ok("tree", "sensitive", "mpx/cairn/prod", "on")
    gw.ok("tree", "sensitive", "mpx/cairn/dev", "on")
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/dev/agent")
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/prod/review")
    out = gw.ok("tree", "sensitive", "mpx/cairn/dev", "off")
    assert "becomes shared (between sensitive and non-sensitive roles)" in out
    assert len(_signals(gw)) == 1


def test_br_079_move_to_another_client_signals(gw: Gateway) -> None:
    _tree(gw)
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/dev/agent")
    result = gw.cli("tree", "move", "mpx/cairn", "acme", "--confirm", "mpx/cairn")
    assert result.code == 0
    assert "becomes shared (outside its origin client)" in result.out
    assert "acme/cairn/dev/agent (non-sensitive)" in result.out
    [entry] = _signals(gw)
    assert entry["reason"] == "tree.move"
    assert json.loads(str(entry["detail"]))["reasons"] == ["other_client"]
    # the placement remains: the signal does not block
    state = json.loads(gw.ok("connection", "status", "--json"))[0]
    assert state["placements"] == [{"role": "acme/cairn/dev/agent", "sensitive": False}]


def test_br_079_move_preview_without_confirm_writes_nothing(gw: Gateway) -> None:
    _tree(gw)
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/dev/agent")
    result = gw.cli("tree", "move", "mpx/cairn", "acme")
    assert result.code == 2
    assert "becomes shared" in result.out
    assert _signals(gw) == []
