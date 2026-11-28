"""BR-051 — under a sensitive environment, only a token placed on the role passes."""

from __future__ import annotations

import json

from tests.support import REGISTRY, Gateway, mcp, standard_tree


def test_br_051_project_token_refused_on_sensitive(gw: Gateway) -> None:
    """AC-03, the ticket's criterion: a `mpx/cairn` token is refused on `mpx/cairn/prod/<role>`."""
    standard_tree(gw)
    _, token = gw.issue("mpx/cairn")
    with gw.client() as client:
        refused = mcp(client, "mpx/cairn/prod/dev", token)
        accepted = mcp(client, "mpx/cairn/staging/dev", token)
    assert refused.status_code == 401
    assert refused.json() == {"error": "invalid_token"}
    assert 'error="invalid_token"' in refused.headers["www-authenticate"]
    assert accepted.status_code == 200
    entry = [e for e in gw.audit() if e["action"] == "access.refused"][-1]
    assert (entry["result"], entry["reason"]) == ("refused", "sensitive_requires_role_token")
    assert entry["path"] == "mpx/cairn/prod/dev"


def test_br_051_client_and_root_tokens_refused_on_sensitive(gw: Gateway) -> None:
    standard_tree(gw)
    _, client_token = gw.issue("mpx")
    _, root = gw.issue("/", "racine", "--confirm", "*")
    with gw.client() as client:
        for token in (client_token, root):
            assert mcp(client, "mpx/cairn/prod/dev", token).status_code == 401
            assert mcp(client, "mpx/cairn/staging/dev", token).status_code == 200


def test_br_051_role_token_accepted_only_on_its_role(gw: Gateway) -> None:
    standard_tree(gw)
    _, dev = gw.issue("mpx/cairn/prod/dev")
    with gw.client() as client:
        assert mcp(client, "mpx/cairn/prod/dev", dev).status_code == 200
        assert mcp(client, "mpx/cairn/prod/principal", dev).status_code == 401


def test_br_051_token_on_sensitive_environment_refused_at_issuance(gw: Gateway) -> None:
    standard_tree(gw)
    result = gw.cli("token", "create", "--path", "mpx/cairn/prod", "--label", "env")
    assert result.code == 2
    assert (
        "a token placed on a sensitive environment is accepted on none of its roles" in result.err
    )
    assert "hg_" not in result.out


def test_br_051_issuance_above_names_uncovered_sensitive_environments(gw: Gateway) -> None:
    standard_tree(gw)
    out = gw.ok("token", "create", "--path", "mpx", "--label", "client")
    assert "not covered (sensitive environment): mpx/cairn/prod" in out
    REGISTRY.secrets.append(out.strip().splitlines()[-1])
    data = json.loads(gw.ok("token", "create", "--path", "mpx", "--label", "c2", "--json"))
    REGISTRY.secrets.append(data["value"])
    assert data["uncovered_sensitive"] == ["mpx/cairn/prod"]


def test_br_051_unmarking_reopens_and_marking_closes_at_next_request(gw: Gateway) -> None:
    standard_tree(gw)
    _, token = gw.issue("mpx/cairn")
    with gw.client() as client:
        assert mcp(client, "mpx/cairn/prod/dev", token).status_code == 401
        gw.ok("tree", "sensitive", "mpx/cairn/prod", "off")
        assert mcp(client, "mpx/cairn/prod/dev", token).status_code == 200
        gw.ok("tree", "sensitive", "mpx/cairn/prod", "on")
        assert mcp(client, "mpx/cairn/prod/dev", token).status_code == 401
