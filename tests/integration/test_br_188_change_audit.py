"""BR-188, BR-066 (F-015 AC-19): every change of a mode, list, argument rule, pin, classification
or baseline writes an audit entry with actor, target, before and after, read back here;
`tool.discovered` is the catalogue's."""

from __future__ import annotations

from tests.fake_upstream import READ, tool
from tests.policy_support import audits, catalogue, gitea
from tests.relay_bed import A, Bed
from tests.support import Gateway

GT = "mpx/gt"


def test_br_188_each_change_is_audited(gw: Gateway) -> None:
    bed = Bed(gw)
    gitea(bed, "gt", A).tools.append(tool("extra", annotations=READ))
    catalogue(gw, GT, A)
    steps = [
        ("policy.mode", ("policy", "mode", GT, "--role", A, "read"),
         {"mode": None}, {"mode": "read"}),
        ("policy.list_add", ("policy", "add", GT, "--role", A, "deny", "issue_write"),
         {"listed": False}, {"listed": True}),
        ("policy.list_remove", ("policy", "remove", GT, "--role", A, "deny", "issue_write"),
         {"listed": True}, {"listed": False}),
        ("policy.argument_set",
         ("policy", "arg", GT, "--role", A, "pull_request_write", "method", "create"),
         None, {"argument": "method", "values": ["create"]}),
        ("policy.argument_remove", ("policy", "arg-remove", GT, "--role", A, "pull_request_write"),
         {"argument": "method", "values": ["create"]}, None),
        ("tool.classify", ("tool", "classify", "sgt", "issue_write", "write"),
         {"manual_class": None, "class": "destructive"},
         {"manual_class": "write", "class": "write"}),
        ("tool.accept", ("tool", "accept", "sgt", "extra"),
         {"queued": True}, {"queued": False, "class": "read", "source": "annotations"}),
    ]  # fmt: skip
    for action, args, before, after in steps:
        gw.ok(*args)
        [entry] = audits(gw, action)
        assert entry["actor"] == "operator-cli", action
        assert (entry["before"], entry["after"]) == (before, after), action
        if action.startswith("policy."):
            assert entry["path"] == A
            assert entry["node_id"] == gw.node_id(A)
            assert entry["detail"]["connection"] == GT
        else:
            assert entry["path"] == "sgt"
            assert entry["detail"]["service"] == "sgt"
    discovered = audits(gw, "tool.discovered")
    assert len(discovered) == 7
    assert {d["detail"]["service"] for d in discovered} == {"sgt"}


def test_br_188_pin_and_baseline_changes_are_audited(gw: Gateway) -> None:
    bed = Bed(gw)
    gitea(bed, "gt", A)
    catalogue(gw, GT, A)
    steps = [
        (("policy", "pin", GT, "--role", A, "on"), {"pin": "inherit"}, {"pin": "on"}),
        (("policy", "pin", GT, "--role", A, "inherit"), {"pin": "on"}, {"pin": "inherit"}),
        (("policy", "pin-tool", GT, "--role", A, "list_issues"),
         {"tool_pin": False}, {"tool_pin": True}),
        (("policy", "unpin-tool", GT, "--role", A, "list_issues"),
         {"tool_pin": True}, {"tool_pin": False}),
    ]  # fmt: skip
    for args, before, after in steps:
        gw.ok(*args)
        entry = audits(gw, "policy.pin")[-1]
        assert entry["actor"] == "operator-cli", args
        assert (entry["before"], entry["after"]) == (before, after), args
        assert entry["path"] == A
        assert entry["node_id"] == gw.node_id(A)
        assert entry["detail"]["connection"] == GT
    assert audits(gw, "policy.pin")[2]["detail"]["tool"] == "list_issues"
    count = len(audits(gw, "policy.pin"))
    gw.ok("policy", "pin", GT, "--role", A, "inherit")  # unchanged: nothing written
    assert len(audits(gw, "policy.pin")) == count
    gw.ok("policy", "pin", GT, "--role", A, "on")
    catalogue(gw, GT, A)  # baselines
    bed.upstream("gt").tools[0]["description"] = "changed"
    catalogue(gw, GT, A)  # suspension
    [suspended] = audits(gw, "tool.suspended")
    assert suspended["actor"] == "operator-cli"
    assert suspended["path"] == GT
    assert suspended["node_id"] == gw.node_id(A)
    assert suspended["detail"]["tool"] == "list_issues"
    baseline = suspended["before"]["sha256"]
    assert suspended["after"] == {"sha256": suspended["after"]["sha256"], "suspended": True}
    gw.ok("tool", "validate", "mpx/cairn/staging", GT)
    [validated] = audits(gw, "tool.validate")
    assert validated["actor"] == "operator-cli"
    assert validated["path"] == A
    assert validated["node_id"] == gw.node_id(A)
    assert validated["detail"] == {"connection": GT, "tool": "list_issues"}
    assert validated["before"] == {"sha256": baseline, "suspended": True}
    assert validated["after"] == {"sha256": suspended["after"]["sha256"], "suspended": False}
