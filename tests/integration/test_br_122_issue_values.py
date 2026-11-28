"""BR-122, BR-111, BR-131 (F-010 AC-21): the relay's audit values are English literals.

- `detail.issue` of a call: `ok`, `tool_error`, `refused`, `cancelled` and the eight codes of
  BR-116 (12 values); each is provoked one by one, except `cancelled` (stream closed by the
  client, provoked by `test_br_120_*` and pinned here by its constant). BR-116 v3's
  `rate_limited` is not one: a limited call writes no `relay.tool_call` (BR-122 v3, F-020).
- reasons for omitting a connection from `tools/list` (`relay.list_omitted`, and `invalid_name`
  of `connection.tool_name_invalid`): the eight values, each provoked here with the outcome
  (the file passes alone, and under `-k br_122`).
The sets are literals, never derived from the code's constants.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from hullgate import policy, relay, upstream_mcp
from tests.fake_upstream import FakeClock, Fault, tool
from tests.integration.test_br_116_error_hygiene import CODES, _provoke
from tests.relay_bed import A, Bed, call, gateway_error, names
from tests.support import Gateway

# BR-122 v3 (F-020): a call refused by a limit is audited through BR-312 alone
CALL_CODES = CODES - {"rate_limited"}
ISSUES = {
    "ok", "tool_error", "refused", "cancelled",
    "upstream_unavailable", "upstream_busy", "deadline_exceeded", "uncertain_outcome",
    "response_too_large", "request_too_large", "connection_reauth_required", "internal_error",
}  # fmt: skip
OMISSIONS = {
    "outage", "deadline", "version", "session", "policy_error", "pagination", "too_many_names",
    "invalid_name",
}  # fmt: skip


def _issues(gw: Gateway) -> list[str]:
    return [
        str(json.loads(str(a["detail"]))["issue"])
        for a in gw.audit()
        if a["action"] == relay.A_CALL
    ]


def test_br_122_pinned_sets_are_the_literals_of_the_spec() -> None:
    assert len(ISSUES) == 12
    assert len(OMISSIONS) == 8
    assert ISSUES - {"ok", "tool_error", "refused", "cancelled"} == CALL_CODES
    assert upstream_mcp.CANCELLED == "cancelled"
    assert relay.TOOL_ERROR == "tool_error"
    assert relay.REFUSED == "refused"
    assert {
        relay.M_DOWN, relay.M_DEADLINE, relay.M_VERSION, relay.M_SESSION, relay.M_POLICY_ERROR,
        relay.M_PAGINATION, relay.OVERFLOW, relay.INVALID,
    } == OMISSIONS  # fmt: skip


@pytest.mark.parametrize("code", sorted(CALL_CODES))
def test_br_122_failure_issue_is_its_br_116_code(gw: Gateway, code: str) -> None:
    bed = Bed(gw)
    up = bed.connection("mk", A)
    clock = FakeClock()
    relay_ = gw.relay(upclock=clock)
    with bed.serve(relay_) as client:
        names(client, A, bed.token(A))
        args = _provoke(up, relay_, clock, code)
        response = call(client, A, bed.token(A), "mk__read", args)
    assert gateway_error(response) == code
    assert _issues(gw) == [code]


def test_br_122_ok_tool_error_and_refused_issues(gw: Gateway) -> None:
    bed = Bed(gw)
    up = bed.connection("au", A)
    with bed.serve() as client:
        token = bed.token(A)
        names(client, A, token)
        call(client, A, token, "au__read")
        result: dict[str, Any] = {"content": [{"type": "text", "text": "x"}], "isError": True}
        up.results["read"] = result
        call(client, A, token, "au__read")
        call(client, A, token, "unknown__read")
    assert _issues(gw) == ["ok", "tool_error", "refused"]


def _rows(gw: Gateway, *actions: str) -> list[tuple[str, str, str]]:
    return [
        (str(a["action"]), str(a["reason"]), str(json.loads(str(a["detail"]))["issue"]))
        for a in gw.audit()
        if a["action"] in actions
    ]


def _outage(bed: Bed) -> None:
    bed.connection("ok", A)
    bed.connection("bd", A).faults.append(Fault("status", status=500, on="tools/list"))


def _version(bed: Bed) -> None:
    bed.connection("ok", A)
    bed.connection("bd", A, generation="none")


def _session(bed: Bed) -> None:
    bed.connection("ok", A)
    bed.connection("bd", A, generation="session").faults.append(
        Fault("bad_session", on="initialize")
    )


def _pagination(bed: Bed) -> None:
    bed.connection("ok", A)
    up = bed.connection("bd", A)
    up.tools = [tool(f"t{i}") for i in range(11)]
    up.page_size = 1


def _too_many_names(bed: Bed) -> None:
    up = bed.connection("bd", A)
    up.tools = [tool(f"t{i}") for i in range(498)]
    up.page_size = 100
    with bed.serve() as client:
        names(client, A, bed.token(A))  # 498 names kept
    up.tools = [tool(f"u{i}") for i in range(500)]  # 998 distinct names: still under 1 000
    with bed.serve() as client:
        names(client, A, bed.token(A))
    up.tools = [tool(f"v{i}") for i in range(500)]  # 1 498: over the ceiling


def _policy_error(bed: Bed) -> None:
    """F-015 BR-185: the evaluator raises on one connection; the test body patches it."""
    bed.connection("ok", A)
    bed.connection("bd", A)


def _deadline(bed: Bed) -> None:
    bed.connection("ok", A)
    bed.connection("bd", A).faults.append(Fault("delay", on="tools/list", seconds=3.0))


# reason -> (provocation, issue written beside it)
OMISSION_CASES = {
    "outage": (_outage, "upstream_unavailable"),
    "version": (_version, "upstream_unavailable"),
    "session": (_session, "upstream_unavailable"),
    "pagination": (_pagination, "upstream_unavailable"),
    "too_many_names": (_too_many_names, "upstream_unavailable"),
    "policy_error": (_policy_error, "refused"),
    "deadline": (_deadline, "deadline_exceeded"),
}


def test_br_122_omission_cases_plus_invalid_name_are_the_eight() -> None:
    assert set(OMISSION_CASES) | {"invalid_name"} == OMISSIONS


@pytest.mark.parametrize("reason", sorted(OMISSION_CASES))
def test_br_122_omission_reason_and_its_issue(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    """Each omission reason is provoked here, so the file passes alone and under `-k br_122`."""
    if reason == "deadline":
        monkeypatch.setattr(relay, "LIST_DEADLINE_S", 1.0)
    bed = Bed(gw)
    provoke, issue = OMISSION_CASES[reason]
    provoke(bed)
    if reason == "policy_error":
        bad = gw.connection_id("mpx/bd")
        original = policy.RoleState.decide

        def broken(self: policy.RoleState, *args: Any) -> policy.Decision:
            if args[1] == bad:
                raise RuntimeError("evaluator failure")
            return original(self, *args)

        monkeypatch.setattr(policy.RoleState, "decide", broken)
    with bed.serve() as client:
        names(client, A, bed.token(A))
    got = _rows(gw, relay.A_OMITTED)
    assert [(r, i) for _, r, i in got] == [(reason, issue)]


def test_br_122_invalid_name_reason(gw: Gateway) -> None:
    bed = Bed(gw)
    bed.connection("nm", A).tools = [tool("a.b/c"), tool("___"), tool("ok")]
    with bed.serve() as client:
        names(client, A, bed.token(A))
    reasons = {a["reason"] for a in gw.audit() if a["action"] == relay.A_TOOL_NAME_INVALID}
    assert reasons == {"invalid_name"}


def test_br_122_limited_call_has_no_call_entry(gw: Gateway) -> None:
    """BR-122 v3 (F-020): `rate_limited` is a BR-116 code but never an issue."""
    bed = Bed(gw)
    up = bed.connection("mk", A)
    clock = FakeClock()
    relay_ = gw.relay(upclock=clock)
    with bed.serve(relay_) as client:
        names(client, A, bed.token(A))
        _provoke(up, relay_, clock, "rate_limited")
        response = call(client, A, bed.token(A), "mk__read")
    assert gateway_error(response) == "rate_limited"
    assert _issues(gw) == []
