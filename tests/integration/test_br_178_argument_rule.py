"""BR-178 (F-015 AC-3, AC-12): a tool carrying an argument rule is called only when the named
top-level argument is present and its value is a string exactly equal to an allowed value. Each
other form (other value, absent argument, array, number, null, case variant) answers "unknown
tool" with 0 `tools/call` and is audited `refused`, reason `argument_value`, naming no value;
allowed calls reach the upstream value for value."""

from __future__ import annotations

from typing import Any

import pytest

from tests.policy_support import audits, called, catalogue, gitea
from tests.relay_bed import A, Bed, call, echoed, listing, unknown
from tests.support import Gateway

GT = "mpx/gt"
REFUSED_FORMS: list[dict[str, Any]] = [
    {"method": "merge"},
    {},
    {"index": 3},
    {"method": ["create"]},
    {"method": 1},
    {"method": None},
    {"method": "Create"},
    {"method": "create "},
]


@pytest.fixture
def bed(gw: Gateway) -> Bed:
    b = Bed(gw)
    gitea(b, "gt", A)
    catalogue(gw, GT, A)
    gw.ok("policy", "add", GT, "--role", A, "open", "pull_request_write")  # destructive (§12)
    gw.ok("policy", "arg", GT, "--role", A, "pull_request_write", "method", "create")
    return b


@pytest.mark.parametrize("arguments", REFUSED_FORMS)
def test_br_178_refused_forms(bed: Bed, arguments: dict[str, Any]) -> None:
    up = bed.upstream("gt")
    with bed.serve() as client:
        response = call(client, A, bed.token(A), "gt__pull_request_write", arguments)
    assert unknown(response)
    assert called(up) == []
    [entry] = audits(bed.gw, "relay.tool_call")
    assert (entry["result"], entry["reason"]) == ("refused", "argument_value")
    assert "merge" not in str(entry)
    assert "Create" not in str(entry)


def test_br_178_allowed_call_reaches_upstream_unchanged(bed: Bed) -> None:
    """AC-3: creating a PR passes, merging is refused, the announced enum holds `create`."""
    up = bed.upstream("gt")
    sent = {"method": "create", "index": 7, "body": "é x"}
    with bed.serve() as client:
        done = call(client, A, bed.token(A), "gt__pull_request_write", sent)
        merge = call(client, A, bed.token(A), "gt__pull_request_write", {"method": "merge"})
        announced = listing(client, A, bed.token(A))
    assert echoed(done) == sent
    assert unknown(merge)
    assert called(up) == ["pull_request_write"]
    [pr] = [t for t in announced if t["name"] == "gt__pull_request_write"]
    assert pr["inputSchema"]["properties"]["method"]["enum"] == ["create"]


def test_br_178_new_rule_replaces_the_old(bed: Bed) -> None:
    gw = bed.gw
    gw.ok("policy", "arg", GT, "--role", A, "pull_request_write", "method", "update", "merge")
    up = bed.upstream("gt")
    with bed.serve() as client:
        assert unknown(call(client, A, bed.token(A), "gt__pull_request_write",
                            {"method": "create"}))  # fmt: skip
        merged = call(client, A, bed.token(A), "gt__pull_request_write", {"method": "merge"})
    assert echoed(merged) == {"method": "merge"}
    assert called(up) == ["pull_request_write"]
    gw.ok("policy", "arg-remove", GT, "--role", A, "pull_request_write")
    with bed.serve() as client:
        created = call(client, A, bed.token(A), "gt__pull_request_write", {"method": "create"})
    assert echoed(created) == {"method": "create"}


def test_br_178_renamed_parameter_fails_closed(bed: Bed) -> None:
    up = bed.upstream("gt")
    for t in up.tools:
        if t["name"] == "pull_request_write":
            props = t["inputSchema"]["properties"]
            props["action"] = props.pop("method")
    bed.gw.clock.advance(301)
    with bed.serve() as client:
        response = call(client, A, bed.token(A), "gt__pull_request_write", {"action": "merge"})
    assert unknown(response)
    assert called(up) == []
