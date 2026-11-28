"""BR-185, BR-122, BR-111 (F-015 AC-19): a `tools/call` refused by the policy is audited `refused`
with one reason of the closed list; an evaluator exception at `tools/list` writes one
`relay.list_omitted` entry with reason `policy_error` per (list, connection), and `policy preview`
exits non-zero naming the connection; no argument value is written in the database. `suspended`
is reached by pinning (lot B), here and in `test_br_181_suspension.py`; a state that cannot be
loaded empties the role in the preview too, naming each connection (lot A review, N2)."""

from __future__ import annotations

from typing import Any

import pytest

from hullgate import policy
from tests.fake_upstream import DESTRUCTIVE, READ, tool
from tests.policy_support import audits, catalogue, gitea
from tests.relay_bed import A, Bed, S, call, names, unknown
from tests.support import Gateway

CLOSED_LIST = {"suspended", "denied", "not_allowlisted", "write_closed", "above_mode",
               "argument_value", "policy_error"}  # fmt: skip
MARKER = "zq-argument-marker-7731"


@pytest.fixture
def bed(gw: Gateway) -> Bed:
    return Bed(gw)


def test_br_185_closed_list_is_the_literal() -> None:
    assert set(policy.REASONS) == CLOSED_LIST
    assert len(policy.REASONS) == 7


def test_br_185_each_reason_reached_and_read_back(
    bed: Bed, monkeypatch: pytest.MonkeyPatch
) -> None:
    gw = bed.gw
    up = bed.connection("au", A)
    up.tools = [tool("r", annotations=READ), tool("d", annotations=DESTRUCTIVE), tool("x")]
    gitea(bed, "gt", A)
    sens = bed.connection("se", S)
    sens.tools = [tool("w"), tool("u", annotations=READ)]
    for name, role in (("mpx/au", A), ("mpx/gt", A), ("mpx/se", S)):
        catalogue(gw, name, role)
    gw.ok("policy", "add", "mpx/au", "--role", A, "deny", "x")
    gw.ok("policy", "add", "mpx/se", "--role", S, "allowlist", "w")
    gw.ok("policy", "add", "mpx/gt", "--role", A, "open", "pull_request_write")
    gw.ok("policy", "arg", "mpx/gt", "--role", A, "pull_request_write", "method", "create")
    with bed.serve() as client:
        assert unknown(call(client, A, bed.token(A), "au__x"))
        assert unknown(call(client, A, bed.token(A), "au__d"))
        assert unknown(call(client, S, bed.token(S), "se__u"))
        assert unknown(call(client, S, bed.token(S), "se__w"))
        assert unknown(call(client, A, bed.token(A), "gt__pull_request_write",
                            {"method": MARKER}))  # fmt: skip
        original = policy.RoleState.decide

        def broken(self: policy.RoleState, *args: Any) -> policy.Decision:
            raise RuntimeError("evaluator failure")

        monkeypatch.setattr(policy.RoleState, "decide", broken)
        assert unknown(call(client, A, bed.token(A), "au__r"))
        monkeypatch.setattr(policy.RoleState, "decide", original)
    got = [(a["detail"]["public_name"], a["result"], a["reason"])
           for a in audits(gw, "relay.tool_call")]  # fmt: skip
    assert got == [
        ("au__x", "refused", "denied"),
        ("au__d", "refused", "above_mode"),
        ("se__u", "refused", "not_allowlisted"),
        ("se__w", "refused", "write_closed"),
        ("gt__pull_request_write", "refused", "argument_value"),
        ("au__r", "refused", "policy_error"),
    ]
    assert {r for _, _, r in got} == CLOSED_LIST - {"suspended"}
    _no_argument_value(gw)


def test_br_185_evaluator_exception_at_list_and_preview(
    bed: Bed, monkeypatch: pytest.MonkeyPatch
) -> None:
    gw = bed.gw
    bed.connection("e1", A)
    bed.connection("e2", A)
    bad = gw.connection_id("mpx/e2")
    original = policy.RoleState.decide

    def broken(self: policy.RoleState, *args: Any) -> policy.Decision:
        if args[1] == bad:
            raise RuntimeError("evaluator failure")
        return original(self, *args)

    monkeypatch.setattr(policy.RoleState, "decide", broken)
    with bed.serve() as client:
        assert names(client, A, bed.token(A)) == ["e1__read", "e1__write"]
        assert names(client, A, bed.token(A)) == ["e1__read", "e1__write"]
    omitted = [(a["path"], a["reason"]) for a in audits(gw, "relay.list_omitted")]
    assert omitted == [("mpx/e2", "policy_error"), ("mpx/e2", "policy_error")]
    real_verdict = policy.RoleState.verdict

    def broken_verdict(self: policy.RoleState, cid: str, *args: Any) -> policy.Verdict:
        if cid == bad:
            raise RuntimeError("evaluator failure")
        return real_verdict(self, cid, *args)

    monkeypatch.setattr(policy.RoleState, "verdict", broken_verdict)
    result = gw.cli("policy", "preview", A, "--json")
    assert result.code == 1
    assert "policy error: mpx/e2" in result.err
    assert '"policy_errors": [\n    "mpx/e2"\n  ]' in result.out


def test_br_185_suspended_reached_and_read_back(bed: Bed) -> None:
    gw = bed.gw
    up = bed.connection("su", A)
    up.tools = [tool("r", annotations=READ)]
    gw.ok("policy", "pin", "mpx/su", "--role", A, "on")
    catalogue(gw, "mpx/su", A)  # baselines
    up.tools[0]["description"] = "changed"
    catalogue(gw, "mpx/su", A)  # a listing of the placement suspends it
    with bed.serve() as client:
        assert unknown(call(client, A, bed.token(A), "su__r"))
    [entry] = audits(gw, "relay.tool_call")
    assert (entry["result"], entry["reason"]) == ("refused", "suspended")


def test_br_185_unloadable_state_in_the_preview(bed: Bed) -> None:
    """A state that cannot be loaded (a corrupted row, not an evaluator exception) makes the
    preview exit non-zero naming every connection of the role, never an uncaught exception."""
    gw = bed.gw
    c1 = bed.connection("c1", A)
    c1.tools = [tool("write", {"type": "object", "properties": {"x": {"type": "string"}}})]
    bed.connection("c2", A)
    catalogue(gw, "mpx/c1", A)
    gw.ok("policy", "arg", "mpx/c1", "--role", A, "write", "x", "y")
    conn = gw.connect()
    try:
        with conn:
            conn.execute("UPDATE argument_rule SET allowed_values = 'not json'")
    finally:
        conn.close()
    result = gw.cli("policy", "preview", A, "--json")
    assert result.code == 1, result
    assert "Traceback" not in result.err
    assert "policy error: mpx/c1" in result.err
    assert "policy error: mpx/c2" in result.err
    assert '"tools": []' in result.out
    with bed.serve() as client:
        assert names(client, A, bed.token(A)) == []
    omitted = sorted(a["path"] for a in audits(gw, "relay.list_omitted"))
    assert omitted == ["mpx/c1", "mpx/c2"]
    assert {a["reason"] for a in audits(gw, "relay.list_omitted")} == {"policy_error"}


def test_br_185_hidden_tool_writes_nothing_at_list(bed: Bed) -> None:
    gw = bed.gw
    up = bed.connection("hi", A)
    up.tools = [tool("d", annotations=DESTRUCTIVE)]
    with bed.serve() as client:
        assert names(client, A, bed.token(A)) == []
    assert audits(gw, "relay.list_omitted") == []
    assert audits(gw, "relay.tool_call") == []


def _no_argument_value(gw: Gateway) -> None:
    conn = gw.connect()
    try:
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
            for row in conn.execute(f"SELECT * FROM {table}"):  # noqa: S608
                assert MARKER not in " ".join(str(v) for v in row), table
    finally:
        conn.close()
