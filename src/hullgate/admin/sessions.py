"""Console sessions (BR-204) and their CSRF token (BR-205) and freshness (BR-210).

A session is a random 256-bit value held in the `hullgate_admin` cookie and stored as its SHA-256
digest. It ends after 30 minutes without a request the operator caused, 8 hours after sign-in, at
sign-out, and (the acting session excepted) when one of its account's passkeys is deleted. The
sign-out and the expiry delete the row, so a value replayed afterwards is unknown.

The CSRF token is derived from the session value, so `GET /admin/api/v1/session` can return it
again under the cookie (a reload, a second tab) while the base keeps only digests: an attacker
without the cookie cannot compute it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import sqlite3
from dataclasses import dataclass

from hullgate.admin.accounts import digest, random_value

COOKIE = "hullgate_admin"
CSRF_HEADER = "x-csrf-token"
ACTIVITY_HEADER = "x-hullgate-activity"
IDLE_S = 30 * 60
ABSOLUTE_S = 8 * 3600
FRESH_S = 5 * 60


def csrf_token(value: str) -> str:
    raw = hashlib.sha256(b"hullgate-csrf\x00" + value.encode("ascii")).digest()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@dataclass(frozen=True, slots=True)
class Session:
    digest: str
    administrator_id: str
    csrf_digest: str
    created_at: int
    last_activity_at: int
    fresh_until: int

    def idle_expires_at(self) -> int:
        return self.last_activity_at + IDLE_S

    def expires_at(self) -> int:
        return min(self.idle_expires_at(), self.created_at + ABSOLUTE_S)

    def csrf_matches(self, token: str) -> bool:
        return hmac.compare_digest(digest_text(token), self.csrf_digest)

    def fresh(self, now: int) -> bool:
        return self.fresh_until >= now


def digest_text(value: str) -> str:
    """Digest of a header value that may hold any text (a forged token)."""
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


_COLUMNS = "digest, administrator_id, csrf_digest, created_at, last_activity_at, fresh_until"
_SELECT = f"SELECT {_COLUMNS} FROM admin_session"  # noqa: S608 (constants)


def open_session(conn: sqlite3.Connection, administrator_id: str, now: int) -> tuple[str, str]:
    """Creates a session; returns the cookie value and its CSRF token. In a transaction."""
    value = random_value()
    token = csrf_token(value)
    conn.execute(
        f"INSERT INTO admin_session ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, 0)",  # noqa: S608 (constant column list)
        (digest(value), administrator_id, digest_text(token), now, now),
    )
    return value, token


def resolve(conn: sqlite3.Connection, value: str, now: int, *, activity: bool) -> Session | None:
    """The live session of a cookie value, or None; an expired row is deleted on sight.

    `activity`: the request was caused by the operator (`X-Hullgate-Activity: 1`), which alone
    extends the idle expiry; a background poll does not (BR-204).
    """
    if not value or len(value) > 128 or not value.isascii():
        return None
    row = conn.execute(_SELECT + " WHERE digest = ?", (digest(value),)).fetchone()
    if row is None:
        return None
    session = Session(*row)
    if now >= session.expires_at():
        conn.execute("DELETE FROM admin_session WHERE digest = ?", (session.digest,))
        return None
    if activity and now > session.last_activity_at:
        conn.execute(
            "UPDATE admin_session SET last_activity_at = ? WHERE digest = ?",
            (now, session.digest),
        )
        session = Session(
            session.digest,
            session.administrator_id,
            session.csrf_digest,
            session.created_at,
            now,
            session.fresh_until,
        )
    return session


def close(conn: sqlite3.Connection, session_digest: str) -> None:
    conn.execute("DELETE FROM admin_session WHERE digest = ?", (session_digest,))


def mark_fresh(conn: sqlite3.Connection, session_digest: str, now: int) -> int:
    """Writes `fresh_until` (BR-210); value and CSRF token do not change."""
    until = now + FRESH_S
    conn.execute(
        "UPDATE admin_session SET fresh_until = ? WHERE digest = ?", (until, session_digest)
    )
    return until


def purge(conn: sqlite3.Connection, now: int) -> None:
    conn.execute(
        "DELETE FROM admin_session WHERE last_activity_at + ? <= ? OR created_at + ? <= ?",
        (IDLE_S, now, ABSOLUTE_S, now),
    )
