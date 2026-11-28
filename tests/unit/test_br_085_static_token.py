"""BR-085 — a provided-token connection is never renewed; its value changes through
`reauthorize --file` (lot A; the refusal signal, `signal_refusal`, is in lot B, AC-14)."""

from __future__ import annotations

import asyncio
import json
import secrets

import pytest

from hullgate import connections
from tests import fake_provider
from tests.fake_provider import Reply
from tests.support import REGISTRY, Gateway, standard_tree


def _access(gw: Gateway, name: str, role: str) -> connections.Access:
    return asyncio.run(
        connections.get_access(
            gw.db,
            gw.connections_key(),
            gw.connection_id(name),
            gw.node_id(role),
            clock=gw.clock,
            out=gw.outbound,
        )
    )


def _rows(gw: Gateway) -> tuple[int, int]:
    conn = gw.connect()
    try:
        return (
            int(conn.execute("SELECT count(*) FROM connection WHERE removed = 0").fetchone()[0]),
            int(conn.execute("SELECT count(*) FROM placement").fetchone()[0]),
        )
    finally:
        conn.close()


def test_br_085_static_token_active_and_served_without_renewal(gw: Gateway) -> None:
    standard_tree(gw)
    gw.service()
    value = gw.static("mpx/gitea-dev", "gd")
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/staging/dev")
    access = _access(gw, "mpx/gitea-dev", "mpx/cairn/staging/dev")
    assert access.authorization == f"Bearer {value}"
    assert value not in repr(access)
    assert value not in str(access)
    gw.clock.advance(400 * 24 * 3600)  # no deadline, no renewal
    assert (
        _access(gw, "mpx/gitea-dev", "mpx/cairn/staging/dev").authorization == access.authorization
    )


def test_br_085_replace_value_keeps_name_prefix_placements(gw: Gateway) -> None:
    standard_tree(gw)
    gw.service()
    old = gw.static("mpx/gitea-dev", "gd")
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/staging/dev")
    # moved to `reauth_required` (the refusal signal is in lot B: written here)
    conn = gw.connect()
    conn.execute("UPDATE connection SET state = 'reauth_required'")
    conn.commit()
    conn.close()
    with pytest.raises(connections.ReauthRequired):
        _access(gw, "mpx/gitea-dev", "mpx/cairn/staging/dev")
    before = _rows(gw)
    first = gw.connection_id("mpx/gitea-dev")
    new = "nouvelle-" + secrets.token_urlsafe(24)
    REGISTRY.secrets.append(new)
    gw.ok("connection", "reauthorize", "mpx/gitea-dev", "--file", "-", "--account", "cairn-ai",
          stdin=new + "\n")  # fmt: skip
    assert _rows(gw) == before
    assert gw.connection_id("mpx/gitea-dev") == first
    access = _access(gw, "mpx/gitea-dev", "mpx/cairn/staging/dev")
    assert access.authorization == f"Bearer {new}"
    assert access.generation == 1
    [state] = json.loads(gw.ok("connection", "status", "--json"))
    assert (state["state"], state["prefix"], state["account"]) == ("active", "gd", "cairn-ai")
    entry = [e for e in gw.audit() if e["action"] == "connection.replace_value"][-1]
    assert json.loads(entry["after"])["generation"] == 1
    for f in (gw.path, gw.path.with_name(gw.path.name + "-wal")):
        if f.exists():
            assert old.encode() not in f.read_bytes()


def test_br_085_reauthorize_file_forms(gw: Gateway) -> None:
    standard_tree(gw)
    gw.service()
    gw.static("mpx/gitea-dev", "gd")
    gw.ok("application", "declare", "app", "--service", "gitea-dev", "--mode", "declared",
          "--client-id", "c")  # fmt: skip
    gw.ok("connection", "add", "mpx/oauth", "--service", "gitea-dev", "--application", "app",
          "--prefix", "go")  # fmt: skip
    oauth_file = gw.cli("connection", "reauthorize", "mpx/oauth", "--file", "-", stdin="v\n")
    assert oauth_file.code == 2
    assert gw.cli("connection", "reauthorize", "mpx/gitea-dev").code == 2


def test_br_085_pending_oauth_connection_is_not_authorized(gw: Gateway) -> None:
    standard_tree(gw)
    gw.service()
    gw.ok("application", "declare", "app", "--service", "gitea-dev", "--mode", "declared",
          "--client-id", "c")  # fmt: skip
    gw.ok("connection", "add", "mpx/oauth", "--service", "gitea-dev", "--application", "app",
          "--prefix", "go")  # fmt: skip
    gw.ok("connection", "place", "mpx/oauth", "mpx/cairn/staging/dev")
    with pytest.raises(connections.NotAuthorized) as err:
        _access(gw, "mpx/oauth", "mpx/cairn/staging/dev")
    assert err.value.code == "access_not_authorized"
    with pytest.raises(connections.NotPlaced):
        _access(gw, "mpx/oauth", "mpx/cairn/staging/principal")


def test_br_085_signaled_refusal_static_token(gw: Gateway) -> None:
    """AC-14 (lot B): `signal_refusal` of the current generation → reauthentication required;
    stale generation → ignored; `reauthorize --file` sets `active` again; no renewal."""
    standard_tree(gw)
    gw.service()
    value = gw.static("mpx/gitea-dev", "gd")
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/staging/dev")
    access = gw.access("mpx/gitea-dev", "mpx/cairn/staging/dev")
    gw.clock.advance(400 * 24 * 3600)
    assert gw.access("mpx/gitea-dev", "mpx/cairn/staging/dev").authorization == f"Bearer {value}"
    assert all(call.method == "GET" for call in gw.provider().journal)  # no renewal
    gw.signal("mpx/gitea-dev", access.generation - 1)  # stale: ignored
    assert gw.row("mpx/gitea-dev")["state"] == "active"
    gw.signal("mpx/gitea-dev", access.generation)
    assert gw.row("mpx/gitea-dev")["state"] == "reauth_required"
    with pytest.raises(connections.ReauthRequired):
        gw.access("mpx/gitea-dev", "mpx/cairn/staging/dev")
    new = "retry-" + secrets.token_urlsafe(24)
    REGISTRY.secrets.append(new)
    gw.ok("connection", "reauthorize", "mpx/gitea-dev", "--file", "-", stdin=new + "\n")
    renewed = gw.access("mpx/gitea-dev", "mpx/cairn/staging/dev")
    assert renewed.authorization == f"Bearer {new}"
    gw.signal("mpx/gitea-dev", access.generation)  # signal of the old value: ignored
    assert gw.row("mpx/gitea-dev")["state"] == "active"
    effects = [
        json.loads(e["detail"])["effect"]
        for e in gw.audit()
        if e["action"] == "connection.refusal_signal"
    ]
    assert effects == [
        "ignored (stale generation)",
        "reauthentication required",
        "ignored (stale generation)",
    ]


def test_br_085_signaled_refusal_oauth_renews_once(gw: Gateway) -> None:
    """AC-14 (OAuth): the generation's access token is marked expired; the next `get_access`
    renews only once even after three signals; `invalid_grant` → reauth."""
    standard_tree(gw)
    provider = gw.oauth()
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/staging/dev")
    access = gw.access("mpx/gitea-dev", "mpx/cairn/staging/dev")
    for _ in range(3):
        gw.signal("mpx/gitea-dev", access.generation)
    renewed = gw.access("mpx/gitea-dev", "mpx/cairn/staging/dev")
    assert renewed.generation == access.generation + 1
    assert gw.access("mpx/gitea-dev", "mpx/cairn/staging/dev").generation == renewed.generation
    assert len(provider.calls(fake_provider.TOKEN, "refresh_token")) == 1
    gw.signal("mpx/gitea-dev", access.generation)  # stale generation: ignored
    assert gw.access("mpx/gitea-dev", "mpx/cairn/staging/dev").generation == renewed.generation
    gw.signal("mpx/gitea-dev", renewed.generation)
    provider.script.append(Reply(status=400, body={"error": "invalid_grant"}))
    with pytest.raises(connections.ReauthRequired):
        gw.access("mpx/gitea-dev", "mpx/cairn/staging/dev")
    assert gw.row("mpx/gitea-dev")["state"] == "reauth_required"
