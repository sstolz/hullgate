"""BR-182 (F-015 AC-16): `tool diff` shows, for each suspended, new or removed tool of the
subtree, the fields that changed, old and new, with the upstream text escaped; `tool validate`
alone records the current definition as baseline and lifts the suspension, audits both hashes,
is refused on a locked node; an upstream that does not answer writes nothing; no access point
validates."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from hullgate import relay
from tests.fake_upstream import READ, FakeClock, Fault, tool
from tests.policy_support import audits, catalogue, rows
from tests.relay_bed import A, B, Bed, names
from tests.support import Gateway

PN = "mpx/pn"
NODE = "mpx/cairn/staging"


@pytest.fixture
def bed(gw: Gateway) -> Bed:
    b = Bed(gw)
    up = b.connection("pn", A, B)
    up.tools = [tool("look", annotations=READ), tool("seek", annotations=READ),
                tool("gone", annotations=READ)]  # fmt: skip
    gw.ok("policy", "pin", PN, "--role", A, "on")
    catalogue(gw, PN, A)  # first listing under the pin: baselines
    return b


def policy_writes(gw: Gateway) -> list[str]:
    """Audit actions of policy and pinning changes (listings, audited too, are left out)."""
    return [a["action"] for a in gw.audit() if a["action"].startswith(("policy.", "tool."))]


def pins(gw: Gateway) -> list[tuple[Any, ...]]:
    return rows(gw, "SELECT * FROM pinned_definition ORDER BY 1, 2, 3")


def suspend_look(bed: Bed) -> None:
    """Change `look` upstream and let a listing suspend it; `gone` disappears."""
    up = bed.upstream("pn")
    up.tools[0]["description"] = "rewritten"
    up.tools.pop()
    catalogue(bed.gw, PN, A)


def test_br_182_diff_shows_old_and_new_per_field(bed: Bed) -> None:
    gw = bed.gw
    suspend_look(bed)
    up = bed.upstream("pn")
    up.tools[1]["description"] = "seek v2"  # changed after the last listing: not yet suspended
    before = pins(gw)
    seen = json.loads(gw.ok("tool", "diff", NODE, "--json"))
    assert pins(gw) == before  # a read
    by_tool = {c["tool"]: c for c in seen["changes"]}
    assert {t: c["state"] for t, c in by_tool.items()} == {
        "look": "suspended", "seek": "changed", "gone": "removed",
    }  # fmt: skip
    assert by_tool["look"]["fields"] == [
        {"field": "description", "old": "tool look", "new": "rewritten"}
    ]
    assert by_tool["look"]["role"] == A
    assert by_tool["gone"]["current_sha256"] is None
    text = gw.ok("tool", "diff", NODE)
    assert "suspended  look  mpx/pn on mpx/cairn/staging/dev" in text
    assert '- "tool look"' in text
    assert '+ "rewritten"' in text


def test_br_182_diff_escapes_hostile_text(bed: Bed) -> None:
    gw = bed.gw
    bed.upstream("pn").tools[0]["description"] = "ok\x1b]0;owned\x07\x1b[2J\u009b31m\x7f"
    catalogue(gw, PN, A)
    for args in (("tool", "diff", NODE), ("tool", "diff", NODE, "--json")):
        out = gw.ok(*args)
        for byte in ("\x1b", "\x07", "\u009b", "\x7f"):
            assert byte not in out, (args, repr(byte))
    text = gw.ok("tool", "diff", NODE)
    assert "ok\\u001b]0;owned\\u0007\\u001b[2J\\x9b31m\\x7f" in text


def test_br_182_validate_records_the_baseline_and_lifts_the_suspension(bed: Bed) -> None:
    gw = bed.gw
    suspend_look(bed)
    clock = FakeClock()
    with bed.serve(gw.relay(upclock=clock)) as client:
        assert names(client, A, bed.token(A)) == ["pn__seek"]
        out = gw.ok("tool", "validate", NODE, PN)
        assert names(client, A, bed.token(A)) == ["pn__look", "pn__seek"]  # next request
        clock.advance(relay.CACHE_TTL_S + 1)
        assert names(client, A, bed.token(A)) == ["pn__look", "pn__seek"]
    entries = {e["detail"]["tool"]: e for e in audits(gw, "tool.validate")}
    assert set(entries) == {"look", "gone"}
    look = entries["look"]
    assert look["before"]["suspended"] is True
    assert look["after"] == {"sha256": look["after"]["sha256"], "suspended": False}
    assert look["before"]["sha256"] != look["after"]["sha256"]
    assert look["after"]["sha256"] in out
    assert entries["gone"]["after"]["sha256"] is None  # removed: its baseline is dropped
    left = rows(gw, "SELECT upstream_name, suspended_at FROM pinned_definition WHERE role_id = ?",
                gw.node_id(A))  # fmt: skip
    assert sorted(left) == [("look", None), ("seek", None)]
    assert json.loads(gw.ok("tool", "diff", NODE, "--json"))["changes"] == []


def test_br_182_validate_one_tool(bed: Bed) -> None:
    gw = bed.gw
    suspend_look(bed)
    bed.upstream("pn").tools[1]["description"] = "seek v2"
    catalogue(gw, PN, A)
    gw.ok("tool", "validate", NODE, PN, "--tool", "look")
    assert [e["detail"]["tool"] for e in audits(gw, "tool.validate")] == ["look"]
    suspended = rows(gw, "SELECT upstream_name FROM pinned_definition WHERE role_id = ?"
                         " AND suspended_at IS NOT NULL", gw.node_id(A))  # fmt: skip
    assert suspended == [("seek",)]
    result = gw.cli("tool", "validate", NODE, PN, "--tool", "nothing")
    assert result.code == 2, result


@pytest.mark.parametrize("node", [NODE, "mpx/cairn", A])
def test_br_182_validate_is_refused_on_a_locked_node(bed: Bed, node: str) -> None:
    gw = bed.gw
    suspend_look(bed)
    gw.ok("tree", "lock", node)
    before = pins(gw), len(gw.audit())
    calls = len(bed.upstream("pn").calls("tools/list"))
    result = gw.cli("tool", "validate", NODE, PN)
    assert result.code == 2, result
    assert "locked by" in result.err
    assert (pins(gw), len(gw.audit())) == before
    assert len(bed.upstream("pn").calls("tools/list")) == calls  # no exchange


@pytest.mark.parametrize("verb", ["diff", "validate"])
def test_br_182_an_upstream_that_does_not_answer_writes_nothing(bed: Bed, verb: str) -> None:
    gw = bed.gw
    suspend_look(bed)
    bed.upstream("pn").faults.append(Fault("status", on="tools/list", status=503))
    before = pins(gw), audits(gw, "tool.validate")
    args = ("tool", verb, NODE) + ((PN,) if verb == "validate" else ())
    result = gw.cli(*args)
    assert result.code == 2, result
    assert "nothing written" in result.err
    assert (pins(gw), audits(gw, "tool.validate")) == before


def test_br_182_no_access_point_validates(bed: Bed) -> None:
    """A client can only list and call: neither lifts a suspension (BR-181)."""
    gw = bed.gw
    suspend_look(bed)
    bed.upstream("pn").tools[0]["description"] = "tool look"  # reverted upstream
    with bed.serve() as client:
        for _ in range(2):
            assert names(client, A, bed.token(A)) == ["pn__seek"]
    assert audits(gw, "tool.validate") == []


FIELDS: dict[str, tuple[Callable[[dict[str, Any]], None], Any, Any]] = {
    "title": (lambda t: t.update(title="Look"), None, "Look"),
    "description": (lambda t: t.update(description="rewritten"), "tool look", "rewritten"),
    "inputSchema": (
        lambda t: t["inputSchema"]["properties"].update(r={"type": "string"}),
        {"type": "object", "properties": {}},
        {"type": "object", "properties": {"r": {"type": "string"}}},
    ),
    "outputSchema": (
        lambda t: t.update(outputSchema={"type": "object"}),
        None,
        {"type": "object"},
    ),
    "annotations": (
        lambda t: t["annotations"].update(openWorldHint=True),
        {"readOnlyHint": True},
        {"readOnlyHint": True, "openWorldHint": True},
    ),
}


@pytest.mark.parametrize("field", sorted(FIELDS))
def test_br_182_diff_shows_old_and_new_for_each_hashed_field(bed: Bed, field: str) -> None:
    """Each of the five hashed fields of BR-181, one at a time (lot B review, N3)."""
    gw = bed.gw
    change, old, new = FIELDS[field]
    change(bed.upstream("pn").tools[0])
    catalogue(gw, PN, A)  # the listing that suspends it
    seen = json.loads(gw.ok("tool", "diff", NODE, "--json"))
    [look] = [c for c in seen["changes"] if c["tool"] == "look"]
    assert look["state"] == "suspended"
    assert look["fields"] == [{"field": field, "old": old, "new": new}]
    text = gw.ok("tool", "diff", NODE)
    assert f"    {field}\n" in text


def test_br_182_unloadable_state_fails_the_readers_closed(bed: Bed) -> None:
    """A stored row that cannot be loaded: `tool diff`, `tool validate`, `policy show`,
    `policy arg`, `connection tools` and `export` answer a policy error naming the connection,
    exit 1, nothing written (lot B review, N2; lot C review, N2; BR-185)."""
    gw = bed.gw
    conn = gw.connect()
    try:
        conn.execute(
            "INSERT INTO argument_rule (connection_id, role_id, upstream_name, argument,"
            " allowed_values) VALUES (?, ?, 'seek', 'q', 'not json')",
            (gw.connection_id(PN), gw.node_id(A)),
        )
        conn.commit()
    finally:
        conn.close()
    before = pins(gw), policy_writes(gw)
    for args in (("tool", "diff", NODE), ("tool", "diff", NODE, "--json"),
                 ("tool", "validate", NODE, PN), ("policy", "show", PN, "--role", A),
                 ("policy", "arg", PN, "--role", A, "seek", "q", "x"),
                 ("connection", "tools", PN, "--role", A), ("export",)):  # fmt: skip
        result = gw.cli(*args)
        assert result.code == 1, (args, result)
        assert "Traceback" not in result.err, args
        assert f"policy error: {PN}:" in result.err, (args, result.err)
    assert (pins(gw), policy_writes(gw)) == before  # listings are audited, no state is written


def test_br_182_a_lone_surrogate_upstream_leaves_diff_and_validate_usable(bed: Bed) -> None:
    """Text UTF-8 cannot encode: the relay's listings fail closed, `tool diff` shows the change
    escaped, `tool validate` refuses that tool with nothing written and validates the others
    (lot B review, N2)."""
    gw = bed.gw
    up = bed.upstream("pn")
    up.tools[0]["description"] = "evil \udc80 text"
    up.tools[1]["description"] = "seek v2"
    assert gw.cli("connection", "tools", PN, "--role", A).code == 1  # fails closed
    for args in (("tool", "diff", NODE), ("tool", "diff", NODE, "--json")):
        result = gw.cli(*args)
        assert result.code == 0, (args, result)
        assert "\udc80" not in result.out
    seen = json.loads(gw.ok("tool", "diff", NODE, "--json"))
    states = {c["tool"]: c["state"] for c in seen["changes"]}
    assert states["look"] == "changed"
    assert "evil \\udc80 text" in gw.ok("tool", "diff", NODE)
    before = pins(gw), len(gw.audit())
    for cmd in (("tool", "validate", NODE, PN), ("tool", "validate", NODE, PN, "--tool", "look")):
        result = gw.cli(*cmd)
        assert result.code == 2, (cmd, result)
        assert "cannot be encoded: nothing written" in result.err
    assert (pins(gw), len(gw.audit())) == before
    gw.ok("tool", "validate", NODE, PN, "--tool", "seek")
    assert [e["detail"]["tool"] for e in audits(gw, "tool.validate")] == ["seek"]


def test_br_182_policy_arg_refuses_a_definition_that_cannot_be_encoded(bed: Bed) -> None:
    """`policy arg` on a tool not pinned yet whose upstream text holds a lone surrogate: the
    baseline taken at entry cannot hold it, so the rule is refused, exit 2, nothing written, as
    `tool validate` answers (lot C review, N1)."""
    gw = bed.gw
    up = bed.upstream("pn")
    up.tools[1]["inputSchema"] = {"type": "object", "properties": {"q": {"type": "string"}}}
    up.tools[1]["description"] = "evil \udc80 text"
    # role B: not pinned, so `seek` holds no baseline and `policy arg` takes it at entry
    before = pins(gw), policy_writes(gw), rows(gw, "SELECT count(*) FROM argument_rule")
    result = gw.cli("policy", "arg", PN, "--role", B, "seek", "q", "x")
    assert result.code == 2, result
    assert "cannot be encoded: nothing written" in result.err
    assert "Traceback" not in result.err
    assert (pins(gw), policy_writes(gw), rows(gw, "SELECT count(*) FROM argument_rule")) == before
