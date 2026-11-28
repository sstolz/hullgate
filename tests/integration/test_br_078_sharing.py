"""BR-078 — a sharing outside the client or between sensitive and non-sensitive requires a
traced confirmation (AC-07)."""

from __future__ import annotations

import json

from tests.admin_support import Console, counts
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
        ("acme", "project", "Site"),
        ("acme/site", "environment", "Dev"),
    )
    gw.ok("tree", "sensitive", "mpx/cairn/prod", "on")
    gw.service()
    gw.static("mpx/gitea-dev", "gd")


def _placements(gw: Gateway) -> int:
    conn = gw.connect()
    try:
        return int(conn.execute("SELECT count(*) FROM placement").fetchone()[0])
    finally:
        conn.close()


def test_br_078_sensitive_and_non_sensitive_needs_confirmation(gw: Gateway) -> None:
    _tree(gw)
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/dev/agent")
    audit_before = len(gw.audit())
    refused = gw.cli("connection", "place", "mpx/gitea-dev", "mpx/cairn/prod/review")
    assert refused.code == 2
    assert (
        "sharing between a sensitive and a non-sensitive role: add --confirm-share" in refused.err
    )
    assert _placements(gw) == 1
    assert len(gw.audit()) == audit_before  # nothing written
    out = gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/prod/review", "--confirm-share")
    assert "sharing confirmed" in out
    assert _placements(gw) == 2
    entry = [e for e in gw.audit() if e["action"] == "connection.share_confirmed"][-1]
    assert entry["reason"] == "sensitivity"
    detail = json.loads(entry["detail"])
    assert detail["connection"] == "mpx/gitea-dev"
    assert detail["role"] == "mpx/cairn/prod/review"
    assert detail["role_sensitive"] is True
    assert detail["other_roles"] == [{"role": "mpx/cairn/dev/agent", "sensitive": False}]


def test_br_078_other_client_needs_confirmation(gw: Gateway) -> None:
    _tree(gw)
    refused = gw.cli("connection", "place", "mpx/gitea-dev", "acme/site/dev/principal")
    assert refused.code == 2
    assert "outside the origin client (mpx)" in refused.err
    assert _placements(gw) == 0
    gw.ok("connection", "place", "mpx/gitea-dev", "acme/site/dev/principal", "--confirm-share")
    entry = [e for e in gw.audit() if e["action"] == "connection.share_confirmed"][-1]
    assert entry["reason"] == "other_client"
    detail = json.loads(entry["detail"])
    assert detail["origin_client"] == "mpx"
    assert detail["role"] == "acme/site/dev/principal"


def test_br_078_same_sensitivity_same_client_needs_nothing(gw: Gateway) -> None:
    _tree(gw)
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/dev/agent")
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/dev/principal")
    assert not [e for e in gw.audit() if e["action"] == "connection.share_confirmed"]


# -- v3 (F-016 slice 2): the console's API, and the confirmation kept on the placement ---------


def _confirmation(gw: Gateway, role_path: str) -> tuple[str | None, int | None]:
    conn = gw.connect()
    try:
        row = conn.execute(
            "SELECT share_confirmed_by, share_confirmed_at FROM placement WHERE role_id = ?",
            (gw.node_id(role_path),),
        ).fetchone()
        return (row[0], row[1])
    finally:
        conn.close()


def test_br_078_v3_api_refuses_an_unconfirmed_share_with_its_reasons(gw: Gateway) -> None:
    _tree(gw)
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/dev/agent")
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        before = counts(gw)
        for body in ({}, {"confirm_share": False}):
            refused = console.call(client, "POST", "placements", {
                "connection": "mpx/gitea-dev", "role": "mpx/cairn/prod/review", **body,
            })  # fmt: skip
            assert refused.status_code == 409, refused.text
            assert refused.json()["code"] == "share_confirmation_required"
            assert refused.json()["reasons"] == ["sensitivity"]
            assert "sensitive and a non-sensitive" in refused.json()["message"]
        assert counts(gw) == before
        placed = console.call(client, "POST", "placements", {
            "connection": "mpx/gitea-dev", "role": "mpx/cairn/prod/review", "confirm_share": True,
        })  # fmt: skip
        assert placed.status_code == 201, placed.text
        assert placed.json()["confirmed"] == ["sensitivity"]
    who, at = _confirmation(gw, "mpx/cairn/prod/review")
    assert who is not None
    assert who.startswith("admin:")
    assert at is not None
    entry = [e for e in gw.audit() if e["action"] == "connection.share_confirmed"][-1]
    assert entry["actor"] == who


def test_br_078_v3_cli_confirmation_is_kept_on_the_placement(gw: Gateway) -> None:
    _tree(gw)
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/dev/agent")
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/prod/review", "--confirm-share")
    assert _confirmation(gw, "mpx/cairn/prod/review")[0] == "operator-cli"
    assert _confirmation(gw, "mpx/cairn/dev/agent") == (None, None)
