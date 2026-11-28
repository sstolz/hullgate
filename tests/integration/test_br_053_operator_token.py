"""BR-053 — operator token: value shown once, never displayed again."""

from __future__ import annotations

import hashlib
import json
import re

from tests.support import REGISTRY, Gateway, mcp, standard_tree

VALUE_RE = re.compile(r"^hg_[A-Za-z0-9_-]{43}$")


def test_br_053_value_printed_once_then_never_again(gw: Gateway) -> None:
    standard_tree(gw)
    out = gw.ok("token", "create", "--path", "mpx/cairn/staging/dev", "--label", "claude-code")
    values = [line for line in out.splitlines() if line.startswith("hg_")]
    assert len(values) == 1
    value = values[0]
    REGISTRY.secrets.append(value)
    assert VALUE_RE.match(value)
    assert out.count(value) == 1
    digest = hashlib.sha256(value.encode()).hexdigest()
    with gw.client() as client:
        assert mcp(client, "mpx/cairn/staging/dev", value).status_code == 200
        mcp(client, "mpx/cairn/prod/dev", value)  # refusal audited
    outputs = [
        gw.ok("token", "list"),
        gw.ok("token", "list", "--json"),
        gw.ok("token", "list", "mpx", "--client", "mpx"),
        gw.ok("audit", "tail", "-n", "100"),
        gw.ok("audit", "tail", "-n", "100", "--json"),
        gw.ok("export"),
        gw.ok("tree", "show"),
        gw.ok("tree", "show", "--json"),
        gw.ok("settings", "get", "mpx/cairn/staging/dev"),
    ]
    for text in outputs:
        assert value not in text
        assert digest not in text
    listed = gw.ok("token", "list")
    for expected in ("claude-code", "mpx/cairn/staging/dev", "active"):
        assert expected in listed


def test_br_053_values_are_unique_and_random(gw: Gateway) -> None:
    standard_tree(gw)
    values = {gw.issue("mpx")[1] for _ in range(5)}
    assert len(values) == 5


def test_br_053_header_bearer_is_the_usage(gw: Gateway) -> None:
    standard_tree(gw)
    _, value = gw.issue("mpx/cairn/staging/dev")
    with gw.client() as client:
        ok = mcp(client, "mpx/cairn/staging/dev", headers={"Authorization": f"bearer {value}"})
        other_scheme = mcp(
            client, "mpx/cairn/staging/dev", headers={"Authorization": f"Token {value}"}
        )
    assert ok.status_code == 200
    assert other_scheme.status_code == 401
    assert "error=" not in other_scheme.headers["www-authenticate"]  # treated as absent


def test_br_053_admin_prefix_and_token_alias(gw: Gateway) -> None:
    standard_tree(gw)
    out = gw.ok("admin", "token", "create", "--path", "mpx", "--label", "alias", "--json")
    assert '"value": "hg_' in out
    data = json.loads(out)
    REGISTRY.secrets.append(data["value"])
    assert gw.ok("admin", "token", "revoke", data["id"]).startswith("revoked")
    assert gw.ok("token", "revoke", data["id"]).startswith("already revoked")
    unknown = gw.cli("token", "revoke", "tok_unknown")
    assert unknown.code == 2
    assert "unknown token" in unknown.err


def test_br_053_a_stored_token_of_another_prefix_is_refused_as_unknown(gw: Gateway) -> None:
    """F-011 AC-05 (BR-053 v3): the prefix gates the lookup. A token stored under a value of
    another prefix (the former one, built from fragments) is refused exactly like an unknown one,
    though its fingerprint is in the table."""
    standard_tree(gw)
    path = "mpx/cairn/staging/dev"
    token_id, value = gw.issue(path)
    foreign = "clo" + "_" + value.removeprefix("hg_")
    REGISTRY.secrets.append(foreign)
    conn = gw.connect()
    try:
        conn.execute(
            "UPDATE operator_token SET hash = ? WHERE id = ?",
            (hashlib.sha256(foreign.encode()).hexdigest(), token_id),
        )
        conn.commit()
    finally:
        conn.close()
    with gw.client() as client:
        stored = mcp(client, path, foreign)
        unknown = mcp(client, path, "hg_" + "Z" * 43)
    assert stored.status_code == unknown.status_code == 401
    assert stored.headers["www-authenticate"] == unknown.headers["www-authenticate"]
