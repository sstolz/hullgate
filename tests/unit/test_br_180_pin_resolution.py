"""BR-180 (F-015 AC-14): a tool is pinned when it has a tool pin or an argument rule, otherwise
when its placement pin says `on`, otherwise, with the pin at `inherit`, when the effective
`pinning` is `on`, or `auto` on a sensitive placement. The pure table over every combination,
then the same rows on a database, with `settings get pinning` read on each row."""

from __future__ import annotations

import itertools
import json

import pytest

from hullgate import policy
from tests.fake_upstream import READ, tool
from tests.policy_support import catalogue
from tests.relay_bed import A, Bed, S
from tests.support import Gateway

PLACEMENT_PINS = (None, "on", "off")
PINNINGS = ("auto", "on", "off")


def expected(tool_pin: bool, rule: bool, pin: str | None, pinning: str, sensitive: bool) -> bool:
    if tool_pin or rule:
        return True
    if pin is not None:
        return pin == "on"
    return pinning == "on" or (pinning == "auto" and sensitive)


ROWS = list(itertools.product((False, True), (False, True), PLACEMENT_PINS, PINNINGS,
                              (False, True)))  # fmt: skip


@pytest.mark.parametrize(("tool_pin", "rule", "pin", "pinning", "sensitive"), ROWS)
def test_br_180_resolution_table(tool_pin: bool, rule: bool, pin: str | None, pinning: str,
                                 sensitive: bool) -> None:  # fmt: skip
    assert policy.is_pinned(tool_pin, rule, pin, pinning, sensitive) == expected(
        tool_pin, rule, pin, pinning, sensitive
    )
    whole = policy.pinned_whole(pin, pinning, sensitive)
    assert whole == expected(False, False, pin, pinning, sensitive)


def test_br_180_the_table_has_every_combination() -> None:
    assert len(ROWS) == 2 * 2 * 3 * 3 * 2


DB_ROWS = list(itertools.product(PLACEMENT_PINS, PINNINGS, (False, True)))


@pytest.mark.parametrize(("pin", "pinning", "sensitive"), DB_ROWS)
def test_br_180_database_rows_agree_with_settings_get(
    gw: Gateway, pin: str | None, pinning: str, sensitive: bool
) -> None:
    bed = Bed(gw)
    role = S if sensitive else A
    up = bed.connection("pn", role)
    up.tools = [tool("plain", annotations=READ), tool("pinned", annotations=READ),
                tool("ruled", {"type": "object", "properties": {"m": {"type": "string"}}},
                     READ)]  # fmt: skip
    catalogue(gw, "mpx/pn", role)
    if pinning != "auto":
        gw.ok("settings", "set", "mpx/cairn", "pinning", pinning)
    if pin is not None:
        gw.ok("policy", "pin", "mpx/pn", "--role", role, pin)
    gw.ok("policy", "pin-tool", "mpx/pn", "--role", role, "pinned")
    gw.ok("policy", "arg", "mpx/pn", "--role", role, "ruled", "m", "x")
    got = json.loads(gw.ok("settings", "get", role, "--json"))
    [entry] = [e for e in got if e["key"] == "pinning"]
    assert entry["value"] == pinning  # what `settings get` says is what the resolution reads
    conn = gw.connect()
    try:
        p = policy.ENGINE.load(conn, gw.node_id(role)).placements[gw.connection_id("mpx/pn")]
    finally:
        conn.close()
    assert p.pinning == pinning
    assert p.pinned("plain") == expected(False, False, pin, pinning, sensitive)
    assert p.pinned("pinned") is True
    assert p.pinned("ruled") is True
    rows = json.loads(gw.ok("policy", "preview", role, "--json"))["tools"]
    shown = {t["upstream_name"]: t["pinned"] for t in rows}
    assert shown == {"plain": p.pinned("plain"), "pinned": True, "ruled": True}
