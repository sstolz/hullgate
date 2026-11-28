"""BR-182, BR-211, BR-217 (F-016 slice 3, review 579) — the edges of `tools/validate`, of the two
listing routes and of the policy object's entries:

- a validation names what it lifts: a row's request (its role as `path`, its tool) leaves the
  connection's other placements suspended, and an empty `tool` is refused with the CLI's text,
  never read as "every tool" (B1, B2);
- a lone surrogate in an upstream's text, which the CLI's `--json` forms print ASCII-escaped,
  answers 200 on `tools/diff` and `rights` (B3);
- an entry whose label differs from its upstream name is removed by the name `names` carries
  (N1), and `"token": ""` on `rights` is an unknown token, not "no token" (N4).
"""

from __future__ import annotations

import json
import math
from typing import Any

from tests.admin_support import Console
from tests.fake_upstream import DESTRUCTIVE, READ, tool
from tests.policy_support import catalogue, rows
from tests.relay_bed import A, Bed, S
from tests.support import Gateway

API = "/admin/api/v1"
C = "mpx/ab"


def _raw(console: Console, client: Any, method: str, route: str, body: Any) -> Any:
    """A request whose body is ASCII-escaped JSON, so that a lone surrogate travels as a direct
    caller sends it (`"\\ud800"`)."""
    return client.request(
        method,
        f"{API}/{route}",
        content=json.dumps(body, ensure_ascii=True).encode("ascii"),
        headers={**console.headers(), "content-type": "application/json"},
    )


def _suspended(gw: Gateway) -> list[tuple[str, str]]:
    """(role path, tool) of each suspended pinned definition."""
    path = {gw.node_id(A): A, gw.node_id(S): S}
    found = rows(gw, "SELECT role_id, upstream_name FROM pinned_definition"
                     " WHERE suspended_at IS NOT NULL")  # fmt: skip
    return sorted((path[r], t) for r, t in found)


def _two_roles(gw: Gateway) -> Bed:
    """One connection on `staging/dev` and on the sensitive `prod/dev`, pinned on both, whose two
    tools change upstream: four suspensions."""
    bed = Bed(gw)
    up = bed.connection("ab", A, S, confirm=True)
    up.tools = [tool("a", annotations=READ), tool("b", annotations=READ)]
    for role in (A, S):
        gw.ok("policy", "pin", C, "--role", role, "on")
        catalogue(gw, C, role)
    up.tools = [{**tool("a", annotations=READ), "description": "changed a"},
                {**tool("b", annotations=READ), "description": "changed b"}]  # fmt: skip
    for role in (A, S):
        catalogue(gw, C, role)
    return bed


def test_br_182_a_rows_validation_lifts_that_row_only(gw: Gateway) -> None:
    _two_roles(gw)
    before = _suspended(gw)
    assert len(before) == 4
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        shown = _raw(console, client, "POST", "tools/diff", {"path": "mpx"}).json()["changes"]
        row = next(c for c in shown if c["role"] == A and c["tool"] == "a")
        # the body the console's row act sends (web/src/lib/policy.ts `validationBody`)
        done = _raw(console, client, "POST", "tools/validate",
                    {"path": row["role"], "connection": C, "tool": row["tool"]})  # fmt: skip
    assert done.status_code == 200, done.text
    assert [(v["role"], v["tool"]) for v in done.json()["validated"]] == [(A, "a")]
    assert _suspended(gw) == sorted([(A, "b"), (S, "a"), (S, "b")])  # prod/dev's `a` stays


def test_br_211_an_empty_tool_is_refused_as_the_cli_refuses_it(gw: Gateway) -> None:
    _two_roles(gw)
    before = _suspended(gw)
    cli = gw.cli("tool", "validate", A, C, "--tool", "")
    assert cli.code == 2, cli
    assert _suspended(gw) == before
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        api = _raw(console, client, "POST", "tools/validate",
                   {"path": A, "connection": C, "tool": ""})  # fmt: skip
    assert api.status_code == 409, api.text
    assert api.json()["code"] == "refused"
    assert cli.err.strip() == f"refused: {api.json()['message']}"
    assert _suspended(gw) == before


def test_br_217_an_empty_token_is_an_unknown_token(gw: Gateway) -> None:
    bed = Bed(gw)
    bed.connection("ab", A).tools = [tool("a", annotations=READ)]
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        api = _raw(console, client, "POST", "rights", {"role": A, "token": ""})
    assert api.status_code == 409, api.text
    assert api.json()["message"] == "unknown token: "


HOSTILE: list[tuple[str, dict[str, Any]]] = [
    ("description", {"description": "bad \ud800 text"}),
    ("title", {"title": "\udfff"}),
    ("schema key", {"inputSchema": {"type": "object",
                                    "properties": {"\ud800": {"enum": ["x"]}}}}),
]  # fmt: skip


def test_br_182_a_lone_surrogate_upstream_reads_as_a_change(gw: Gateway) -> None:
    """The diff answers the CLI's `tool diff --json` document, each hostile field included."""
    bed = Bed(gw)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        for (label, patch), prefix in zip(HOSTILE, ("ha", "hb", "hc"), strict=True):
            up = bed.connection(prefix, A)
            name = f"mpx/{prefix}"
            up.tools = [tool("a", annotations=READ), tool("d", annotations=DESTRUCTIVE)]
            gw.ok("policy", "pin", name, "--role", A, "on")
            catalogue(gw, name, A)
            up.tools = [{**tool("a", annotations=READ), **patch},
                        tool("d", annotations=DESTRUCTIVE)]  # fmt: skip
            cli = json.loads(gw.ok("tool", "diff", A, "--json"))
            api = _raw(console, client, "POST", "tools/diff", {"path": A})
            assert api.status_code == 200, (label, api.text)
            assert api.content.isascii(), label
            assert api.json() == cli, label
            mine = [c for c in api.json()["changes"] if c["connection"] == name]
            assert [c["tool"] for c in mine] == ["a"], label


def test_br_217_a_lone_surrogate_schema_key_answers_the_preview(gw: Gateway) -> None:
    bed = Bed(gw)
    up = bed.connection("ab", A)
    up.tools = [{**tool("a", annotations=READ), **HOSTILE[2][1]}]
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        api = _raw(console, client, "POST", "rights", {"role": A})
    assert api.status_code == 200, api.text
    assert api.content.isascii()  # written ASCII-escaped, as `policy preview --json` would be
    assert "\ud800" in json.dumps(api.json()["tools"], ensure_ascii=False)


def test_br_211_an_entry_is_removed_by_its_upstream_name(gw: Gateway) -> None:
    bed = Bed(gw)
    weird, long = "we\x1bird", "L" * 300
    up = bed.connection("ab", A)
    schema = {"type": "object", "properties": {"m": {"type": "string"}}}
    up.tools = [tool("a", annotations=READ), tool(weird, annotations=READ),
                tool(long, schema, annotations=READ)]  # fmt: skip
    catalogue(gw, C, A)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        added = _raw(
            console,
            client,
            "POST",
            "placements/lists",
            {"connection": C, "role": A, "list": "deny", "tools": [weird, long]},
        )
        assert added.status_code == 200, added.text
        shown = added.json()
        assert weird not in shown["deny"]  # labels
        assert long not in shown["deny"]
        assert sorted(shown["names"]["deny"]) == sorted([weird, long])
        assert len(shown["names"]["deny"]) == len(shown["deny"])
        pinned = _raw(
            console,
            client,
            "POST",
            "placements/tool-pins",
            {"connection": C, "role": A, "tool": weird},
        ).json()
        assert pinned["names"]["pinned_tools"] == [weird]
        ruled = _raw(console, client, "PUT", "placements/arguments",
                     {"connection": C, "role": A, "tool": long, "argument": "m",
                      "values": ["x"]}).json()  # fmt: skip
        assert list(ruled["names"]["arguments"].values()) == [long]
        # what the console's remove buttons send (`entryName`, `ruleName`)
        for route, body in [
            ("placements/lists/remove", {"list": "deny", "tools": shown["names"]["deny"]}),
            ("placements/tool-pins/remove", {"tool": pinned["names"]["pinned_tools"][0]}),
            (
                "placements/arguments/remove",
                {"tool": ruled["names"]["arguments"][next(iter(ruled["arguments"]))]},
            ),
        ]:
            gone = _raw(console, client, "POST", route, {"connection": C, "role": A, **body})
            assert gone.status_code == 200, (route, gone.text)
        last = gone.json()
    assert (last["deny"], last["pinned_tools"], last["arguments"]) == ([], [], {})
    assert last["names"] == {"open": [], "deny": [], "allowlist": [], "pinned_tools": [],
                             "arguments": {}}  # fmt: skip


def _strict(text: str) -> Any:
    """`JSON.parse`'s reading: `NaN` and `Infinity` are not JSON."""

    def refuse(token: str) -> Any:
        raise ValueError(f"not JSON: {token}")

    return json.loads(text, parse_constant=refuse)


def _cli_spelling(value: Any) -> Any:
    """The CLI's `--json` document with each non-finite number as the token it prints."""
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    if isinstance(value, dict):
        return {k: _cli_spelling(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_cli_spelling(v) for v in value]
    return value


def test_br_182_a_non_finite_number_upstream_reads_as_a_change(gw: Gateway) -> None:
    """Review 581 B3: `NaN` and `Infinity` in a pinned definition (the upstream's JSON may carry
    them, `json.loads` reads them) answer 200 with JSON a browser parses, each number written as
    the CLI prints it."""
    bed = Bed(gw)
    up = bed.connection("ab", A)
    up.tools = [tool("a", annotations=READ)]
    gw.ok("policy", "pin", C, "--role", A, "on")
    catalogue(gw, C, A)
    number = {"type": "number", "default": math.nan, "maximum": math.inf, "minimum": -math.inf}
    up.tools = [tool("a", {"type": "object", "properties": {"n": number}}, annotations=READ)]
    cli = gw.cli("tool", "diff", A, "--json")
    assert cli.code == 0, cli
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        api = _raw(console, client, "POST", "tools/diff", {"path": A})
    assert api.status_code == 200, api.text
    shown = _strict(api.text)
    assert shown == _cli_spelling(json.loads(cli.out))
    new = next(f["new"] for f in shown["changes"][0]["fields"] if f["field"] == "inputSchema")
    assert new["properties"]["n"] == {"type": "number", "default": "NaN", "maximum": "Infinity",
                                      "minimum": "-Infinity"}  # fmt: skip


def test_br_211_an_empty_selector_is_a_name(gw: Gateway) -> None:
    """Review 581 N5: `connection`, `role` or `service` given empty is refused as the CLI refuses
    it, never read as "every placement" or "every service"."""
    bed = Bed(gw)
    bed.connection("ab", A).tools = [tool("a", annotations=READ)]
    catalogue(gw, C, A)
    cases = [
        (("policy", "show", "", "--role", A), {"connection": "", "role": A}, "placements/policy"),
        (("policy", "show", C, "--role", ""), {"connection": C, "role": ""}, "placements/policy"),
        (("tool", "queue", "--service", ""), {"service": ""}, "tools/queue"),
    ]
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        for argv, params, route in cases:
            cli = gw.cli(*argv)
            assert cli.code == 2, (argv, cli)
            api = client.get(f"{API}/{route}", params=params, headers=console.headers())
            assert api.status_code == 409, (params, api.text)
            # the message may end on the empty name: compare the line, both sides stripped
            assert cli.err.strip() == f"refused: {api.json()['message']}".strip(), argv
        both = client.get(f"{API}/placements/policy", params={"connection": "", "role": ""},
                          headers=console.headers())  # fmt: skip
    assert both.status_code == 409, both.text
