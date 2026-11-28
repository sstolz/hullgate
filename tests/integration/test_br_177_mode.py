"""BR-177, BR-048 (F-015 AC-9, AC-11): the effective mode of a placement is its own mode when set,
otherwise the effective value of the setting `mode` on its role: inherited value and origin,
override upward and downward, `inherit`; a change applies at the next request, warm cache
included, with no restart (BR-112)."""

from __future__ import annotations

import json

import pytest

from tests.fake_upstream import DESTRUCTIVE, READ, tool
from tests.policy_support import audits, catalogue
from tests.relay_bed import A, Bed, names
from tests.support import Gateway

MO = "mpx/mo"


@pytest.fixture
def bed(gw: Gateway) -> Bed:
    b = Bed(gw)
    up = b.connection("mo", A)
    up.tools = [tool("r", annotations=READ), tool("w"), tool("d", annotations=DESTRUCTIVE)]
    catalogue(gw, MO, A)
    return b


def _shown(gw: Gateway) -> tuple[str, str]:
    data = json.loads(gw.ok("policy", "show", MO, "--role", A, "--json"))
    return data["mode"], data["mode_origin"]


def test_br_177_settings_declare_the_three_keys(gw: Gateway) -> None:
    got = {e["key"]: (e["value"], e["origin"]) for e in json.loads(gw.ok("settings", "get", "/",
                                                                         "--json"))}  # fmt: skip
    assert got["mode"] == ("write", "/")
    assert got["pinning"] == ("auto", "/")
    assert got["response_cap"] == ("1MiB", "/")
    for key, bad in (("mode", "admin"), ("pinning", "yes"), ("response_cap", "2MiB"),
                     ("response_cap", "0KiB"), ("response_cap", "10kb")):  # fmt: skip
        assert gw.cli("settings", "set", "/", key, bad).code == 2


def test_br_177_inherited_override_and_inherit_at_the_next_request(bed: Bed) -> None:
    gw = bed.gw
    with bed.serve() as client:
        assert names(client, A, bed.token(A)) == ["mo__r", "mo__w"]
        assert _shown(gw) == ("write", "/")
        gw.ok("settings", "set", "mpx/cairn", "mode", "read")  # inherited, warm cache
        assert names(client, A, bed.token(A)) == ["mo__r"]
        assert _shown(gw) == ("read", "mpx/cairn")
        gw.ok("policy", "mode", MO, "--role", A, "destructive")  # upward override
        assert names(client, A, bed.token(A)) == ["mo__r", "mo__w", "mo__d"]
        assert _shown(gw) == ("destructive", "placement")
        gw.ok("settings", "set", "/", "mode", "destructive")
        gw.ok("policy", "mode", MO, "--role", A, "read")  # downward override
        assert names(client, A, bed.token(A)) == ["mo__r"]
        out = gw.ok("policy", "mode", MO, "--role", A, "inherit")
        assert "read (inherited)" in out
        assert names(client, A, bed.token(A)) == ["mo__r"]
        assert _shown(gw) == ("read", "mpx/cairn")
    changes = [(a["before"], a["after"]) for a in audits(gw, "policy.mode")]
    assert changes == [
        ({"mode": None}, {"mode": "destructive"}),
        ({"mode": "destructive"}, {"mode": "read"}),
        ({"mode": "read"}, {"mode": None}),
    ]
