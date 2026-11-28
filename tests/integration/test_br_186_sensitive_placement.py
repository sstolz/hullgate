"""BR-186, BR-112 (F-015 AC-8, AC-11, D3): a placement is sensitive iff its connection is placed on
at least one sensitive role, read from the database at each request. The non-sensitive role of a
shared connection is held by BR-175; marking an environment holds it at the next request, warm
cache included; the preview names the sensitive roles (`sensitive_via`)."""

from __future__ import annotations

import json

import pytest

from tests.fake_upstream import READ, tool
from tests.policy_support import catalogue
from tests.relay_bed import A, Bed, S, names
from tests.support import Gateway

SH = "mpx/sh"


@pytest.fixture
def bed(gw: Gateway) -> Bed:
    b = Bed(gw)
    up = b.connection("sh", A)
    up.tools = [tool("look", annotations=READ), tool("edit")]
    catalogue(gw, SH, A)
    return b


def test_br_186_shared_connection_holds_the_plain_role(bed: Bed) -> None:
    gw = bed.gw
    with bed.serve() as client:
        assert names(client, A, bed.token(A)) == ["sh__look", "sh__edit"]
        gw.ok("connection", "place", SH, S, "--confirm-share")  # warm cache
        assert names(client, A, bed.token(A)) == []
        gw.ok("policy", "add", SH, "--role", A, "allowlist", "look", "edit")
        assert names(client, A, bed.token(A)) == ["sh__look"]  # `edit` is write: closed
        gw.ok("connection", "unplace", SH, S)
        assert names(client, A, bed.token(A)) == ["sh__look", "sh__edit"]
    shown = json.loads(gw.ok("policy", "show", SH, "--role", A, "--json"))
    assert shown["sensitive"] is False


def test_br_186_marking_the_environment_holds_at_the_next_request(bed: Bed) -> None:
    gw = bed.gw
    with bed.serve() as client:
        assert names(client, A, bed.token(A)) == ["sh__look", "sh__edit"]
        gw.ok("tree", "sensitive", "mpx/cairn/staging", "on")
        assert names(client, A, bed.token(A)) == []
        gw.ok("tree", "sensitive", "mpx/cairn/staging", "off")
        assert names(client, A, bed.token(A)) == ["sh__look", "sh__edit"]


def test_br_186_preview_names_the_sensitive_roles(bed: Bed) -> None:
    gw = bed.gw
    gw.ok("connection", "place", SH, S, "--confirm-share")
    seen = json.loads(gw.ok("policy", "preview", A, "--json"))
    assert {tuple(t["sensitive_via"]) for t in seen["tools"]} == {(S,)}
    own = json.loads(gw.ok("policy", "preview", S, "--json"))
    assert {tuple(t["sensitive_via"]) for t in own["tools"]} == {()}
