"""BR-178 entry (F-015 AC-12): `policy arg` lists the upstream at entry and refuses an argument
absent from `inputSchema.properties`, a value outside an `enum` the schema declares, an unknown
tool and an unavailable upstream, exit 2, no rule recorded."""

from __future__ import annotations

import pytest

from tests.fake_upstream import Fault
from tests.policy_support import audits, catalogue, gitea, policy_rows
from tests.relay_bed import A, Bed
from tests.support import Gateway

GT = "mpx/gt"


@pytest.fixture
def bed(gw: Gateway) -> Bed:
    b = Bed(gw)
    gitea(b, "gt", A)
    catalogue(gw, GT, A)
    return b


@pytest.mark.parametrize(
    "args",
    [
        ["pull_request_write", "action", "create"],  # argument absent from the schema
        ["pull_request_write", "method", "create", "squash"],  # value outside the enum
        ["list_issues", "method", "x"],  # no `method` in that schema
        ["nothing", "method", "create"],  # unknown tool
    ],
)
def test_br_178_entry_refusals(bed: Bed, args: list[str]) -> None:
    gw = bed.gw
    before = policy_rows(gw)[:3]
    result = gw.cli("policy", "arg", GT, "--role", A, *args)
    assert result.code == 2, result
    assert policy_rows(gw)[:3] == before
    assert audits(gw, "policy.argument_set") == []


def test_br_178_unavailable_upstream_refused(bed: Bed) -> None:
    gw = bed.gw
    bed.upstream("gt").faults.append(Fault("status", on="tools/list", status=503))
    result = gw.cli("policy", "arg", GT, "--role", A, "pull_request_write", "method", "create")
    assert result.code == 2, result
    assert "no rule recorded" in result.err
    assert audits(gw, "policy.argument_set") == []


def test_br_178_free_string_argument_accepted(bed: Bed) -> None:
    """Without an `enum`, any value list is accepted at entry (only BR-178 applies)."""
    gw = bed.gw
    gw.ok("policy", "arg", GT, "--role", A, "issue_write", "body", "ok")
    [entry] = audits(gw, "policy.argument_set")
    assert entry["after"] == {"argument": "body", "values": ["ok"]}
    assert entry["before"] is None
