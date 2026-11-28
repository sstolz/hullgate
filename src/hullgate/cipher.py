"""Encrypted vault of connections: the organisation's key, AES-256-GCM envelope (BR-087, BR-088).

The connections key is a mounted file (`CONNECTIONS_KEY_FILE`, BR-032 v3): 32 bytes encoded in
base64, reread on every use like the signing key. It is never in the database: the database holds
only a key check (HMAC-SHA256 of a constant, truncated to 16 bytes), written in the transaction of
the first ciphertext. A well-formed key whose key check differs is treated as absent
(`key_different`): nothing is decrypted, written or erased by the gateway.

Envelope: version byte ‖ nonce (12 bytes, random) ‖ ciphertext and GCM tag. Associated data:
version ‖ table ‖ row id ‖ field, so that a ciphertext moved to another row or another field does
not authenticate (`IntegrityFailure`). No value or key is logged; the representation of the
objects that hold one is redacted.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import secrets
import sqlite3

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ENVELOPE_VERSION = 1
KEY_BYTES = 32
NONCE_BYTES = 12
CHECK_BYTES = 16
# The HMAC domain of the stored key check (BR-088, BR-131 v2). Changing it makes every stored key
# read `key_different`: it changed once, at F-011's fresh start, on an empty database.
_CHECK_LABEL = b"hullgate/connections/key-check/v1"
_FILE_MAX = 4096

# Reasons of an unavailable key, as `connection status` and the audit say them (BR-088).
ABSENT = "key_absent"
INVALID = "key_invalid"
DIFFERENT = "key_different"


class IntegrityFailure(Exception):
    """A ciphertext does not authenticate under a key whose check passes (BR-088)."""


class KeyUnavailable(Exception):
    """Connections key absent, invalid or different; `reason` is one of the three reasons."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _load(path: str) -> bytes | str:
    """The 32 bytes of the key, or the reason they are unavailable."""
    try:
        with open(path, "rb") as f:
            data = f.read(_FILE_MAX + 1)
    except OSError:
        return ABSENT  # removed or unreadable: the gateway has no key
    if len(data) > _FILE_MAX:
        return INVALID
    try:
        raw = base64.b64decode(data.strip(), validate=True)
    except (binascii.Error, ValueError):
        return INVALID
    if len(raw) != KEY_BYTES:
        return INVALID
    return raw


def key_check(raw: bytes) -> bytes:
    return hmac.new(raw, _CHECK_LABEL, hashlib.sha256).digest()[:CHECK_BYTES]


def stored_check(conn: sqlite3.Connection) -> bytes | None:
    row = conn.execute("SELECT key_check FROM vault_meta WHERE id = 1").fetchone()
    return None if row is None else bytes(row[0])


def _aad(table: str, row_id: str, field: str) -> bytes:
    return b"\x00".join(
        (bytes([ENVELOPE_VERSION]), table.encode(), row_id.encode(), field.encode())
    )


class Sealer:
    """Key loaded and checked against the database; encrypts and decrypts the upstream values."""

    __slots__ = ("_aead", "_check")

    def __init__(self, raw: bytes) -> None:
        self._aead = AESGCM(raw)
        self._check = key_check(raw)

    def __repr__(self) -> str:
        return "Sealer(<redacted>)"

    def seal(
        self, conn: sqlite3.Connection, table: str, row_id: str, field: str, value: str
    ) -> bytes:
        """Encrypts `value`; to call in the transaction that writes it (the key check is set
        there if the database has none yet, BR-088)."""
        if stored_check(conn) is None:
            conn.execute(
                "INSERT INTO vault_meta (id, envelope_version, key_check) VALUES (1, ?, ?)",
                (ENVELOPE_VERSION, self._check),
            )
        nonce = secrets.token_bytes(NONCE_BYTES)
        sealed = self._aead.encrypt(nonce, value.encode("utf-8"), _aad(table, row_id, field))
        return bytes([ENVELOPE_VERSION]) + nonce + sealed

    def open(self, blob: bytes, table: str, row_id: str, field: str) -> str:
        data = bytes(blob)
        if len(data) < 1 + NONCE_BYTES + 16 or data[0] != ENVELOPE_VERSION:
            raise IntegrityFailure(f"unknown envelope ({table}.{field})")
        try:
            plain = self._aead.decrypt(
                data[1 : 1 + NONCE_BYTES], data[1 + NONCE_BYTES :], _aad(table, row_id, field)
            )
        except InvalidTag as exc:
            raise IntegrityFailure(f"ciphertext not authenticated ({table}.{field})") from exc
        return plain.decode("utf-8")


class ConnectionsKey:
    """File of the connections key, reread on every call (a removal is seen at the next one)."""

    def __init__(self, path: str) -> None:
        self.path = path

    def __repr__(self) -> str:
        return f"ConnectionsKey({self.path!r})"

    def status(self, conn: sqlite3.Connection) -> str | None:
        """None if the key is usable with this database, otherwise the reason (BR-088)."""
        try:
            self.sealer(conn)
        except KeyUnavailable as exc:
            return exc.reason
        return None

    def sealer(self, conn: sqlite3.Connection) -> Sealer:
        """Key loaded and matching the database's key check, otherwise `KeyUnavailable`."""
        loaded = _load(self.path)
        if isinstance(loaded, str):
            raise KeyUnavailable(loaded)
        expected = stored_check(conn)
        if expected is not None and not hmac.compare_digest(expected, key_check(loaded)):
            raise KeyUnavailable(DIFFERENT)
        return Sealer(loaded)


def live_ciphertexts(conn: sqlite3.Connection) -> list[str]:
    """What still holds a ciphertext under the database's key (connections, applications,
    open authorizations); empty when the key check can be forgotten."""
    found: list[str] = []
    for table, column, label in (
        ("connection", "tokens_enc", "connection"),
        ("application", "secret_enc", "application"),
        ("pending_auth", "verifier_enc", "open authorization"),
    ):
        count = conn.execute(
            f"SELECT count(*) FROM {table} WHERE {column} IS NOT NULL"  # noqa: S608
        ).fetchone()[0]
        if count:
            found.append(f"{label}: {count}")
    return found


def forget_key_check(conn: sqlite3.Connection, now: float) -> list[str]:
    """Recovery gesture after a key loss (review 417 o1): forgets the key check when no
    ciphertext exists any more, so that a new key is adopted at the first ciphertext.

    Refused (nothing written) while a ciphertext lives: then returns what holds it. Audited.
    """
    from hullgate import audit
    from hullgate.db import transaction

    with transaction(conn):
        remaining = live_ciphertexts(conn)
        if remaining:
            return remaining
        had = stored_check(conn) is not None
        conn.execute("DELETE FROM vault_meta")
        audit.write(
            conn,
            actor=audit.actor(),
            action="vault.key_check_forgotten",
            result="ok" if had else "skipped",
            now=now,
        )
    return []


def generate_connections_key(path: str) -> None:
    """File of 32 random bytes in base64, mode 0400, never overwritten (the operator's gesture;
    throwaway key of the tests)."""
    value = base64.b64encode(secrets.token_bytes(KEY_BYTES)) + b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    with os.fdopen(fd, "wb") as f:
        f.write(value)
