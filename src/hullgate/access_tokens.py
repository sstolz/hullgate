"""OAuth access tokens: RFC 9068 JWTs signed ES256 (BR-054), register of the `jti` (BR-059, BR-072).

`issue` issues a token in the caller's transaction (BR-065); `JwtResolver` is the resolver of
step 4 of the authorization order (BR-055) for the tokens that are not operator tokens:
signature, header, required claims, single audience, issuer of the audience's access point, then
state of the `jti` and of its lineage in the database, without cache.
"""

from __future__ import annotations

import secrets
import sqlite3
import time
from typing import Any

import jwt

from hullgate import routes, tokens
from hullgate.authorization import Credential
from hullgate.keys import ALGORITHM, SigningKey
from hullgate.tree import Clock

ACCESS_TTL = 3600  # BR-052: an OAuth access token lives 1 hour
IAT_SKEW = 60  # BR-054: `iat` more than 60 s in the future, refused
TYP = "at+jwt"
REQUIRED = ("iss", "sub", "aud", "exp", "iat", "jti", "client_id", "scope")


class KeyUnavailable(Exception):
    """Signing key not loaded at issuing time: nothing is issued (BR-064)."""


def issuer_for(public_url: str, org_id: str, path: str) -> str:
    return routes.issuer_url(public_url, org_id, path)


def resource_for(public_url: str, org_id: str, path: str) -> str:
    return routes.resource_url(public_url, org_id, path)


def issue(
    conn: sqlite3.Connection,
    key: SigningKey,
    *,
    issuer: str,
    audience: str,
    role_id: str,
    client_id: str,
    scope: str,
    lineage_id: str,
    now: int,
) -> tuple[str, str]:
    """Returns (token, jti); the `jti` is recorded in the current transaction, never the token."""
    private = key.private()
    if private is None:
        raise KeyUnavailable("signing key not loaded")
    jti = secrets.token_hex(16)
    claims = {
        "iss": issuer,
        "sub": role_id,
        "aud": audience,
        "exp": now + ACCESS_TTL,
        "iat": now,
        "jti": jti,
        "client_id": client_id,
        "scope": scope,
    }
    token = jwt.encode(claims, private, algorithm=ALGORITHM, headers={"typ": TYP})
    conn.execute(
        "INSERT INTO access_jti (jti, lineage_id, expires_at) VALUES (?, ?, ?)",
        (jti, lineage_id, now + ACCESS_TTL),
    )
    return token, jti


def verify(key: SigningKey, token: str, public_url: str, now: float) -> dict[str, Any] | None:
    """Claims of an authentic and well-formed access token (without the database state), or None.

    Expiry is not judged here: step 4 reads it in `Credential.expires_at`.
    """
    public = key.public()
    if public is None or token.count(".") != 2:
        return None
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        return None
    if header.get("alg") != ALGORITHM or str(header.get("typ", "")).lower() != TYP:
        return None
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            public,
            algorithms=[ALGORITHM],  # no `alg` read from the token is trusted
            options={
                "verify_aud": False,
                "verify_iss": False,
                "verify_exp": False,
                "verify_iat": False,
                "verify_nbf": False,
                "require": list(REQUIRED),
            },
        )
    except jwt.PyJWTError:
        return None
    aud, iss = claims.get("aud"), claims.get("iss")
    if not isinstance(aud, str):  # a list, even of a single element, is refused (BR-054)
        return None
    for name in ("iss", "sub", "jti", "client_id", "scope"):
        if not isinstance(claims.get(name), str) or not claims[name]:
            return None
    for name in ("exp", "iat"):
        if not isinstance(claims.get(name), int) or isinstance(claims[name], bool):
            return None
    if claims["iat"] > now + IAT_SKEW:
        return None
    # the audience is an access point's URL, the issuer its own server (BR-054, BR-056 v2); which
    # access point, and whether its identifier is the instance's, step 6 decides (BR-055)
    point = routes.parse_resource(public_url, aud)
    if point is None or iss != issuer_for(public_url, *point):
        return None
    return claims


class JwtResolver:
    """Step 4 resolver for OAuth JWTs; None for any token not recognised."""

    def __init__(self, key: SigningKey, public_url: str, clock: Clock = time.time) -> None:
        self.key = key
        self.public_url = public_url
        self.clock = clock

    def __call__(self, conn: sqlite3.Connection, bearer: str) -> Credential | None:
        if bearer.startswith(tokens.PREFIX):
            return None
        claims = verify(self.key, bearer, self.public_url, self.clock())
        if claims is None:
            return None
        row = conn.execute(
            "SELECT a.revoked, l.revoked, l.role_id, l.client_id, n.deleted, l.id"
            " FROM access_jti a JOIN lineage l ON l.id = a.lineage_id"
            " JOIN node n ON n.id = l.role_id WHERE a.jti = ?",
            (claims["jti"],),
        ).fetchone()
        if row is None:
            return None  # `jti` unknown to the register: token never issued by this gateway
        jti_revoked, lineage_revoked, role_id, client_id, deleted, lineage_id = row
        if role_id != claims["sub"] or client_id != claims["client_id"]:
            return None
        return Credential(
            kind="oauth",
            id=claims["jti"],
            actor=client_id,
            node_id=role_id,
            expires_at=float(claims["exp"]),
            revoked=bool(jti_revoked or lineage_revoked),
            node_deleted=bool(deleted),
            scopes=frozenset(claims["scope"].split()),
            audience=claims["aud"],
            token_key=f"lineage:{lineage_id}",
        )
