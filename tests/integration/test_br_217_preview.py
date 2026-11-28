"""BR-217 (AC-16) — the rights preview is the relay's own decision.

For a role, `POST rights` answers what `policy preview` returns, from the evaluator `tools/list`
uses: the preview's exposed names equal a real `tools/list` under a token of that role, no tool is
called, and each hidden tool carries the rule hiding it. An upstream that could not be read is
unknown, never absent. For a token on a role, the answer first says whether the token reaches
that role and why (steps 4 to 7 of BR-055, BR-051), without its value and writing nothing, then
gives the role's preview.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.admin_support import Console
from tests.fake_upstream import DESTRUCTIVE, NONE, READ, Fault, tool
from tests.integration.test_br_174_list_and_call import EXPECTED, SETUPS, TOOLS
from tests.policy_support import catalogue, rows
from tests.relay_bed import A, B, Bed, S, names
from tests.support import Gateway, add_lineage


@pytest.fixture
def bed(gw: Gateway) -> Bed:
    return Bed(gw)


def _rights(console: Console, client: Any, role: str, token: str | None = None) -> Any:
    body = {"role": role} if token is None else {"role": role, "token": token}
    answer = console.call(client, "POST", "rights", body)
    assert answer.status_code == 200, answer.text
    return answer.json()


def _token_id(console: Console, client: Any, path: str, kind: str = "operator") -> str:
    listed = console.call(client, "GET", "tokens").json()["tokens"]
    return str(next(t["id"] for t in listed if t["type"] == kind and t["path"] == path))


def _token_state(gw: Gateway) -> tuple[list[tuple[Any, ...]], ...]:
    return (
        rows(gw, "SELECT * FROM operator_token ORDER BY id"),
        rows(gw, "SELECT * FROM lineage ORDER BY id"),
        rows(gw, "SELECT * FROM access_jti ORDER BY 1"),
    )


@pytest.mark.parametrize("prefix", sorted(SETUPS))
def test_br_217_preview_names_equal_a_real_tools_list(bed: Bed, prefix: str) -> None:
    gw = bed.gw
    role, lists = SETUPS[prefix]
    up = bed.connection(prefix, role)
    up.tools = [dict(t) for t in TOOLS]
    catalogue(gw, f"mpx/{prefix}", role)
    for which, members in lists.items():
        gw.ok("policy", "add", f"mpx/{prefix}", "--role", role, which, *members)
    token = bed.token(role)
    console = Console(gw)
    with console.client() as admin:
        console.admin(admin)
        seen = _rights(console, admin, role)
    with bed.serve() as public:
        listed = names(public, role, token)
    assert sorted(seen["exposed"]) == sorted(listed)
    assert seen["unavailable"] == []
    hidden = {t["upstream_name"]: t for t in seen["tools"] if not t["exposed"]}
    for upstream, row in hidden.items():
        assert row["reasons"][0] == EXPECTED[prefix][upstream]  # the rule hiding it
    assert up.calls("tools/call") == []


def test_br_217_the_api_gives_the_clis_preview(bed: Bed) -> None:
    gw = bed.gw
    up = bed.connection("ab", A)
    up.tools = [tool("a", annotations=READ), tool("d", annotations=DESTRUCTIVE)]
    cli = json.loads(gw.ok("policy", "preview", A, "--json"))
    console = Console(gw)
    with console.client() as admin:
        console.admin(admin)
        seen = _rights(console, admin, A)
    assert seen["tools"] == cli["tools"]
    assert seen["unavailable"] == cli["unavailable"]
    assert seen["policy_errors"] == cli["policy_errors"] == []
    assert seen["token"] is None


def test_br_217_an_unreachable_upstream_is_unknown_never_absent(bed: Bed) -> None:
    gw = bed.gw
    ok = bed.connection("ok", A)
    ok.tools = [tool("a", annotations=READ), tool("b", annotations=NONE)]
    down = bed.connection("dn", A)
    down.faults.append(Fault("status", on="tools/list", status=503))
    console = Console(gw)
    with console.client() as admin:
        console.admin(admin)
        seen = _rights(console, admin, A)
    assert seen["unavailable"] == [{"connection": "mpx/dn", "code": "upstream_unavailable"}]
    assert {t["connection"] for t in seen["tools"]} == {"mpx/ok"}


def test_br_217_nothing_placed_is_an_empty_preview(bed: Bed) -> None:
    console = Console(bed.gw)
    with console.client() as admin:
        console.admin(admin)
        seen = _rights(console, admin, B)
        refused = console.call(admin, "POST", "rights", {"role": "mpx/cairn"})
    assert (seen["tools"], seen["unavailable"], seen["exposed"]) == ([], [], [])
    cli = bed.gw.cli("policy", "preview", "mpx/cairn")
    assert refused.status_code == 409
    assert cli.err.strip() == f"refused: {refused.json()['message']}"


def test_br_217_a_token_of_the_role_reaches_it(bed: Bed) -> None:
    gw = bed.gw
    up = bed.connection("ab", A)
    up.tools = [tool("a", annotations=READ)]
    bed.token(A)
    console = Console(gw)
    with console.client() as admin:
        console.admin(admin)
        token_id = _token_id(console, admin, A)
        before = _token_state(gw)
        seen = _rights(console, admin, A, token_id)
    assert seen["token"] == {
        "token": token_id, "type": "operator", "role": A, "reaches": True, "step": 7,
        "reason": None,
    }  # fmt: skip
    assert seen["exposed"] == ["ab__a"]
    assert _token_state(gw) == before


def test_br_217_a_token_out_of_scope_says_why_and_is_not_used(bed: Bed) -> None:
    gw = bed.gw
    up = bed.connection("ab", A)
    up.tools = [tool("a", annotations=READ)]
    bed.token(B)
    gw.issue("mpx/cairn")  # covers S, but S is in a sensitive environment (BR-051)
    revoked = gw.issue(A, "old")[0]
    gw.ok("token", "revoke", revoked)
    console = Console(gw)
    with console.client() as admin:
        console.admin(admin)
        other = _token_id(console, admin, B)
        env = _token_id(console, admin, "mpx/cairn")
        before, audit_before = _token_state(gw), len(gw.audit())
        out_of_scope = _rights(console, admin, A, other)
        sensitive = _rights(console, admin, S, env)
        gone = _rights(console, admin, A, revoked)
        unknown = console.call(admin, "POST", "rights", {"role": A, "token": "nope"})
    assert (out_of_scope["token"]["reaches"], out_of_scope["token"]["reason"]) == (
        False, "out_of_scope",
    )  # fmt: skip
    assert out_of_scope["token"]["step"] == 6
    assert out_of_scope["exposed"] == ["ab__a"]  # the role's preview follows
    assert sensitive["token"]["reason"] == "sensitive_requires_role_token"
    assert gone["token"]["reason"] == "revoked"
    assert unknown.status_code == 409
    assert unknown.json()["message"] == "unknown token: nope"
    # the token's value is never used: no row of the token tables moved, no audit names it
    assert _token_state(gw) == before
    new = gw.audit()[audit_before:]
    assert all(e["actor"].startswith("admin:") for e in new)
    assert not [e for e in new if other in json.dumps(e) or env in json.dumps(e)]


def test_br_217_an_oauth_lineage_reaches_only_its_role(bed: Bed) -> None:
    gw = bed.gw
    bed.connection("ab", A)
    add_lineage(gw, A)
    console = Console(gw)
    with console.client() as admin:
        console.admin(admin)
        lineage = _token_id(console, admin, A, "oauth")
        own = _rights(console, admin, A, lineage)
        other = _rights(console, admin, B, lineage)
    assert (own["token"]["type"], own["token"]["reaches"]) == ("oauth", True)
    assert other["token"]["reaches"] is False
    assert other["token"]["reason"] in ("audience_mismatch", "out_of_scope")


def test_br_217_the_preview_is_audited_as_the_administrator(bed: Bed) -> None:
    gw = bed.gw
    up = bed.connection("ab", A)
    up.tools = [tool("a", annotations=READ)]
    console = Console(gw)
    with console.client() as admin:
        console.admin(admin)
        start = len(gw.audit())
        _rights(console, admin, A)
    listing = [e for e in gw.audit()[start:] if e["action"] == "connection.tools_list"]
    assert listing
    assert all(e["actor"].startswith("admin:") for e in listing)
