"""BR-175, BR-113 (F-015 AC-2 fake upstream, AC-8): a sensitive placement exposes nothing but the
tools of its `allowlist` whose class is `read` and the tools of its `open` list, whatever the
mode. A blank sensitive placement announces nothing; a listed read shows; a listed write stays
hidden with `write_closed`; `open` shows it. A sensitive placement reaches stage 2 of BR-113: a
listing and a `get_access` happen, no `tools/call` does without an opening."""

from __future__ import annotations

import json

import pytest

from tests.policy_support import audits, called, catalogue, gitea
from tests.relay_bed import Bed, S, call, echoed, listing, names, unknown
from tests.support import Gateway

GT = "mpx/gt"


@pytest.fixture
def bed(gw: Gateway) -> Bed:
    return Bed(gw)


def test_br_175_blank_sensitive_placement_lists_nothing(bed: Bed) -> None:
    up = gitea(bed, "gt", S)
    bed.gw.ok("settings", "set", "/", "mode", "destructive")  # whatever the mode
    with bed.serve() as client:
        assert names(client, S, bed.token(S)) == []
        assert unknown(call(client, S, bed.token(S), "gt__list_issues"))
    assert len(up.calls("tools/list")) >= 1  # stage 2 reached: a listing happened
    assert up.calls("tools/call") == []


def test_br_175_listed_read_shows_listed_write_closed_open_shows(bed: Bed) -> None:
    gw = bed.gw
    up = gitea(bed, "gt", S)
    catalogue(gw, GT, S)
    gw.ok("policy", "add", GT, "--role", S, "allowlist", "list_issues", "issue_write")
    with bed.serve() as client:
        assert names(client, S, bed.token(S)) == ["gt__list_issues"]
        refused = call(client, S, bed.token(S), "gt__issue_write", {"method": "create"})
    assert unknown(refused)
    assert up.calls("tools/call") == []
    [entry] = [a for a in audits(gw, "relay.tool_call") if a["reason"] is not None]
    assert entry["reason"] == "write_closed"
    gw.ok("policy", "add", GT, "--role", S, "open", "issue_write")
    with bed.serve() as client:
        assert sorted(names(client, S, bed.token(S))) == ["gt__issue_write", "gt__list_issues"]
        done = call(client, S, bed.token(S), "gt__issue_write", {"method": "create"})
    assert echoed(done) == {"method": "create"}
    assert len(up.calls("tools/call")) == 1


def test_br_175_mode_does_not_lift_the_allowlist(bed: Bed) -> None:
    gw = bed.gw
    gitea(bed, "gt", S)
    catalogue(gw, GT, S)
    gw.ok("policy", "mode", GT, "--role", S, "destructive")
    gw.ok("policy", "add", GT, "--role", S, "allowlist", "delete_file")
    with bed.serve() as client:
        assert names(client, S, bed.token(S)) == []


def test_br_175_reviewer_of_the_specification(bed: Bed) -> None:
    """AC-2 against a fake upstream with the Gitea tool names: the reviewer configured as in
    §3.3 does not see the merge tool, a direct call answers "unknown tool" with 0 `tools/call`
    for it, and `pull_request_review_write` works."""
    gw = bed.gw
    up = gitea(bed, "gt", S)
    gw.ok("connection", "tools", GT, "--role", S)
    gw.ok("policy", "add", GT, "--role", S, "allowlist", "--class", "read")
    gw.ok("policy", "add", GT, "--role", S, "open", "pull_request_review_write")
    gw.ok("policy", "arg", GT, "--role", S, "pull_request_review_write", "method", "create",
          "submit")  # fmt: skip
    gw.ok("policy", "add", GT, "--role", S, "deny", "pull_request_write", "issue_write")
    seen = json.loads(gw.ok("policy", "preview", S, "--json"))
    by_name = {t["upstream_name"]: t for t in seen["tools"]}
    assert by_name["pull_request_write"]["exposed"] is False
    with bed.serve() as client:
        announced = listing(client, S, bed.token(S))
        merge = call(client, S, bed.token(S), "gt__pull_request_write", {"method": "merge"})
        review = call(client, S, bed.token(S), "gt__pull_request_review_write",
                      {"method": "create", "index": 3})  # fmt: skip
    assert sorted(t["name"] for t in announced) == [
        "gt__get_pull_request", "gt__list_issues", "gt__pull_request_review_write",
    ]  # fmt: skip
    assert "gt__pull_request_write" not in [t["name"] for t in announced]
    assert unknown(merge)
    assert called(up) == ["pull_request_review_write"]
    assert echoed(review) == {"method": "create", "index": 3}
    review_def = next(t for t in announced if t["name"] == "gt__pull_request_review_write")
    assert review_def["inputSchema"]["properties"]["method"]["enum"] == ["create", "submit"]
