"""BR-087 — the upstream secrets are encrypted at rest by the organisation's key (AC-16).

Population (`executable-guards.md` §The population): every table and every file of the database
(main, `-wal`, `VACUUM INTO` backup) after a tour of lot A (provided token, replacement of its
value, application secret) then of lot B (dynamic registration with a returned secret,
authorization, renewal); values: those of the test and of the fake provider.
"""

from __future__ import annotations

import asyncio
import secrets
import sqlite3
from pathlib import Path

import pytest

from hullgate import cipher, connections
from tests.support import REGISTRY, Gateway, standard_tree


def _flow(gw: Gateway) -> list[str]:
    standard_tree(gw)
    gw.service()
    values = [gw.static("mpx/gitea-dev", "gd"), gw.static("mpx/gitea-rev", "gr")]
    app_secret = "appsecret-" + secrets.token_urlsafe(24)
    REGISTRY.secrets.append(app_secret)
    gw.ok("application", "declare", "app", "--service", "gitea-dev", "--mode", "declared",
          "--client-id", "cid", "--secret-file", "-", stdin=app_secret)  # fmt: skip
    replaced = "replaced-" + secrets.token_urlsafe(24)
    REGISTRY.secrets.append(replaced)
    gw.ok("connection", "reauthorize", "mpx/gitea-rev", "--file", "-", stdin=replaced)
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/staging/dev")
    # Lot B : enregistrement dynamique (secret rendu), autorisation, renouvellement.
    provider = gw.provider()
    provider.dcr_secret = True
    gw.ok("application", "declare", "dyn", "--service", "gitea-dev", "--mode", "dynamic")
    gw.oauth("mpx/gitea-oauth", "go", "gitea-dev", "dyn")
    gw.ok("connection", "place", "mpx/gitea-oauth", "mpx/cairn/staging/dev")
    gw.expire("mpx/gitea-oauth", 30)
    gw.access("mpx/gitea-oauth", "mpx/cairn/staging/dev")
    upstream = [
        *provider.access_tokens,
        *provider.refresh_tokens,
        *provider.codes,
        *(s for s in provider.clients.values() if s is not None),
        *(c.form["code_verifier"] for c in provider.journal if "code_verifier" in c.form),
    ]
    assert len(provider.refresh_tokens) == 2
    assert len(provider.codes) == 1
    return [*values, app_secret, replaced, *upstream]


def _dump(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        rows: list[str] = []
        for table in tables:
            for row in conn.execute(f'SELECT * FROM "{table}"'):  # noqa: S608
                rows.append(repr(row))
        return "\n".join(rows)
    finally:
        conn.close()


def test_br_087_no_plaintext_in_tables_wal_or_backup(gw: Gateway, tmp_path: Path) -> None:
    values = _flow(gw)
    backup = tmp_path / "backup.db"
    conn = gw.connect()
    try:
        conn.execute("VACUUM INTO ?", (str(backup),))
    finally:
        conn.close()
    files = [gw.path, gw.path.with_name(gw.path.name + "-wal"), backup]
    assert files[1].exists()  # the -wal holds the last writes
    for value in values:
        for f in files:
            assert value.encode() not in f.read_bytes(), f
        assert value not in _dump(gw.path)
        assert value not in _dump(backup)
    key_text = gw.connections_key_path.read_text().strip()
    for f in files:
        assert key_text.encode() not in f.read_bytes()


def test_br_087_envelope_version_and_unique_nonce(gw: Gateway) -> None:
    standard_tree(gw)
    gw.service()
    value = "same-" + secrets.token_urlsafe(16)
    REGISTRY.secrets.append(value)
    for name, prefix in (("mpx/a", "ga"), ("mpx/b", "gb")):
        gw.ok("connection", "add", name, "--service", "gitea-dev", "--provided", "--file",
              "-", "--prefix", prefix, stdin=value)  # fmt: skip
    conn = gw.connect()
    try:
        blobs = [bytes(r[0]) for r in conn.execute("SELECT tokens_enc FROM connection")]
        meta = conn.execute("SELECT envelope_version, length(key_check) FROM vault_meta").fetchall()
    finally:
        conn.close()
    assert [b[0] for b in blobs] == [cipher.ENVELOPE_VERSION] * 2
    assert blobs[0] != blobs[1]
    assert blobs[0][1:13] != blobs[1][1:13]  # nonces distincts
    assert meta == [(1, 16)]


def test_br_087_ciphertext_moved_to_other_row_or_field_does_not_open(gw: Gateway) -> None:
    standard_tree(gw)
    gw.service()
    gw.static("mpx/gitea-dev", "gd")
    gw.static("mpx/gitea-rev", "gr")
    gw.ok("connection", "place", "mpx/gitea-rev", "mpx/cairn/staging/dev")
    dev, rev = gw.connection_id("mpx/gitea-dev"), gw.connection_id("mpx/gitea-rev")
    conn = gw.connect()
    try:
        conn.execute(
            "UPDATE connection SET tokens_enc = (SELECT tokens_enc FROM connection WHERE id = ?)"
            " WHERE id = ?",
            (dev, rev),
        )
        conn.commit()
        before = conn.execute("SELECT * FROM connection ORDER BY id").fetchall()
    finally:
        conn.close()
    role = gw.node_id("mpx/cairn/staging/dev")
    with pytest.raises(connections.IntegrityErrorAccess) as err:
        asyncio.run(connections.get_access(gw.db, gw.connections_key(), rev, role, out=gw.outbound))
    assert err.value.code == "access_integrity_error"
    conn = gw.connect()
    try:
        assert conn.execute("SELECT * FROM connection ORDER BY id").fetchall() == before
    finally:
        conn.close()
    assert [e for e in gw.audit() if e["action"] == "connection.integrity_error"]
    # same ciphertext, other field: refused too
    conn = gw.connect()
    try:
        blob = conn.execute("SELECT tokens_enc FROM connection WHERE id = ?", (dev,)).fetchone()
    finally:
        conn.close()
    sealer = gw.connections_key().sealer(gw.connect())
    assert sealer.open(blob[0], "connection", dev, "tokens")
    with pytest.raises(cipher.IntegrityFailure):
        sealer.open(blob[0], "application", dev, "secret")
    assert "redacted" in repr(sealer)
