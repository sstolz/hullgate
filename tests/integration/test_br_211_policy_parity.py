"""BR-211, BR-215 (F-016 slice 3) — the policy and catalogue writes of the console are F-015's
operations: each route leaves the state the CLI verb leaves, refuses with the CLI's text, writes
its audit as `admin:<id>`, and says that clients see the change at their next `tools/list`.

Two identical beds, one driven by the CLI and one by the API, are compared on every policy row
after the same sequence of acts; the refusals are played on both and compared word for word.
"""

from __future__ import annotations

import json
from typing import Any

from tests.admin_support import Console
from tests.fake_upstream import READ, Fault, tool
from tests.policy_support import catalogue, gitea, policy_rows, rows
from tests.relay_bed import A, Bed, names
from tests.support import Gateway

C = "mpx/gt"
# (CLI argv, API method, API route, API body) — the same act on both sides
ACTS: list[tuple[tuple[str, ...], str, str, dict[str, Any]]] = [
    (("policy", "mode", C, "--role", A, "destructive"),
     "PUT", "placements/mode", {"connection": C, "role": A, "mode": "destructive"}),
    (("policy", "add", C, "--role", A, "deny", "delete_file"),
     "POST", "placements/lists", {"connection": C, "role": A, "list": "deny",
                                  "tools": ["delete_file"]}),
    (("policy", "add", C, "--role", A, "allowlist", "--class", "read"),
     "POST", "placements/lists", {"connection": C, "role": A, "list": "allowlist",
                                  "class": "read"}),
    (("policy", "remove", C, "--role", A, "allowlist", "get_pull_request"),
     "POST", "placements/lists/remove", {"connection": C, "role": A, "list": "allowlist",
                                         "tools": ["get_pull_request"]}),
    (("policy", "arg", C, "--role", A, "issue_write", "method", "create"),
     "PUT", "placements/arguments", {"connection": C, "role": A, "tool": "issue_write",
                                     "argument": "method", "values": ["create"]}),
    (("policy", "pin", C, "--role", A, "on"),
     "PUT", "placements/pin", {"connection": C, "role": A, "pin": "on"}),
    (("policy", "pin-tool", C, "--role", A, "list_issues"),
     "POST", "placements/tool-pins", {"connection": C, "role": A, "tool": "list_issues"}),
    (("policy", "unpin-tool", C, "--role", A, "list_issues"),
     "POST", "placements/tool-pins/remove", {"connection": C, "role": A,
                                             "tool": "list_issues"}),
    (("policy", "arg-remove", C, "--role", A, "issue_write"),
     "POST", "placements/arguments/remove", {"connection": C, "role": A,
                                             "tool": "issue_write"}),
    (("tool", "classify", "sgt", "pull_request_write", "write"),
     "POST", "tools/classify", {"service": "sgt", "tool": "pull_request_write",
                                "class": "write"}),
    (("tool", "accept", "sgt", "issue_write"),
     "POST", "tools/accept", {"service": "sgt", "tool": "issue_write"}),
]  # fmt: skip
# refusals the CLI and the API must word alike (argparse `choices` are the domain's checks too)
REFUSALS: list[tuple[tuple[str, ...], str, str, dict[str, Any]]] = [
    (("policy", "add", C, "--role", A, "allowlist", "list_issues", "--class", "read"),
     "POST", "placements/lists", {"connection": C, "role": A, "list": "allowlist",
                                  "tools": ["list_issues"], "class": "read"}),
    (("policy", "add", C, "--role", A, "deny", "nope"),
     "POST", "placements/lists", {"connection": C, "role": A, "list": "deny",
                                  "tools": ["nope"]}),
    (("policy", "add", C, "--role", A, "deny"),
     "POST", "placements/lists", {"connection": C, "role": A, "list": "deny", "tools": []}),
    (("policy", "remove", C, "--role", A, "open", "list_issues"),
     "POST", "placements/lists/remove", {"connection": C, "role": A, "list": "open",
                                         "tools": ["list_issues"]}),
    (("policy", "arg", C, "--role", A, "issue_write", "method", "explode"),
     "PUT", "placements/arguments", {"connection": C, "role": A, "tool": "issue_write",
                                     "argument": "method", "values": ["explode"]}),
    (("policy", "arg-remove", C, "--role", A, "issue_write"),
     "POST", "placements/arguments/remove", {"connection": C, "role": A,
                                             "tool": "issue_write"}),
    (("policy", "unpin-tool", C, "--role", A, "list_issues"),
     "POST", "placements/tool-pins/remove", {"connection": C, "role": A,
                                             "tool": "list_issues"}),
    (("policy", "show", C, "--role", "mpx/cairn/staging/rev"),
     "GET", "placements/policy?connection=mpx/gt&role=mpx/cairn/staging/rev", {}),
    (("tool", "accept", "sgt", "list_issues"),
     "POST", "tools/accept", {"service": "sgt", "tool": "list_issues"}),
    (("tool", "classify", "nope", "x", "read"),
     "POST", "tools/classify", {"service": "nope", "tool": "x", "class": "read"}),
    (("tool", "validate", "mpx/cairn/staging/rev", "mpx/gt"),
     "POST", "tools/validate", {"path": "mpx/cairn/staging/rev", "connection": C}),
]  # fmt: skip
# API values the CLI's argparse `choices` refuse before the domain: the domain refuses them too
DOMAIN_CHOICES: list[tuple[str, str, dict[str, Any], str]] = [
    ("PUT", "placements/mode", {"connection": C, "role": A, "mode": "all"}, "invalid mode"),
    ("POST", "placements/lists", {"connection": C, "role": A, "list": "x", "tools": ["a"]},
     "unknown list"),
    ("POST", "placements/lists", {"connection": C, "role": A, "list": "deny", "class": "read"},
     "--class: only `allowlist --class read`"),
    ("PUT", "placements/pin", {"connection": C, "role": A, "pin": "yes"}, "invalid pin"),
    ("POST", "tools/classify", {"service": "sgt", "tool": "list_issues", "class": "x"},
     "invalid class"),
]  # fmt: skip


def _bed(gw: Gateway) -> Bed:
    bed = Bed(gw)
    gitea(bed, "gt", A)
    catalogue(gw, C, A)
    return bed


def _policy(gw: Gateway) -> tuple[Any, ...]:
    # the catalogue without `first_seen`: each bed's clock starts at the wall second it was built,
    # so two beds compared may differ there by one second (review 581 B4)
    placement_tool, argument_rule, placement, _catalog, _audit = policy_rows(gw)
    catalog = rows(gw, "SELECT service_id, upstream_name, manual_class, annotation_class, queued,"
                       " removed FROM tool_catalog ORDER BY 1, 2")  # fmt: skip
    pins = rows(gw, "SELECT upstream_name, tool_pin FROM pinned_definition ORDER BY 1")
    return (placement_tool, argument_rule, placement, catalog, pins)


def _call(console: Console, client: Any, method: str, route: str, body: dict[str, Any]) -> Any:
    if method == "GET":
        return client.get(f"/admin/api/v1/{route}", headers=console.headers())
    return console.call(client, method, route, body)


def _ids(gw: Gateway) -> tuple[Any, ...]:
    # the two beds hold the same names under other identifiers: compare by name, not by id
    cid, rid = gw.connection_id(C), gw.node_id(A)
    return tuple(
        sorted(str(r).replace(cid, "<c>").replace(rid, "<r>") for r in table)
        for table in _policy(gw)
    )


def test_br_211_policy_writes_leave_the_clis_state(gw: Gateway, tmp_path_factory: Any) -> None:
    by_cli = Gateway(tmp_path_factory.mktemp("cli"))
    try:
        _bed(by_cli)
        cli_steps = [_ids(by_cli)]
        for argv, *_ in ACTS:
            by_cli.ok(*argv)
            cli_steps.append(_ids(by_cli))
        _bed(gw)
        api_steps = [_ids(gw)]
        console = Console(gw)
        with console.client() as client:
            console.admin(client)
            start = len(gw.audit())
            for _argv, method, route, body in ACTS:
                done = _call(console, client, method, route, body)
                assert done.status_code == 200, (route, done.text)
                assert done.json()["tools_notice"] is True, route
                api_steps.append(_ids(gw))
        # compared after each act, not only at the end: an act followed by its inverse (pin-tool
        # then unpin-tool) would pass as two no-ops otherwise (review 579 N3)
        for i, (argv, *_) in enumerate(ACTS, start=1):
            assert api_steps[i] == cli_steps[i], argv
            assert cli_steps[i] != cli_steps[i - 1], argv  # every act of the sequence writes
        written = [e for e in gw.audit()[start:] if e["action"].startswith(("policy.", "tool."))]
        assert {e["action"] for e in written} >= {"policy.mode", "policy.list_add",
                                                  "policy.list_remove", "policy.argument_set",
                                                  "policy.argument_remove", "policy.pin",
                                                  "tool.classify", "tool.accept"}  # fmt: skip
        assert {e["actor"] for e in written} == {gw.audit()[-1]["actor"]}
        assert gw.audit()[-1]["actor"].startswith("admin:")
    finally:
        by_cli.close()


def test_br_211_policy_refusals_are_the_clis(gw: Gateway) -> None:
    _bed(gw)
    gw.ok("tool", "accept", "sgt", "issue_write")  # leaves the queue: a second accept refuses
    gw.ok("tool", "accept", "sgt", "list_issues")
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        for argv, method, route, body in REFUSALS:
            cli = gw.cli(*argv)
            assert cli.code == 2, (argv, cli)
            before = _policy(gw)
            api = _call(console, client, method, route, body)
            assert api.status_code == 409, (route, api.text)
            assert api.json()["code"] == "refused"
            assert cli.err.strip() == f"refused: {api.json()['message']}", argv
            assert _policy(gw) == before, route
        for method, route, body, text in DOMAIN_CHOICES:
            api = _call(console, client, method, route, body)
            assert api.status_code == 409, (route, api.text)
            assert text in api.json()["message"]


def test_br_211_an_argument_rule_needs_the_upstream_at_entry(gw: Gateway) -> None:
    bed = _bed(gw)
    bed.upstream("gt").faults.append(Fault("status", on="tools/list", status=503))
    bed.upstream("gt").faults.append(Fault("status", on="tools/list", status=503))
    argv = ("policy", "arg", C, "--role", A, "issue_write", "method", "create")
    cli = gw.cli(*argv)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        before = _policy(gw)
        api = console.call(client, "PUT", "placements/arguments", {
            "connection": C, "role": A, "tool": "issue_write", "argument": "method",
            "values": ["create"],
        })  # fmt: skip
    assert api.status_code == 409, api.text
    assert cli.err.strip() == f"refused: {api.json()['message']}"
    assert "no rule recorded" in api.json()["message"]
    assert _policy(gw) == before


def test_br_211_policy_reads_are_the_clis(gw: Gateway) -> None:
    _bed(gw)
    gw.ok("policy", "add", C, "--role", A, "deny", "delete_file")
    shown = json.loads(gw.ok("policy", "show", C, "--role", A, "--json"))
    queued = json.loads(gw.ok("tool", "queue", "--json"))
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        one = client.get("/admin/api/v1/placements/policy", params={"connection": C, "role": A},
                         headers=console.headers()).json()  # fmt: skip
        under = client.get("/admin/api/v1/placements/policy", params={"path": "mpx/cairn"},
                           headers=console.headers()).json()  # fmt: skip
        unknown = client.get("/admin/api/v1/placements/policy", params={"path": "mpx/spare"},
                           headers=console.headers())  # fmt: skip
        queue = client.get("/admin/api/v1/tools/queue", headers=console.headers()).json()
    # the CLI's document, plus the upstream names behind its labels (review 579 N1)
    names = {"open": [], "deny": ["delete_file"], "allowlist": [], "pinned_tools": [],
             "arguments": {}}  # fmt: skip
    assert one == {"placements": [{**shown, "names": names}]}
    assert under == {"placements": [{**shown, "names": names}]}
    assert unknown.status_code == 409  # an unknown path, refused as `tree show` refuses it
    assert queue == {"queue": queued}


def test_br_215_a_mode_change_binds_the_next_tools_list(gw: Gateway) -> None:
    bed = Bed(gw)
    up = bed.connection("ab", A)
    up.tools = [tool("r", annotations=READ), tool("w")]
    token = bed.token(A)
    console = Console(gw)
    with bed.serve() as public, console.client() as admin:
        console.admin(admin)
        assert sorted(names(public, A, token)) == ["ab__r", "ab__w"]
        done = console.call(admin, "PUT", "placements/mode",
                            {"connection": "mpx/ab", "role": A, "mode": "read"})  # fmt: skip
        assert done.status_code == 200, done.text
        assert (done.json()["mode"], done.json()["tools_notice"]) == ("read", True)
        assert names(public, A, token) == ["ab__r"]
        preview = console.call(admin, "POST", "rights", {"role": A}).json()
        assert preview["exposed"] == ["ab__r"]


def test_br_211_diff_and_validate_are_the_clis(gw: Gateway) -> None:
    bed = _bed(gw)
    gw.ok("policy", "pin", C, "--role", A, "on")
    catalogue(gw, C, A)  # the listing records the baselines of the pinned placement
    up = bed.upstream("gt")
    changed = [dict(t) for t in up.tools]
    changed[0] = {**changed[0], "description": "changed <b>upstream</b>"}
    up.tools = changed
    cli = json.loads(gw.ok("tool", "diff", "mpx", "--json"))
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        api = console.call(client, "POST", "tools/diff", {"path": "mpx"})
        assert api.status_code == 200, api.text
        assert api.json() == cli
        assert cli["changes"]
        done = console.call(client, "POST", "tools/validate", {"path": "mpx", "connection": C})
        assert done.status_code == 200, done.text
        assert done.json()["tools_notice"] is True
        assert [v["tool"] for v in done.json()["validated"]] == [changed[0]["name"]]
        again = console.call(client, "POST", "tools/diff", {"path": "mpx"})
    assert again.json()["changes"] == []
