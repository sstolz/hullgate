"""BR-088 — without the encryption key, the connections are unavailable and nothing is decrypted
(AC-17, lot A; `revoke` without a key is in lot B with `revoke`)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets

import pytest

from hullgate import cipher, connections
from tests.support import Gateway, standard_tree

ROLE = "mpx/cairn/staging/dev"


def _setup(gw: Gateway) -> None:
    standard_tree(gw)
    gw.service()
    gw.static("mpx/gitea-dev", "gd")
    gw.ok("connection", "place", "mpx/gitea-dev", ROLE)
    gw.ok("application", "declare", "app", "--service", "gitea-dev", "--mode", "declared",
          "--client-id", "c")  # fmt: skip


def _fingerprint(gw: Gateway) -> str:
    conn = gw.connect()
    try:
        rows = []
        for table in ("connection", "placement", "application", "service", "vault_meta"):
            rows += [repr(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]  # noqa: S608
    finally:
        conn.close()
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


def _access(gw: Gateway) -> connections.Access:
    return asyncio.run(
        connections.get_access(
            gw.db,
            gw.connections_key(),
            gw.connection_id("mpx/gitea-dev"),
            gw.node_id(ROLE),
            out=gw.outbound,
        )
    )


def _break_key(gw: Gateway, how: str) -> bytes:
    path = gw.connections_key_path
    original = path.read_bytes()
    os.chmod(path, 0o600)
    if how == "absent":
        path.unlink()
    elif how == "unreadable":
        os.chmod(path, 0o000)
    elif how == "invalid":
        path.write_bytes(base64.b64encode(secrets.token_bytes(31)))
    else:
        path.write_bytes(base64.b64encode(secrets.token_bytes(32)))
    return original


def _restore(gw: Gateway, original: bytes) -> None:
    path = gw.connections_key_path
    if path.exists():
        os.chmod(path, 0o600)
        path.unlink()
    path.write_bytes(original)


@pytest.mark.parametrize(
    ("how", "reason"),
    [
        ("absent", "key_absent"),
        ("unreadable", "key_absent"),
        ("invalid", "key_invalid"),
        ("different", "key_different"),
    ],
)
def test_br_088_key_unavailable_then_restored(gw: Gateway, how: str, reason: str) -> None:
    if how == "unreadable" and os.geteuid() == 0:
        pytest.skip("root reads a 0000 file")
    _setup(gw)
    assert _access(gw).authorization.startswith("Bearer ")
    original = _break_key(gw, how)
    before = _fingerprint(gw)
    try:
        with gw.client() as client:
            assert client.get("/readyz").status_code == 200
        [state] = json.loads(gw.ok("connection", "status", "--json"))
        assert (state["state"], state["key"]) == (reason, reason)
        assert reason in gw.ok("connection", "status")
        with pytest.raises(connections.KeyUnavailableError) as err:
            _access(gw)
        assert err.value.code == "access_key_unavailable"
        assert err.value.reason == reason
        assert _fingerprint(gw) == before
        entry = [e for e in gw.audit() if e["action"] == "connection.key_unavailable"][-1]
        assert entry["reason"] == reason
    finally:
        _restore(gw, original)
    assert _access(gw).authorization.startswith("Bearer ")  # recovery at the next call
    assert json.loads(gw.ok("connection", "status", "--json"))[0]["state"] == "active"


def test_br_088_refused_and_permitted_commands_without_key(gw: Gateway) -> None:
    _setup(gw)
    gw.static("mpx/autre", "ga")
    gw.oauth("mpx/oauthc", "go", "gitea-dev", "app")  # lot B: authorized OAuth connection
    gw.ok("application", "declare", "dyn", "--service", "gitea-dev", "--mode", "dynamic")
    gw.connections_key_path.chmod(0o600)
    gw.connections_key_path.unlink()
    refused = [
        ("connection", "authorize", "mpx/oauthc", "--callback", "-"),
        ("connection", "reauthorize", "mpx/oauthc"),
        ("connection", "renew", "mpx/oauthc"),
        ("application", "declare", "dyn2", "--service", "gitea-dev", "--mode", "dynamic"),
        ("application", "reregister", "dyn", "--confirm", "dyn"),
        ("connection", "add", "mpx/x", "--service", "gitea-dev", "--provided", "--file", "-",
         "--prefix", "gx"),
        ("connection", "add", "mpx/y", "--service", "gitea-dev", "--application", "app",
         "--prefix", "gy"),
        ("connection", "reauthorize", "mpx/gitea-dev", "--file", "-"),
        ("application", "declare", "app2", "--service", "gitea-dev", "--mode", "declared",
         "--client-id", "c", "--secret-file", "-"),
    ]  # fmt: skip
    before = _fingerprint(gw)
    for args in refused:
        result = gw.cli(*args, stdin="value\n")
        assert result.code == 1, (args, result)
        assert "key_absent" in result.err
    assert _fingerprint(gw) == before
    permitted = [
        ("connection", "status"),
        ("connection", "list"),
        ("connection", "prefix", "mpx/autre", "gz"),
        ("connection", "place", "mpx/autre", ROLE),
        ("connection", "unplace", "mpx/autre", ROLE),
        ("service", "list"),
        ("application", "list"),
        ("application", "declare", "app3", "--service", "gitea-dev", "--mode", "declared",
         "--client-id", "c"),
        ("application", "remove", "app3"),
        ("service", "declare", "s2", "--server", "https://s2.example.test"),
        ("service", "remove", "s2"),
        ("connection", "remove", "mpx/autre", "--confirm", "mpx/autre"),
        ("connection", "revoke", "mpx/oauthc"),
    ]  # fmt: skip
    for args in permitted:
        result = gw.cli(*args)
        assert result.code == 0, (args, result)
    entry = [e for e in gw.audit() if e["action"] == "connection.upstream_revoke"][-1]
    assert entry["reason"] == "key_unavailable"


def test_br_088_key_check_written_with_first_ciphertext(gw: Gateway) -> None:
    standard_tree(gw)
    gw.service()
    conn = gw.connect()
    try:
        assert cipher.stored_check(conn) is None
    finally:
        conn.close()
    gw.static("mpx/gitea-dev", "gd")
    conn = gw.connect()
    try:
        stored = cipher.stored_check(conn)
    finally:
        conn.close()
    raw = base64.b64decode(gw.connections_key_path.read_text())
    assert stored == cipher.key_check(raw)


def test_br_088_recovery_after_key_loss(gw: Gateway, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review 417 o1: key lost — the database refuses any other key (`key_different`); the
    recovery gesture (`python -m hullgate.keys connections-key-forget --confirm`) is refused while a
    ciphertext lives, then forgets the key check and a new key is adopted."""
    from hullgate import keys

    _setup(gw)
    gw.ok("application", "declare", "sec", "--service", "gitea-dev", "--mode", "declared",
          "--client-id", "c", "--secret-file", "-", stdin="secret-app-" + "q" * 20)  # fmt: skip
    _break_key(gw, "different")  # "key lost": a new secret was generated
    for name, value in gw.env.items():
        monkeypatch.setenv(name, value)
    added = gw.cli("connection", "add", "mpx/x", "--service", "gitea-dev", "--provided",
                   "--file", "-", "--prefix", "gx", stdin="v\n")  # fmt: skip
    assert added.code == 1
    assert "key_different" in added.err
    assert keys.main(["connections-key-forget"]) == 2  # without --confirm: usage
    assert keys.main(["connections-key-forget", "--confirm"]) == 2  # live ciphertexts
    gw.ok("connection", "remove", "mpx/gitea-dev", "--confirm", "mpx/gitea-dev")
    gw.ok("application", "remove", "sec")
    assert keys.main(["connections-key-forget", "--confirm"]) == 0
    value = gw.static("mpx/gitea-dev", "gd")  # new key adopted at the first ciphertext
    gw.ok("connection", "place", "mpx/gitea-dev", ROLE)
    assert _access(gw).authorization == f"Bearer {value}"
    actions = [e["action"] for e in gw.audit()]
    assert "vault.key_check_forgotten" in actions


def test_br_088_key_unavailable_audited_once_per_connection_and_reason(gw: Gateway) -> None:
    """Review 417 m2: one entry per (connection, reason) as long as the reason does not change;
    the `connection` row is not touched (the marker lives in `key_alert`)."""
    _setup(gw)
    original = _break_key(gw, "absent")
    before = _fingerprint(gw)
    try:
        for _ in range(20):
            with pytest.raises(connections.KeyUnavailableError):
                _access(gw)
        assert _fingerprint(gw) == before
        gw.connections_key_path.write_bytes(base64.b64encode(secrets.token_bytes(31)))
        for _ in range(5):
            with pytest.raises(connections.KeyUnavailableError):
                _access(gw)
    finally:
        _restore(gw, original)
    _access(gw)  # key restored: the marker drops
    original = _break_key(gw, "absent")
    try:
        with pytest.raises(connections.KeyUnavailableError):
            _access(gw)
    finally:
        _restore(gw, original)
    reasons = [e["reason"] for e in gw.audit() if e["action"] == "connection.key_unavailable"]
    assert reasons == ["key_absent", "key_invalid", "key_absent"]
