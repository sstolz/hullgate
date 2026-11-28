"""BR-211 (AC-10) — one rule set, decided on the server.

The parity table maps every CLI verb, walked from `cli._parser()`, to an API route, to an exclusion
with its reason, or (F-016 lot A only, §9 delta) to the later lot of §10 that maps it. A verb
neither mapped, nor excluded, nor planned fails; a planned verb whose lot has shipped must be
moved to a route by that lot's sweep. Replayed after each lot.
"""

from __future__ import annotations

import argparse

from hullgate import cli
from tests.admin_support import Console, SoftAuthenticator
from tests.support import Gateway

HOST_ONLY = "host only by design: the console's administrators come from the host (BR-201)"
S5 = "slice 5"
SLICES = ("slice 2", "slice 3", "slice 4", S5)

# verb -> ("route", "METHOD path") | ("excluded", reason) | ("planned", slice of §10)
PARITY: dict[str, tuple[str, str]] = {
    "tree add": ("route", "POST nodes"), "tree rename": ("route", "POST nodes/rename"),
    "tree move": ("route", "POST nodes/move"), "tree delete": ("route", "POST nodes/delete"),
    "tree show": ("route", "GET nodes"), "tree sensitive": ("route", "POST nodes/sensitive"),
    "tree lock": ("route", "POST nodes/lock"), "tree unlock": ("route", "POST nodes/unlock"),
    "settings get": ("route", "GET settings"), "settings set": ("route", "PUT settings"),
    "settings unset": ("route", "POST settings/unset"),
    "token create": ("route", "POST tokens"), "token list": ("route", "GET tokens"),
    "token revoke": ("route", "POST tokens/revoke"),
    "audit tail": ("excluded", "replaced by the audit route, GET audit (BR-218)"),
    "client add": ("route", "POST clients"), "client list": ("route", "GET clients"),
    "client remove": ("route", "POST clients/remove"), "client dcr": ("route", "PUT clients/dcr"),
    "export": ("route", "GET export"),
    "import": ("route", "POST import/apply"),
    "undo": ("route", "POST changes/undo"),
    "service declare": ("route", "POST services"), "service list": ("route", "GET services"),
    "service remove": ("route", "POST services/remove"),
    "service rediscover": ("route", "POST services/rediscover"),
    "service upstream": ("route", "POST services/upstream"),
    "application declare": ("route", "POST applications"),
    "application list": ("route", "GET applications"),
    "application remove": ("route", "POST applications/remove"),
    "application reregister": ("route", "POST applications/reregister"),
    "connection add": ("route", "POST connections"),
    # its stdin handling is replaced by BR-216's paste field and web return
    "connection authorize": ("route", "POST connections/authorize"),
    "connection reauthorize": ("route", "POST connections/reauthorize"),
    "connection renew": ("route", "POST connections/renew"),
    "connection revoke": ("route", "POST connections/revoke"),
    "connection prefix": ("route", "PUT connections/prefix"),
    "connection place": ("route", "POST placements"),
    "connection unplace": ("route", "POST placements/remove"),
    "connection status": ("route", "GET connections"),
    "connection list": ("route", "GET connections"),
    "connection remove": ("route", "POST connections/remove"),
    "connection tools": ("route", "POST connections/tools"),
    "policy show": ("route", "GET placements/policy"),
    "policy mode": ("route", "PUT placements/mode"),
    "policy add": ("route", "POST placements/lists"),
    "policy remove": ("route", "POST placements/lists/remove"),
    "policy arg": ("route", "PUT placements/arguments"),
    "policy arg-remove": ("route", "POST placements/arguments/remove"),
    "policy pin": ("route", "PUT placements/pin"),
    "policy pin-tool": ("route", "POST placements/tool-pins"),
    "policy unpin-tool": ("route", "POST placements/tool-pins/remove"),
    "policy preview": ("route", "POST rights"),
    "tool queue": ("route", "GET tools/queue"), "tool classify": ("route", "POST tools/classify"),
    "tool accept": ("route", "POST tools/accept"), "tool diff": ("route", "POST tools/diff"),
    "tool validate": ("route", "POST tools/validate"),
    "admin recover": ("excluded", HOST_ONLY + "; BR-228 recovery"),
    "admin bootstrap": ("excluded", HOST_ONLY), "admin list": ("excluded", HOST_ONLY),
    "admin attempts": ("excluded", HOST_ONLY + "; BR-207's way back"),
    "admin allowlist": ("excluded", HOST_ONLY + "; BR-235's way back"),
    # F-020 (A12): the host approval of one pending request is BR-310's way past the barrier
    "consent list": ("excluded", HOST_ONLY + "; BR-310"),
    "consent approve": ("excluded", HOST_ONLY + "; BR-310's way past a closed barrier"),
}  # fmt: skip
# slices whose verbs must all be mapped or excluded by now
SHIPPED = {"slice 1", "slice 2", "slice 3", "slice 4", "slice 5"}
# Acts of the API with no CLI verb: reads, or named in F-016 §9 (BR-211's clarification).
API_ONLY = {
    "GET changes": "read: history and undo impact (BR-219–221)",
    "POST import/preview": "read: bounded YAML preview (BR-222)",
    "GET organisation": "read (slice 4)",
    "GET setup": "read (BR-229)",
    "GET tools/catalogue": "read (slice 4, §3.10)",
    "POST setup/dismiss": "BR-229 explicitly permits the guide dismissal",
    "GET impacts": "read: the impact of an act before it (BR-214)",
    "GET audit": "read (BR-218)",
    "POST audit/export": "read, audited as a trace (BR-218)",
    "POST connections/callback": "BR-216's web return, the console's form of connection authorize",
    "PUT applications/web-return": "F-016 §9: the web return flag of BR-216, set after the URI is "
    "added at the provider",
}


def _verbs() -> list[str]:
    def sub(p: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
        for action in p._actions:
            if isinstance(action, argparse._SubParsersAction):
                return dict(action.choices)
        return {}

    out: list[str] = []
    for group, gp in sub(cli._parser()).items():
        verbs = sub(gp)
        out.extend([f"{group} {v}" for v in verbs] if verbs else [group])
    return out


def test_br_211_every_cli_verb_is_mapped_excluded_or_planned() -> None:
    verbs = _verbs()
    assert sorted(verbs) == sorted(PARITY), set(verbs) ^ set(PARITY)
    for verb, (kind, target) in PARITY.items():
        assert kind in ("route", "excluded", "planned"), verb
        assert target, verb
        if kind == "planned":
            assert target in SLICES, verb
            assert target not in SHIPPED, verb


def test_br_211_mapped_verbs_name_existing_routes(gw: Gateway) -> None:
    routes = {f"{s.method} {s.path}" for s in Console(gw).app.api_routes}
    for verb, (kind, target) in PARITY.items():
        if kind == "route":
            assert target in routes, verb


def test_br_211_api_writes_are_audited_as_the_administrator(gw: Gateway) -> None:
    console = Console(gw)
    with console.client() as client:
        key = console.admin(client)
        assert console.fresh(client, key).status_code == 200
        assert console.add_passkey(client, SoftAuthenticator()).status_code == 201
        assert (
            console.call(client, "PUT", "allowlist", {"cidrs": ["203.0.113.0/24"]}).status_code
            == 200
        )
        assert console.call(client, "POST", "session/signout", {}).status_code == 200
    admin_id = gw.audit()[-1]["actor"].removeprefix("admin:")
    by_console = [e for e in gw.audit() if e["action"].startswith("admin.")
                  and e["action"] not in ("admin.create", "admin.enrolment.issue")]  # fmt: skip
    assert by_console
    assert {e["actor"] for e in by_console} == {f"admin:{admin_id}"}
    host = [e for e in gw.audit() if e["action"] in ("admin.create", "admin.enrolment.issue")]
    assert {e["actor"] for e in host} == {"operator-cli"}


def test_br_211_every_console_write_is_a_verb_or_named(gw: Gateway) -> None:
    """An act present in the API and not in the CLI is either a read or named in §9."""
    mapped = {target for kind, target in PARITY.values() if kind == "route"}
    lot_a = {"auth/", "enrol/", "session", "passkeys", "allowlist", "meta"}
    for spec in Console(gw).app.api_routes:
        route = f"{spec.method} {spec.path}"
        if any(spec.path.startswith(p) for p in lot_a):
            continue
        assert route in mapped or route in API_ONLY, route


HUGE_RATE = "9" * 4301 + "/min"


def test_br_211_a_refused_act_returns_the_clis_reason(gw: Gateway) -> None:
    gw.build(("/", "client", "MPX"))
    cases = [
        (("tree", "add", "mpx", "role", "X"), ("POST", "nodes",
         {"parent": "mpx", "level": "role", "name": "X"})),
        (("tree", "add", "nowhere", "project", "P"), ("POST", "nodes",
         {"parent": "nowhere", "level": "project", "name": "P"})),
        (("settings", "set", "mpx", "token_duration", "never"), ("PUT", "settings",
         {"path": "mpx", "key": "token_duration", "value": "never"})),
        # review 585, N1: more digits than int() converts is a refusal, not a ValueError
        (("settings", "set", "mpx", "token_rate_limit", HUGE_RATE), ("PUT", "settings",
         {"path": "mpx", "key": "token_rate_limit", "value": HUGE_RATE})),
        (("token", "revoke", "nope"), ("POST", "tokens/revoke", {"id": "nope"})),
    ]  # fmt: skip
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        for argv, (method, path, body) in cases:
            cli_result = gw.cli(*argv)
            assert cli_result.code == 2, argv
            api = console.call(client, method, path, body)
            assert api.status_code == 409, (path, api.text)
            assert api.json()["code"] == "refused"
            assert cli_result.err.strip() == f"refused: {api.json()['message']}", argv


def test_br_211_console_writes_are_audited_as_the_administrator(gw: Gateway) -> None:
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        added = console.call(client, "POST", "nodes",
                             {"parent": "/", "level": "client", "name": "MPX"})  # fmt: skip
        assert added.status_code == 201, added.text
        assert (
            console.call(
                client,
                "PUT",
                "settings",
                {"path": "mpx", "key": "token_duration", "value": "7d"},
            ).status_code
            == 200
        )
    admin = [e for e in gw.audit() if e["action"] in ("tree.add", "settings.set")]
    assert admin
    assert {e["actor"] for e in admin} == {f"admin:{_admin_id(gw)}"}
    gw.ok("settings", "set", "mpx", "token_duration", "14d")
    assert gw.audit()[-1]["actor"] == "operator-cli"


def _admin_id(gw: Gateway) -> str:
    conn = gw.connect()
    try:
        return str(conn.execute("SELECT id FROM administrator").fetchone()[0])
    finally:
        conn.close()
