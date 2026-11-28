"""BR-083 — an outbound dynamic registration is kept before use and never redone on its own
(AC-12), against the fake provider."""

from __future__ import annotations

import json

from tests import fake_provider
from tests.support import REGISTRY, Gateway, standard_tree

ROLE = "mpx/cairn/staging/dev"


def _db_bytes(gw: Gateway) -> bytes:
    data = gw.path.read_bytes()
    wal = gw.path.with_name(gw.path.name + "-wal")
    return data + (wal.read_bytes() if wal.exists() else b"")


def test_br_083_registration_stored_encrypted_before_authorization(gw: Gateway) -> None:
    standard_tree(gw)
    provider = gw.provider()
    provider.dcr_secret = True
    provider.dcr_secret_expires_at = int(gw.clock()) + 30 * 86400
    gw.service()
    out = gw.ok("application", "declare", "dyn", "--service", "gitea-dev", "--mode", "dynamic")
    [register] = provider.calls(fake_provider.REGISTER)
    assert register.body["token_endpoint_auth_method"] == "none"  # noqa: S105
    # the loopback URI, and the console's return URI since F-016 slice 2 (BR-216)
    assert register.body["redirect_uris"] == [
        "http://127.0.0.1/oauth/callback",
        "http://localhost:14791/admin/oauth/callback",
    ]
    [client_id] = list(provider.clients)
    secret = provider.clients[client_id]
    assert secret is not None
    assert secret in REGISTRY.secrets
    assert f"client_id: {client_id}" in out
    assert "secret: encrypted" in out
    conn = gw.connect()
    try:
        row = conn.execute(
            "SELECT client_id, secret_enc, mode, auth_method FROM application WHERE id = 'dyn'"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == client_id
    assert row[1] is not None
    assert row[2:] == ("dynamic", "client_secret_post")
    assert secret.encode() not in _db_bytes(gw)
    assert provider.calls(fake_provider.TOKEN) == []  # before any authorization
    [listed] = json.loads(gw.ok("application", "list", "--json"))
    assert listed["secret_expires_at"] is not None
    assert "secret expire" in gw.ok("application", "list")
    # a second identical command: refusal, no new registration
    again = gw.cli("application", "declare", "dyn", "--service", "gitea-dev", "--mode", "dynamic")
    assert again.code == 2
    assert len(provider.calls(fake_provider.REGISTER)) == 1
    # the returned secret is presented by the returned method (client_secret_post)
    gw.oauth("mpx/gitea-dev", "gd", "gitea-dev", "dyn")
    [exchange] = provider.calls(fake_provider.TOKEN, "authorization_code")
    assert exchange.form["client_id"] == client_id
    assert exchange.form["client_secret"] == secret


def test_br_083_reregister_moves_connections_to_reauth_and_revokes_with_old_client(
    gw: Gateway,
) -> None:
    standard_tree(gw)
    provider = gw.provider()
    gw.service()
    gw.ok("application", "declare", "dyn", "--service", "gitea-dev", "--mode", "dynamic")
    old_client = next(iter(provider.clients))
    gw.oauth("mpx/a", "ga", "gitea-dev", "dyn")
    gw.oauth("mpx/b", "gb", "gitea-dev", "dyn")
    gw.ok("connection", "place", "mpx/a", ROLE)
    tokens = [gw.tokens("mpx/a"), gw.tokens("mpx/b")]
    refused = gw.cli("application", "reregister", "dyn")
    assert refused.code == 2
    assert "mpx/a, mpx/b" in refused.err
    assert len(provider.calls(fake_provider.REGISTER)) == 1
    out = gw.ok("application", "reregister", "dyn", "--confirm", "dyn")
    assert "mpx/a: reauthorization required" in out
    new_client = next(c for c in provider.clients if c != old_client)
    for name in ("mpx/a", "mpx/b"):
        row = gw.row(name)
        assert (row["state"], row["tokens_enc"]) == ("reauth_required", None)
    assert [
        p["role"]
        for p in json.loads(gw.ok("connection", "status", "mpx/a", "--json"))[0]["placements"]
    ] == [ROLE]
    revoked = {(client, token) for _hint, client, token in provider.revoked}
    for t in tokens:
        assert t is not None
        assert (old_client, t["refresh_token"]) in revoked
        assert (old_client, t["access_token"]) in revoked
    entry = [e for e in gw.audit() if e["action"] == "application.reregister"][-1]
    assert json.loads(entry["before"])["client_id"] == old_client
    assert json.loads(entry["after"])["client_id"] == new_client
    # an application declared by hand is not reregistered
    gw.application("manuelle")
    assert gw.cli("application", "reregister", "manuelle", "--confirm", "manuelle").code == 2
