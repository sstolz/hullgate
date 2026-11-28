"""AC-22: every reversible class, both directions, through CLI domain operations and the API."""

from __future__ import annotations

import json
from typing import Any

import pytest

from hullgate import changes
from tests.admin_support import Console
from tests.policy_support import catalogue, gitea
from tests.relay_bed import A, B, Bed
from tests.support import Gateway

CASES = [
    ("setting", ("settings", "set", A, "token_duration", "7d")),
    ("unset", ("settings", "unset", A, "token_duration")),
    ("lock", ("tree", "lock", A)),
    ("unlock", ("tree", "unlock", A)),
    ("sensitive", ("tree", "sensitive", "mpx/cairn/staging", "on")),
    ("place", ("connection", "place", "mpx/ab", B)),
    ("unplace", ("connection", "unplace", "mpx/ab", A)),
    ("prefix", ("connection", "prefix", "mpx/ab", "xy")),
    ("mode", ("policy", "mode", "mpx/ab", "--role", A, "write")),
    ("list_add", ("policy", "add", "mpx/ab", "--role", A, "deny", "issue_write")),
    ("list_remove", ("policy", "remove", "mpx/ab", "--role", A, "deny", "issue_write")),
    ("arg", ("policy", "arg", "mpx/ab", "--role", A, "issue_write", "method", "create")),
    ("arg_remove", ("policy", "arg-remove", "mpx/ab", "--role", A, "issue_write")),
    ("pin", ("policy", "pin", "mpx/ab", "--role", A, "on")),
    ("pin_tool", ("policy", "pin-tool", "mpx/ab", "--role", A, "issue_write")),
    ("unpin_tool", ("policy", "unpin-tool", "mpx/ab", "--role", A, "issue_write")),
    ("threshold", ("settings", "set", "/", "connection_expiry_days", "5")),
]


def latest(gw: Gateway, action: str | None = None) -> dict[str, Any]:
    return next(e for e in reversed(gw.audit()) if action is None or e["action"] == action)


def reverse(console: Console, client: Any, seq: int, **extra: Any) -> Any:
    plan = console.call(client, "GET", f"changes?seq={seq}")
    assert plan.status_code == 200, plan.text
    impact = plan.json().get("impact")
    return console.call(
        client,
        "POST",
        "changes/undo",
        {"seq": seq, **({"impact": impact["digest"]} if impact else {}), **extra},
    )


@pytest.mark.parametrize(("case", "argv"), CASES, ids=[c[0] for c in CASES])
def test_br_219_every_class_undo_and_redo(gw: Gateway, case: str, argv: tuple[str, ...]) -> None:
    bed = Bed(gw)
    gitea(bed, "ab", A)
    catalogue(gw, "mpx/ab", A)
    if case == "unset":
        gw.ok("settings", "set", A, "token_duration", "14d")
    if case == "unlock":
        gw.ok("tree", "lock", A)
    if case == "list_remove":
        gw.ok("policy", "add", "mpx/ab", "--role", A, "deny", "issue_write")
    if case in ("arg_remove", "unplace"):
        gw.ok("policy", "arg", "mpx/ab", "--role", A, "issue_write", "method", "update")
    if case == "unpin_tool":
        gw.ok("policy", "pin-tool", "mpx/ab", "--role", A, "issue_write")
    if case == "prefix":
        gw.ok("connection", "unplace", "mpx/ab", A)
    gw.ok(*argv)
    original = latest(gw)
    assert original["before"] is not None or original["after"] is not None
    saved = list(gw.audit())
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        result = reverse(console, client, original["seq"])
        assert result.status_code == 200, result.text
        envelope = latest(gw, "change.undo")
        assert json.loads(envelope["detail"])["undo_of"] == original["seq"]
        assert envelope["actor"].startswith("admin:")
        result = reverse(console, client, envelope["seq"])
        assert result.status_code == 200, result.text
    assert gw.audit()[: len(saved)] == saved
    with gw.connect() as conn:
        e = changes.entry(conn, original["seq"])
        assert changes.current(conn, e) == e["after"]


def test_br_219_lock_refuses_and_is_audited(gw: Gateway) -> None:
    Bed(gw)
    gw.ok("settings", "set", A, "token_duration", "7d")
    seq = latest(gw)["seq"]
    gw.ok("tree", "lock", "mpx/cairn")
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        result = reverse(console, client, seq)
        assert result.status_code == 409
        assert "locked" in result.json()["message"]
    assert latest(gw)["result"] == "refused"


def test_br_219_share_requires_new_confirmation(gw: Gateway) -> None:
    bed = Bed(gw)
    bed.connection("ab", A, "mpx/cairn/prod/dev", confirm=True)
    gw.ok("connection", "unplace", "mpx/ab", "mpx/cairn/prod/dev")
    seq = latest(gw)["seq"]
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        refused = reverse(console, client, seq)
        assert refused.status_code == 409
        assert refused.json()["code"] == "share_confirmation_required"
        ok = reverse(console, client, seq, confirm_share=True)
        assert ok.status_code == 200, ok.text
    confirmations = [e for e in gw.audit() if e["action"] == "connection.share_confirmed"]
    assert len(confirmations) == 2
    assert json.loads(confirmations[-1]["detail"])["undo_of"] == seq
