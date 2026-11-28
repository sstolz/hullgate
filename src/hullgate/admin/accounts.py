"""Administrators, enrolment links, passkeys, the IP allow-list and sign-in attempts (F-016 lot A).

Domain operations shared by the host CLI (`hullgate admin …`) and the admin API: one rule set
(BR-211). Every write runs in the transaction of the act it audits (BR-065); no entry carries an
enrolment link, a session value or their full digests (BR-066 v5).

- BR-200: an administrator has an e-mail, a display name and a state, `pending` or `active`.
- BR-201: the first administrator comes from the host, `admin bootstrap`, and only from it.
- BR-203: an enrolment link carries 256 random bits, is kept as its SHA-256 digest, is bound to
  one account, expires after 30 minutes, is consumed once and replaced by any newer link.
- BR-207: 5 failed attempts per client address in a sliding 15 minutes close sign-in and
  enrolment to that address; `admin attempts clear` reopens them.
- BR-209, BR-235: the allow-list, its self-exclusion refusal and `admin allowlist clear`.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import re
import secrets
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from hullgate import audit
from hullgate.admin.client_address import in_networks
from hullgate.config import Network
from hullgate.db import transaction
from hullgate.limits import limit_key
from hullgate.tree import Refused

STATES = ("pending", "active")
ENROLMENT_TTL_S = 30 * 60
ATTEMPT_WINDOW_S = 15 * 60
ATTEMPT_LIMIT = 5
LINK_PREFIX = "ENROLMENT-LINK: "
ENROL_PATH = "/admin/enrol/"
LABEL_MAX = 64
NAME_MAX = 128
ALLOWLIST_MAX = 64

_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}$")


def digest(value: str) -> str:
    """SHA-256 hex digest under which a link or a session value is stored."""
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def random_value() -> str:
    """256 random bits, base64url without padding (43 characters)."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")


def actor(administrator_id: str) -> str:
    """Audit actor of an act from the console (BR-066 v5, BR-211)."""
    return f"admin:{administrator_id}"


@dataclass(frozen=True, slots=True)
class Administrator:
    id: str
    email: str
    name: str
    state: str
    created_at: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "state": self.state,
            "created_at": audit.utc(self.created_at),
        }


@dataclass(frozen=True, slots=True)
class Passkey:
    credential_id: str
    administrator_id: str
    public_key: bytes
    sign_count: int
    label: str
    created_at: int
    last_used_at: int | None

    def as_dict(self) -> dict[str, Any]:
        """The API's view: never the public key (it is no secret, but it is no one's business)."""
        return {
            "id": self.credential_id,
            "label": self.label,
            "created_at": audit.utc(self.created_at),
            "last_used_at": None if self.last_used_at is None else audit.utc(self.last_used_at),
        }


_ADMIN_COLUMNS = "id, email, name, state, created_at"
_PASSKEY_COLUMNS = (
    "credential_id, administrator_id, public_key, sign_count, label, created_at, last_used_at"
)


_SELECT_ADMIN = f"SELECT {_ADMIN_COLUMNS} FROM administrator"  # noqa: S608 (constants)
_SELECT_PASSKEY = f"SELECT {_PASSKEY_COLUMNS} FROM passkey"  # noqa: S608 (constants)


def _admin(row: Sequence[Any] | None) -> Administrator | None:
    return None if row is None else Administrator(*row)


def get(conn: sqlite3.Connection, administrator_id: str) -> Administrator | None:
    return _admin(conn.execute(_SELECT_ADMIN + " WHERE id = ?", (administrator_id,)).fetchone())


def list_administrators(conn: sqlite3.Connection) -> list[Administrator]:
    rows = conn.execute(_SELECT_ADMIN + " ORDER BY created_at, id")
    return [Administrator(*row) for row in rows]


def _check_email(email: str) -> str:
    value = email.strip().lower()
    if not _EMAIL.match(value):
        raise Refused("e-mail address expected (name@domain)")
    return value


def _check_text(value: str, what: str, limit: int) -> str:
    text = value.strip()
    if not text or len(text) > limit or any(ord(c) < 32 for c in text):
        raise Refused(f"{what}: 1 to {limit} printable characters expected")
    return text


def link_url(admin_origin: str, token: str) -> str:
    """The enrolment link: the token is a path segment, never a query string (BR-203)."""
    return f"{admin_origin}{ENROL_PATH}{token}"


def _issue_link(
    conn: sqlite3.Connection, admin: Administrator, kind: str, actor_name: str, now: int
) -> str:
    """Inserts a new link and replaces every earlier unused one of the account (BR-203)."""
    replaced = conn.execute(
        "DELETE FROM enrolment WHERE administrator_id = ? AND used_at IS NULL", (admin.id,)
    ).rowcount
    token = random_value()
    conn.execute(
        "INSERT INTO enrolment (digest, administrator_id, kind, created_at, expires_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (digest(token), admin.id, kind, now, now + ENROLMENT_TTL_S),
    )
    audit.write(
        conn,
        actor=actor_name,
        action="admin.enrolment.issue",
        result="ok",
        detail={"administrator": admin.id, "kind": kind, "replaced": replaced},
        now=now,
    )
    return token


def bootstrap(
    conn: sqlite3.Connection, email: str, name: str | None, now: int
) -> tuple[Administrator, str]:
    """BR-201: creates the first administrator, or reissues its link while it is `pending`.

    Refused when an active administrator exists. With a `pending` account: the same e-mail
    reissues the link (the old one is replaced); another e-mail replaces the account, so that a
    typo at bootstrap has a way out. Returns the account and the link's token, to print once.
    """
    address = _check_email(email)
    display = _check_text(name if name else address.split("@", 1)[0], "name", NAME_MAX)
    with transaction(conn):
        if conn.execute("SELECT 1 FROM administrator WHERE state = 'active'").fetchone():
            raise Refused("an active administrator exists: bootstrap creates the first one only")
        pending = list_administrators(conn)
        same = next((a for a in pending if a.email == address), None)
        for other in pending:
            if other is same:
                continue
            conn.execute("DELETE FROM enrolment WHERE administrator_id = ?", (other.id,))
            conn.execute("DELETE FROM webauthn_challenge WHERE administrator_id = ?", (other.id,))
            conn.execute("DELETE FROM administrator WHERE id = ?", (other.id,))
            audit.write(
                conn,
                actor=audit.OPERATOR_CLI,
                action="admin.remove",
                result="ok",
                reason="replaced_at_bootstrap",
                before=other.as_dict(),
                now=now,
            )
        if same is None:
            same = Administrator(
                id="adm_" + secrets.token_hex(6),
                email=address,
                name=display,
                state="pending",
                created_at=now,
            )
            conn.execute(
                "INSERT INTO administrator (id, email, name, state, created_at)"
                " VALUES (?, ?, ?, 'pending', ?)",
                (same.id, same.email, same.name, now),
            )
            audit.write(
                conn,
                actor=audit.OPERATOR_CLI,
                action="admin.create",
                result="ok",
                after=same.as_dict(),
                now=now,
            )
        token = _issue_link(conn, same, "bootstrap", audit.OPERATOR_CLI, now)
    return same, token


def recover(conn: sqlite3.Connection, email: str, now: int) -> tuple[Administrator, str]:
    """Host-only recovery: kill every credential/session before issuing the replacement link."""
    address = _check_email(email)
    with transaction(conn):
        admin = next((a for a in list_administrators(conn) if a.email == address), None)
        if admin is None:
            raise Refused("unknown administrator")
        if admin.state != "active":
            raise Refused("pending administrator: use admin bootstrap")
        keys = conn.execute("DELETE FROM passkey WHERE administrator_id = ?", (admin.id,)).rowcount
        sessions = conn.execute(
            "DELETE FROM admin_session WHERE administrator_id = ?", (admin.id,)
        ).rowcount
        conn.execute("DELETE FROM webauthn_challenge WHERE administrator_id = ?", (admin.id,))
        audit.write(
            conn,
            actor=audit.OPERATOR_CLI,
            action="admin.recover",
            result="ok",
            detail={
                "administrator": admin.id,
                "passkeys_revoked": keys,
                "sessions_ended": sessions,
            },
            now=now,
        )
        token = _issue_link(conn, admin, "recovery", audit.OPERATOR_CLI, now)
    return admin, token


@dataclass(frozen=True, slots=True)
class Link:
    digest: str
    administrator: Administrator
    kind: str


def find_link(conn: sqlite3.Connection, token: str, now: int) -> Link | None:
    """A usable link: known, unused, unexpired, of an existing account. Read only."""
    if not token or len(token) > 128 or not token.isascii():
        return None
    row = conn.execute(
        "SELECT digest, administrator_id, kind FROM enrolment"
        " WHERE digest = ? AND used_at IS NULL AND expires_at > ?",
        (digest(token), now),
    ).fetchone()
    if row is None:
        return None
    admin = get(conn, row[1])
    return None if admin is None else Link(row[0], admin, row[2])


def consume_link(conn: sqlite3.Connection, link: Link, now: int) -> bool:
    """Marks the link used; False when another request consumed it first. In a transaction."""
    changed = conn.execute(
        "UPDATE enrolment SET used_at = ? WHERE digest = ? AND used_at IS NULL AND expires_at > ?",
        (now, link.digest, now),
    ).rowcount
    return changed == 1


def activate(conn: sqlite3.Connection, admin: Administrator, now: int) -> None:
    """`pending` → `active` at the first enrolment (6.4). In a transaction."""
    if admin.state == "active":
        return
    conn.execute("UPDATE administrator SET state = 'active' WHERE id = ?", (admin.id,))
    audit.write(
        conn,
        actor=actor(admin.id),
        action="admin.activate",
        result="ok",
        before={"state": admin.state},
        after={"state": "active"},
        now=now,
    )


# -- passkeys --------------------------------------------------------------------------------------


def passkeys(conn: sqlite3.Connection, administrator_id: str) -> list[Passkey]:
    rows = conn.execute(
        _SELECT_PASSKEY + " WHERE administrator_id = ? ORDER BY created_at, credential_id",
        (administrator_id,),
    )
    return [Passkey(*row) for row in rows]


def passkey(conn: sqlite3.Connection, credential_id: str) -> Passkey | None:
    row = conn.execute(_SELECT_PASSKEY + " WHERE credential_id = ?", (credential_id,)).fetchone()
    return None if row is None else Passkey(*row)


def check_label(label: str) -> str:
    return _check_text(label, "passkey label", LABEL_MAX)


def add_passkey(
    conn: sqlite3.Connection,
    admin: Administrator,
    credential_id: str,
    public_key: bytes,
    sign_count: int,
    label: str,
    now: int,
) -> Passkey:
    """Registers a credential for `admin`; refused when the credential is already known."""
    if passkey(conn, credential_id) is not None:
        raise Refused("this passkey is already registered")
    conn.execute(
        "INSERT INTO passkey"
        " (credential_id, administrator_id, public_key, sign_count, label, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (credential_id, admin.id, public_key, sign_count, label, now),
    )
    audit.write(
        conn,
        actor=actor(admin.id),
        action="admin.passkey.add",
        result="ok",
        after={"id": credential_id, "label": label},
        now=now,
    )
    return Passkey(credential_id, admin.id, public_key, sign_count, label, now, None)


class LastPasskey(Refused):
    """Deleting the account's last passkey would leave it with no way back but the host."""


def delete_passkey(
    conn: sqlite3.Connection,
    admin: Administrator,
    credential_id: str,
    keep_session_digest: str,
    now: int,
) -> int | None:
    """Deletes one of `admin`'s passkeys and ends its other sessions (BR-204, BR-210).

    None when the passkey is not the account's; `LastPasskey` for the last one. Returns the
    number of sessions ended. In a transaction.
    """
    own = passkeys(conn, admin.id)
    target = next((p for p in own if p.credential_id == credential_id), None)
    if target is None:
        return None
    if len(own) == 1:
        raise LastPasskey("the last passkey of the account cannot be deleted")
    conn.execute("DELETE FROM passkey WHERE credential_id = ?", (credential_id,))
    ended = conn.execute(
        "DELETE FROM admin_session WHERE administrator_id = ? AND digest != ?",
        (admin.id, keep_session_digest),
    ).rowcount
    audit.write(
        conn,
        actor=actor(admin.id),
        action="admin.passkey.delete",
        result="ok",
        before={"id": credential_id, "label": target.label},
        detail={"sessions_ended": ended},
        now=now,
    )
    return ended


# -- sign-in attempts (BR-207) ---------------------------------------------------------------------


def limited(conn: sqlite3.Connection, address: str, now: int) -> bool:
    """True while `address`, counted by its limit key (BR-207 v2, BR-301), counts 5 failures or
    more in the last 15 minutes."""
    row = conn.execute(
        "SELECT count(*) FROM admin_attempt WHERE address = ? AND at > ?",
        (limit_key(address), now - ATTEMPT_WINDOW_S),
    ).fetchone()
    return int(row[0]) >= ATTEMPT_LIMIT


def record_failure(conn: sqlite3.Connection, address: str, now: int) -> None:
    conn.execute("INSERT INTO admin_attempt (address, at) VALUES (?, ?)", (limit_key(address), now))


def purge(conn: sqlite3.Connection, now: int) -> None:
    """Expired challenges, attempts out of the window, links and sessions past their life."""
    conn.execute("DELETE FROM webauthn_challenge WHERE expires_at <= ?", (now,))
    conn.execute("DELETE FROM admin_attempt WHERE at <= ?", (now - ATTEMPT_WINDOW_S,))


def clear_attempts(conn: sqlite3.Connection, now: int) -> int:
    """`hullgate admin attempts clear`: empties every window from the host (BR-207)."""
    with transaction(conn):
        cleared = conn.execute("DELETE FROM admin_attempt").rowcount
        audit.write(
            conn,
            actor=audit.OPERATOR_CLI,
            action="admin.attempts.clear",
            result="ok",
            detail={"cleared": cleared},
            now=now,
        )
    return cleared


# -- IP allow-list (BR-209, BR-235) ----------------------------------------------------------------


def allowlist(conn: sqlite3.Connection) -> list[str]:
    return [row[0] for row in conn.execute("SELECT cidr FROM admin_allowlist ORDER BY cidr")]


def networks(cidrs: Sequence[str]) -> list[Network]:
    return [ipaddress.ip_network(cidr, strict=False) for cidr in cidrs]


def normalise_allowlist(values: Sequence[str]) -> list[str]:
    """Canonical CIDRs, sorted and deduplicated; `Refused` on a value that is not one."""
    if len(values) > ALLOWLIST_MAX:
        raise Refused(f"at most {ALLOWLIST_MAX} entries")
    out: set[str] = set()
    for value in values:
        try:
            out.add(str(ipaddress.ip_network(str(value).strip(), strict=False)))
        except ValueError as exc:
            raise Refused("CIDR expected (203.0.113.0/24, 2001:db8::/32 or one address)") from exc
    return sorted(out)


class AllowlistSelf(Refused):
    """The update would exclude the caller's own address (BR-235)."""

    def __init__(self, address: str) -> None:
        super().__init__(f"this list would exclude your own address {address}")
        self.address = address


def set_allowlist(
    conn: sqlite3.Connection, cidrs: Sequence[str], caller: str, actor_name: str, now: int
) -> list[str]:
    """Replaces the list; refused when non-empty and excluding `caller`. In a transaction."""
    after = normalise_allowlist(cidrs)
    if after and not in_networks(caller, networks(after)):
        raise AllowlistSelf(caller)
    before = allowlist(conn)
    if before == after:
        return after
    conn.execute("DELETE FROM admin_allowlist")
    conn.executemany("INSERT INTO admin_allowlist (cidr) VALUES (?)", [(c,) for c in after])
    audit.write(
        conn,
        actor=actor_name,
        action="admin.allowlist",
        result="ok",
        before={"cidrs": before},
        after={"cidrs": after},
        now=now,
    )
    return after


def clear_allowlist(conn: sqlite3.Connection, now: int) -> list[str]:
    """`hullgate admin allowlist clear` (BR-235): the host can always reopen the console."""
    with transaction(conn):
        before = allowlist(conn)
        if before:
            conn.execute("DELETE FROM admin_allowlist")
            audit.write(
                conn,
                actor=audit.OPERATOR_CLI,
                action="admin.allowlist",
                result="ok",
                before={"cidrs": before},
                after={"cidrs": []},
                now=now,
            )
    return before
