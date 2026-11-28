"""WebAuthn ceremonies of the console (BR-202), on py_webauthn.

A session opens only on an assertion with user verification, for a registered credential of an
active administrator, from the origin of `ADMIN_URL`, under the RP id `ADMIN_RP_ID`, on a
single-use server challenge of at most 5 minutes. Discoverable credentials: nothing is typed before
the ceremony. Attestation is not requested. No password, no TOTP, no local-address exemption.

Challenges live in `webauthn_challenge`, at most 5 live per client address (BR-207); a challenge is
found back from the `clientDataJSON` the browser signs, and deleted when taken.
"""

from __future__ import annotations

import base64
import json
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Any

from webauthn import verify_authentication_response, verify_registration_response
from webauthn.helpers.exceptions import WebAuthnException

from hullgate.admin import accounts
from hullgate.config import AdminOrigin
from hullgate.limits import limit_key

CHALLENGE_TTL_S = 5 * 60
LIVE_CHALLENGES = 5
RP_NAME = "hullgate"
# ES256, EdDSA, RS256: what platform and roaming authenticators offer.
ALGORITHMS = (-7, -8, -257)
TIMEOUT_MS = CHALLENGE_TTL_S * 1000


class CeremonyFailed(Exception):
    """The ceremony did not prove what it must; `reason` is for the audit, never the caller."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class TooManyChallenges(Exception):
    """The client address already holds 5 live challenges (BR-207)."""


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True, slots=True)
class Challenge:
    value: str
    purpose: str
    administrator_id: str | None
    enrolment_digest: str | None
    address: str
    expires_at: int


def new_challenge(
    conn: sqlite3.Connection,
    purpose: str,
    address: str,
    now: int,
    *,
    administrator_id: str | None = None,
    enrolment_digest: str | None = None,
) -> str:
    """Stores a fresh challenge; `TooManyChallenges` past 5 live ones for `address`, counted by
    its limit key (BR-207 v2, BR-301), which the row stores."""
    address = limit_key(address)
    live = conn.execute(
        "SELECT count(*) FROM webauthn_challenge WHERE address = ? AND expires_at > ?",
        (address, now),
    ).fetchone()
    if int(live[0]) >= LIVE_CHALLENGES:
        raise TooManyChallenges
    value = b64url(secrets.token_bytes(32))
    conn.execute(
        "INSERT INTO webauthn_challenge"
        " (challenge, purpose, administrator_id, enrolment_digest, address, expires_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (value, purpose, administrator_id, enrolment_digest, address, now + CHALLENGE_TTL_S),
    )
    return value


def client_challenge(credential: dict[str, Any]) -> str:
    """The challenge a credential response carries in its `clientDataJSON`, or ''."""
    try:
        response = credential["response"]
        data = json.loads(unb64url(str(response["clientDataJSON"])))
        value = data["challenge"]
        if not isinstance(value, str) or len(value) >= 128:
            return ""
        value.encode("utf-8")  # Reject lone surrogates before SQLite binds the challenge.
    except (KeyError, TypeError, ValueError, RecursionError):
        return ""
    return value


def take_challenge(
    conn: sqlite3.Connection, credential: dict[str, Any], purpose: str, now: int
) -> Challenge | None:
    """Consumes the challenge the credential answers: single use, live, of that purpose."""
    value = client_challenge(credential)
    if not value:
        return None
    row = conn.execute(
        "SELECT challenge, purpose, administrator_id, enrolment_digest, address, expires_at"
        " FROM webauthn_challenge WHERE challenge = ?",
        (value,),
    ).fetchone()
    if row is None:
        return None
    conn.execute("DELETE FROM webauthn_challenge WHERE challenge = ?", (value,))
    challenge = Challenge(*row)
    if challenge.purpose != purpose or challenge.expires_at <= now:
        return None
    return challenge


# -- options sent to the browser (JSON form of PublicKeyCredential*Options) -----------------------


def registration_options(
    origin: AdminOrigin,
    admin: accounts.Administrator,
    challenge: str,
    exclude: list[str],
) -> dict[str, Any]:
    return {
        "challenge": challenge,
        "rp": {"id": origin.rp_id, "name": RP_NAME},
        "user": {
            "id": b64url(admin.id.encode("ascii")),
            "name": admin.email,
            "displayName": admin.name,
        },
        "pubKeyCredParams": [{"type": "public-key", "alg": alg} for alg in ALGORITHMS],
        "timeout": TIMEOUT_MS,
        "attestation": "none",
        "authenticatorSelection": {
            "residentKey": "required",
            "requireResidentKey": True,
            "userVerification": "required",
        },
        "excludeCredentials": [{"type": "public-key", "id": c} for c in exclude],
    }


def authentication_options(
    origin: AdminOrigin, challenge: str, allow: list[str] | None = None
) -> dict[str, Any]:
    return {
        "challenge": challenge,
        "rpId": origin.rp_id,
        "timeout": TIMEOUT_MS,
        "userVerification": "required",
        "allowCredentials": [{"type": "public-key", "id": c} for c in allow or []],
    }


# -- verification ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NewCredential:
    credential_id: str
    public_key: bytes
    sign_count: int


def verify_registration(
    credential: dict[str, Any], challenge: Challenge, origin: AdminOrigin
) -> NewCredential:
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=unb64url(challenge.value),
            expected_rp_id=origin.rp_id,
            expected_origin=origin.origin,
            require_user_presence=True,
            require_user_verification=True,
        )
    except (WebAuthnException, KeyError, TypeError, ValueError, RecursionError) as exc:
        raise CeremonyFailed("invalid_registration") from exc
    return NewCredential(b64url(verified.credential_id), verified.credential_public_key,
                         verified.sign_count)  # fmt: skip


def verify_assertion(
    conn: sqlite3.Connection,
    credential: dict[str, Any],
    challenge: Challenge,
    origin: AdminOrigin,
    now: int,
) -> tuple[accounts.Administrator, accounts.Passkey]:
    """Checks an assertion against the stored credential; updates its counter. In a transaction.

    `CeremonyFailed` reasons: `unknown_credential`, `inactive_administrator`, `counter_regressed`,
    `invalid_assertion` — one and the same answer for the caller (BR-202, BR-207).
    """
    raw_id = credential.get("id") if isinstance(credential, dict) else None
    if not isinstance(raw_id, str):
        raise CeremonyFailed("invalid_assertion")
    try:
        raw_id.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CeremonyFailed("invalid_assertion") from exc
    key = accounts.passkey(conn, raw_id)
    if key is None:
        raise CeremonyFailed("unknown_credential")
    admin = accounts.get(conn, key.administrator_id)
    if admin is None or admin.state != "active":
        raise CeremonyFailed("inactive_administrator")
    if challenge.administrator_id is not None and challenge.administrator_id != admin.id:
        raise CeremonyFailed("unknown_credential")
    try:
        # The counter is judged below, on verified data only (N2): the library's own check would
        # run before the signature, on whatever `authenticatorData` the caller sent.
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=unb64url(challenge.value),
            expected_rp_id=origin.rp_id,
            expected_origin=origin.origin,
            credential_public_key=key.public_key,
            credential_current_sign_count=0,
            require_user_verification=True,
        )
    except (WebAuthnException, KeyError, TypeError, ValueError, RecursionError) as exc:
        raise CeremonyFailed("invalid_assertion") from exc
    reported = verified.new_sign_count
    if (reported > 0 or key.sign_count > 0) and reported <= key.sign_count:
        raise CeremonyFailed("counter_regressed")
    conn.execute(
        "UPDATE passkey SET sign_count = ?, last_used_at = ? WHERE credential_id = ?",
        (verified.new_sign_count, now, key.credential_id),
    )
    return admin, key
