"""BR-123 (F-008 lot B, AC-28): the relay consults the policy at announcement and at call, with
the role, the connection, the name and the upstream definition (and the arguments at call); masked
or refused means "unknown tool" with no `tools/call`; a policy in error closes the tools
concerned; the default policy is the F-015 engine (BR-123 v2), there is no "no policy" any more."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from hullgate.policy import ALLOW, ENGINE, HIDE, Decision, Refuse
from tests.fake_upstream import NONE, tool
from tests.relay_bed import A, Bed, call, names, unknown
from tests.support import Gateway


class TestPolicy:
    __test__ = False

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.seen: list[tuple[str, str, str, str, Any]] = []

    def decide(
        self,
        role_id: str,
        connection_id: str,
        upstream_name: str,
        definition: Mapping[str, Any],
        arguments: Mapping[str, Any] | None,
    ) -> Decision:
        self.seen.append(
            (role_id, connection_id, upstream_name, str(definition.get("description")), arguments)
        )
        if self.fail:
            raise RuntimeError("broken policy")
        if upstream_name == "write":
            return HIDE
        if arguments is not None and arguments.get("method") == "delete":
            return Refuse("suppression")
        return ALLOW


@pytest.fixture
def bed(gw: Gateway) -> Bed:
    return Bed(gw)


@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
def test_br_123_hidden_and_refused_are_unknown_without_call(
    bed: Bed, monkeypatch: pytest.MonkeyPatch, warm: bool
) -> None:
    gw = bed.gw
    up = bed.connection("po", A)
    with bed.serve() as client:
        names(client, A, bed.token(A))  # table filled with no policy
    policy = TestPolicy()
    relay = gw.relay(policy=policy)
    if warm:
        with bed.serve(relay) as client:
            assert names(client, A, bed.token(A)) == ["po__read"]
    lists = len(up.calls("tools/list"))
    accesses = bed.spy_access(monkeypatch)
    with bed.serve(relay) as client:
        hidden = call(client, A, bed.token(A), "po__write")
        refused = call(client, A, bed.token(A), "po__read", {"method": "delete"})
        allowed = call(client, A, bed.token(A), "po__read", {"method": "get"})
    assert unknown(hidden)
    assert unknown(refused)
    assert hidden.json()["error"] == {**hidden.json()["error"], "code": -32602}
    assert allowed.json()["result"]["isError"] is False
    assert len(up.calls("tools/call")) == 1
    assert len(accesses) == 3  # one `get_access` per call, never more
    assert len(up.calls("tools/list")) - lists == (0 if warm else 1)
    rid, cid = gw.node_id(A), gw.connection_id("mpx/po")
    assert (rid, cid, "write", "tool write", {}) in policy.seen  # at call
    assert (rid, cid, "read", "tool read", {"method": "delete"}) in policy.seen
    if warm:
        assert (rid, cid, "write", "tool write", None) in policy.seen  # at announcement


def test_br_123_failing_policy_closes_the_tools(bed: Bed) -> None:
    up = bed.connection("po", A)
    with bed.serve(bed.gw.relay(policy=TestPolicy(fail=True))) as client:
        assert names(client, A, bed.token(A)) == []
        bed.gw.ok("connection", "tools", "mpx/po", "--role", A)  # names recorded by the CLI
        assert unknown(call(client, A, bed.token(A), "po__read"))
    assert up.calls("tools/call") == []


def test_br_123_default_policy_is_the_engine(bed: Bed) -> None:
    """F-015: the relay's default policy is the real engine; an unannotated tool is
    `destructive` (BR-171) and hidden under the default mode `write`."""
    up = bed.connection("po", A)
    up.tools.append(tool("bare", annotations=NONE))
    relay = bed.gw.relay()
    assert relay.policy is ENGINE
    with bed.serve(relay) as client:
        assert names(client, A, bed.token(A)) == ["po__read", "po__write"]
