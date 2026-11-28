"""BR-050 — a token covers its node and its descendants, segment by segment."""

from __future__ import annotations

import json

from hullgate import authorization, tree
from tests.support import Gateway, mcp, standard_tree


def _loaded(gw: Gateway) -> tree.Tree:
    conn = gw.connect()
    try:
        return tree.Tree.load(conn)
    finally:
        conn.close()


def test_br_050_covers_compares_identifiers_not_text(gw: Gateway) -> None:
    standard_tree(gw)
    gw.build(("mpx", "project", "Cai"), ("mpx/cai", "environment", "Dev"))
    t = _loaded(gw)
    role = t.resolve("mpx/cairn/staging/dev")
    for covering in ("/", "mpx", "mpx/cairn", "mpx/cairn/staging", "mpx/cairn/staging/dev"):
        assert authorization.covers(t, t.resolve(covering).id, role), covering
    for other in ("mpx/cai", "mpx/cai/dev", "mpx/cairn/prod", "mpx/cairn/staging/principal"):
        assert not authorization.covers(t, t.resolve(other).id, role), other


def test_br_050_textual_prefix_token_refused_at_endpoint(gw: Gateway) -> None:
    standard_tree(gw)
    gw.build(("mpx", "project", "Cai"))
    _, cai = gw.issue("mpx/cai", "textual prefix")
    _, cairn = gw.issue("mpx/cairn", "project")
    with gw.client() as client:
        assert mcp(client, "mpx/cairn/staging/dev", cai).status_code == 401
        assert mcp(client, "mpx/cairn/staging/dev", cairn).status_code == 200
    reasons = [e["reason"] for e in gw.audit() if e["action"] == "access.refused"]
    assert reasons == ["out_of_scope"]


def test_br_050_token_of_another_client_refused(gw: Gateway) -> None:
    standard_tree(gw)
    gw.build(("/", "client", "Acme"))
    _, acme = gw.issue("acme")
    with gw.client() as client:
        response = mcp(client, "mpx/cairn/staging/dev", acme)
    assert response.status_code == 401
    assert response.json() == {"error": "invalid_token"}


def test_br_050_root_token_requires_confirmation_and_is_marked(gw: Gateway) -> None:
    standard_tree(gw)
    refused = gw.cli("token", "create", "--path", "*", "--label", "root")
    assert refused.code == 2
    assert "--confirm '*'" in refused.err
    _, root = gw.issue("*", "root", "--confirm", "*")
    issue = [e for e in gw.audit() if e["action"] == "token.issue"][-1]
    assert json.loads(issue["detail"]) == {"root_token": True}
    assert issue["path"] == "/"
    with gw.client() as client:
        assert mcp(client, "mpx/cairn/staging/dev", root).status_code == 200
        assert mcp(client, "mpx/cairn/prod/dev", root).status_code == 401  # sensitive ones excluded
