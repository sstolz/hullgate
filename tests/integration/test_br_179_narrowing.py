"""BR-179, BR-109 (F-015 AC-13): when a tool with an argument rule is announced and its schema has
an `enum` for that argument, the announced enum holds only the allowed values, upstream order
kept, nothing else of the definition changing; a schema without enum is relayed unchanged; the
upstream client and its `Mcp-Param-*` headers work on the upstream definition."""

from __future__ import annotations

import copy

import pytest

from tests.policy_support import called, catalogue, gitea, method_schema
from tests.relay_bed import A, Bed, call, echoed, listing
from tests.support import MODERN_VERSION, Gateway

GT = "mpx/gt"


@pytest.fixture
def bed(gw: Gateway) -> Bed:
    b = Bed(gw)
    gitea(b, "gt", A)
    catalogue(gw, GT, A)
    gw.ok("policy", "add", GT, "--role", A, "open", "pull_request_review_write", "issue_write")
    return b


def _announced(bed: Bed, name: str) -> dict[str, object]:
    with bed.serve() as client:
        [found] = [t for t in listing(client, A, bed.token(A)) if t["name"] == f"gt__{name}"]
    return found


def test_br_179_enum_narrowed_in_upstream_order_nothing_else(bed: Bed) -> None:
    gw = bed.gw
    up = bed.upstream("gt")
    upstream_def = copy.deepcopy(
        next(t for t in up.tools if t["name"] == "pull_request_review_write")
    )
    gw.ok("policy", "arg", GT, "--role", A, "pull_request_review_write", "method", "delete",
          "create")  # fmt: skip
    shown = _announced(bed, "pull_request_review_write")
    expected = copy.deepcopy(upstream_def)
    expected["name"] = "gt__pull_request_review_write"
    expected["inputSchema"]["properties"]["method"]["enum"] = ["create", "delete"]
    assert shown == expected
    assert next(t for t in up.tools if t["name"] == "pull_request_review_write") == upstream_def


def test_br_179_schema_without_enum_relayed_unchanged(bed: Bed) -> None:
    gw = bed.gw
    up = bed.upstream("gt")
    upstream_def = copy.deepcopy(next(t for t in up.tools if t["name"] == "issue_write"))
    gw.ok("policy", "arg", GT, "--role", A, "issue_write", "body", "x")
    assert _announced(bed, "issue_write") == {**upstream_def, "name": "gt__issue_write"}


def test_br_179_upstream_definition_drives_the_param_headers(bed: Bed) -> None:
    """Modern generation: the upstream checks `Mcp-Param-*` against its own schema; a call on the
    narrowed tool with an allowed value passes it."""
    gw = bed.gw
    up = bed.upstream("gt")
    up.tools = [
        {**t, "inputSchema": {**method_schema("create", "submit", "delete"),
                              "properties": {"method": {"type": "string",
                                                        "enum": ["create", "submit", "delete"],
                                                        "x-mcp-header": "Method"}}}}
        if t["name"] == "pull_request_review_write" else t
        for t in up.tools
    ]  # fmt: skip
    gw.clock.advance(301)
    gw.ok("policy", "arg", GT, "--role", A, "pull_request_review_write", "method", "submit")
    with bed.serve() as client:
        done = call(client, A, bed.token(A), "gt__pull_request_review_write",
                    {"method": "submit"}, version=MODERN_VERSION,
                    headers={"Mcp-Param-Method": "submit"})  # fmt: skip
    assert echoed(done) == {"method": "submit"}
    assert called(up) == ["pull_request_review_write"]
