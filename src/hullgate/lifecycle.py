"""Connection lifecycle (F-006 lot B): authorization, renewal under a lease, revocation,
removal, refusal signal, dynamic registration.

**Renewal lease** (BR-089). The SQLite write lock is global to the database: no transaction is
held during a call to the provider. A short `BEGIN IMMEDIATE` transaction rereads the state and
places the lease (`renewing_until`, `renewal_owner`); the call is made outside any transaction; a
second short transaction writes the result only if the caller still holds the lease and
`generation` is unchanged (BR-090). A caller that finds a live lease rereads the state by polling
(250 ms) until the end of the lease + 1 s. `revoke`, `remove` and the write of an `authorize`
wait for the end of a live lease before writing, and increment `generation`: a renewal result
arriving afterwards is discarded and its tokens revoked on a best-effort basis.

**Runner**: each short transaction goes through a `Runner` — `Database.call` on the server side
(the process lock is held only for the time of a transaction, never during network I/O), or
`Direct(conn)` for the CLI, which has its own connection.

No value of a token, a secret, a code or a callback URL enters an output, an audit entry, a
message or a representation (BR-066 v2, BR-086).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import math
import secrets
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from hullgate import audit, upstream
from hullgate.cipher import ConnectionsKey, IntegrityFailure, KeyUnavailable, Sealer
from hullgate.connections import (
    _ROW_COLUMNS,
    A_CONN_REMOVE,
    A_CONN_REVOKE_UPSTREAM,
    A_INTEGRITY,
    A_KEY_UNAVAILABLE,
    EXPIRY_MARGIN,
    MCP_REFUSAL_REASON,
    REDIRECT_URI,
    Access,
    AccessError,
    IntegrityErrorAccess,
    KeyUnavailableError,
    NotAuthorized,
    NotPlaced,
    ReauthRequired,
    Row,
    Service,
    TemporarilyUnavailable,
    _applications,
    _find,
    _placements,
    _row,
    _service,
    display_name,
    erase_policy,
    internal_of,
    state_hash,
)
from hullgate.db import Database, StoreUnavailable, provider_outcome, transaction
from hullgate.outbound import ANOMALY, InternalHost, Outbound
from hullgate.tree import Clock, Refused, Tree, validate_segment

LEASE_S = 12.0  # hard deadline of BR-084 (10 s) + 2 s
POLL_S = 0.25
BACKOFF_MAX_S = 900  # BR-094 : 15 min
EXPIRY_DEFAULT = upstream.DEFAULT_EXPIRY

A_AUTHORIZE = "connection.authorize"
A_AUTHORIZE_REFUSED = "connection.authorize_refused"
A_RENEW = "connection.renew"
A_REVOKE = "connection.revoke"
A_SIGNAL = "connection.refusal_signal"
A_APP_REGISTER = "application.register"
A_APP_REREGISTER = "application.reregister"

GATEWAY = "gateway"
MCP_401 = "mcp_401"  # cause of `signal_refusal` set by the relay (F-008, BR-127)
SIGNAL_MCP_REASON = MCP_REFUSAL_REASON


class Unavailable(Exception):
    """Provider unavailable for a CLI command (code 1, nothing written); `reason` is a short
    reason from the table of BR-092."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Runner(Protocol):
    def __call__[T](self, fn: Callable[[sqlite3.Connection], T], /) -> T: ...


class Direct:
    """Runner on a connection of the caller's own (CLI, instrument, tests)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __call__[T](self, fn: Callable[[sqlite3.Connection], T], /) -> T:
        try:
            return fn(self.conn)
        except sqlite3.Error as exc:
            raise StoreUnavailable(str(exc)) from exc


Sleep = Callable[[float], None]
# A check run inside the act's own transaction; it refuses by raising (BR-214, admin API).
Precondition = Callable[[sqlite3.Connection], None]


# ---------------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Lease:
    renewing_until: int | None
    owner: str | None
    fail_count: int
    removed: bool


def _live(conn: sqlite3.Connection, connection_id: str) -> Row | None:
    found = conn.execute(
        f"SELECT {_ROW_COLUMNS} FROM connection WHERE removed = 0 AND id = ?",  # noqa: S608
        (connection_id,),
    ).fetchone()
    return None if found is None else _row(found)


def _lease(conn: sqlite3.Connection, connection_id: str) -> _Lease:
    r = conn.execute(
        "SELECT renewing_until, renewal_owner, fail_count, removed FROM connection WHERE id = ?",
        (connection_id,),
    ).fetchone()
    return _Lease(r[0], r[1], int(r[2]), bool(r[3]))


def _tokens(conn: sqlite3.Connection, sealer: Sealer, row_id: str) -> dict[str, Any] | None:
    """Decrypted token set, or None if there is none; `IntegrityFailure` otherwise."""
    blob = conn.execute("SELECT tokens_enc FROM connection WHERE id = ?", (row_id,)).fetchone()[0]
    if blob is None:
        return None
    try:
        data = json.loads(sealer.open(blob, "connection", row_id, "tokens"))
    except ValueError as exc:
        raise IntegrityFailure("unreadable token set") from exc
    if not isinstance(data, dict) or not isinstance(data.get("access_token"), str):
        raise IntegrityFailure("unreadable token set")
    return data


def _seal_tokens(
    conn: sqlite3.Connection, sealer: Sealer, row_id: str, access: str, refresh: str | None
) -> bytes:
    data: dict[str, str] = {"access_token": access, "token_type": "Bearer"}
    if refresh is not None:
        data["refresh_token"] = refresh
    return sealer.seal(conn, "connection", row_id, "tokens", json.dumps(data))


def _client(conn: sqlite3.Connection, sealer: Sealer | None, app_id: str) -> upstream.Client:
    r = conn.execute(
        "SELECT client_id, secret_enc, auth_method FROM application WHERE id = ?", (app_id,)
    ).fetchone()
    if r is None:
        raise Refused(f"unknown application: {app_id}")
    secret = None
    if r[1] is not None and sealer is not None:
        secret = sealer.open(r[1], "application", app_id, "secret")
    return upstream.Client(r[0], secret, r[2] if secret is not None else "none")


@dataclass(frozen=True, slots=True, repr=False)
class _Upstream:
    """What a call to the provider of an OAuth connection needs (never displayed)."""

    service: Service
    client: upstream.Client | None
    internal: InternalHost | None

    @property
    def resource(self) -> str | None:
        return self.service.resource if self.service.model == "generic" else None

    def endpoint(self, name: str) -> str | None:
        return self.service.endpoints.get(name)


def _upstream_of(conn: sqlite3.Connection, sealer: Sealer | None, row: Row) -> _Upstream:
    service = _service(conn, row.service_id)
    client = None
    if row.application_id is not None:
        with contextlib.suppress(IntegrityFailure):
            client = _client(conn, sealer, row.application_id)
    return _Upstream(service, client, internal_of(service))


def _audit(conn: sqlite3.Connection, action: str, result: str, path: str, **kw: Any) -> None:
    audit.write(
        conn,
        actor=kw.pop("actor", None) or audit.actor(),
        action=action,
        result=result,
        path=path,
        **kw,
    )


def _backoff(fail_count: int, retry_after: float | None) -> int:
    """BR-094: a valid `Retry-After` capped at 15 min, otherwise 1 s doubling up to 15 min."""
    if retry_after is not None and retry_after > 0:
        return math.ceil(min(retry_after, BACKOFF_MAX_S))
    return int(min(2 ** min(fail_count, 20), BACKOFF_MAX_S))


def _expiry(tokens: upstream.TokenSet, now: float) -> tuple[int, bool]:
    if tokens.expires_in is None:
        return int(now) + EXPIRY_DEFAULT, True
    return int(now) + tokens.expires_in, False


# ---------------------------------------------------------------------------------------------
# Revocation at the provider (best effort)
# ---------------------------------------------------------------------------------------------


def revoke_upstream(out: Outbound, up: _Upstream | None, tokens: dict[str, Any] | None) -> str:
    """Revokes the tokens at the stored `revocation_endpoint` (BR-097); returns the outcome,
    audited by the caller. Never raises."""
    if tokens is None:
        return "no_token"
    if up is None or up.client is None:
        return "unsupported"
    endpoint = up.endpoint("revocation_endpoint")
    if endpoint is None:
        return "unsupported"
    try:
        for name in ("refresh_token", "access_token"):
            value = tokens.get(name)
            if isinstance(value, str) and value:
                upstream.revoke(out, endpoint, up.client, value, name, up.internal)
    except upstream.ProviderError as exc:
        return f"failed ({exc.reason})"
    return "revoked"


def _audit_upstream(
    run: Runner, name: str, outcome: str, clock: Clock, actor: str | None = None
) -> None:
    result = {"revoked": "ok", "unsupported": "skipped", "key_unavailable": "skipped"}

    def write(conn: sqlite3.Connection) -> None:
        with provider_outcome(), transaction(conn):
            _audit(
                conn,
                A_CONN_REVOKE_UPSTREAM,
                result.get(outcome, "failed"),
                name,
                reason=outcome,
                now=clock(),
                actor=actor,
            )

    with provider_outcome(), contextlib.suppress(StoreUnavailable):
        run(write)


def _discard(
    run: Runner, out: Outbound, up: _Upstream, tokens: upstream.TokenSet, name: str, clock: Clock
) -> None:
    """Tokens obtained but never written: revoked on a best-effort basis, outcome audited."""
    data: dict[str, Any] = {"access_token": tokens.access_token}
    if tokens.refresh_token is not None:
        data["refresh_token"] = tokens.refresh_token
    _audit_upstream(run, name, revoke_upstream(out, up, data), clock, GATEWAY)


# ---------------------------------------------------------------------------------------------
# Waiting for a live lease (revoke, remove, write of authorize)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Busy:
    remaining: float


def _act_with[T](
    act: Callable[[sqlite3.Connection, bool], T], deadline: float | None, conn: sqlite3.Connection
) -> T:
    return act(conn, deadline is not None and time.monotonic() >= deadline)


def _when_free[T](
    run: Runner, act: Callable[[sqlite3.Connection, bool], T | _Busy], sleep: Sleep
) -> T:
    """Plays `act` as soon as no live lease holds the connection; at the end of the lease + 1 s
    without release (process killed), `act` is replayed with `force=True` and overrides it."""
    deadline: float | None = None
    while True:
        result = run(functools.partial(_act_with, act, deadline))
        if not isinstance(result, _Busy):
            return result
        if deadline is None:
            deadline = time.monotonic() + max(result.remaining, 0.0) + 1.0
        sleep(POLL_S)


def _lease_live(lease: _Lease, now: float) -> bool:
    return lease.renewing_until is not None and lease.renewing_until > now


# ---------------------------------------------------------------------------------------------
# get_access (§3.3), renewal (BR-089 to BR-094)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, repr=False)
class _Renew:
    row: Row
    name: str
    owner: str
    refresh: str
    up: _Upstream
    lease_until: int


@dataclass(frozen=True, slots=True)
class _Wait:
    generation: int
    remaining: float


type _Step = Access | AccessError | Refused | _Renew | _Wait


def _key_alert(conn: sqlite3.Connection, row: Row, name: str, reason: str, now: float) -> None:
    """BR-066 v2: "key unavailable or different" is audited once per (connection, reason) as
    long as the reason does not change (review 417 m2); the marker lives outside the `connection`
    row (BR-088: no connection row modified without the key)."""
    found = conn.execute(
        "SELECT reason FROM key_alert WHERE connection_id = ?", (row.id,)
    ).fetchone()
    if found is not None and found[0] == reason:
        return
    conn.execute(
        "INSERT INTO key_alert (connection_id, reason, at) VALUES (?, ?, ?)"
        " ON CONFLICT (connection_id) DO UPDATE SET reason = excluded.reason, at = excluded.at",
        (row.id, reason, int(now)),
    )
    _audit(conn, A_KEY_UNAVAILABLE, "refused", name, reason=reason, now=now, actor=GATEWAY)


def _begin(
    conn: sqlite3.Connection,
    key: ConnectionsKey,
    connection_id: str,
    role_id: str | None,
    now: float,
    owner: str,
    force: bool,
) -> _Step:
    with transaction(conn):
        row = _live(conn, connection_id)
        if row is None or (
            role_id is not None
            and not conn.execute(
                "SELECT 1 FROM placement WHERE connection_id = ? AND role_id = ?",
                (connection_id, role_id),
            ).fetchone()
        ):
            return NotPlaced(NotPlaced.code)
        name = display_name(Tree.load(conn).nodes, row)
        try:
            sealer = key.sealer(conn)
        except KeyUnavailable as exc:
            _key_alert(conn, row, name, exc.reason, now)
            return KeyUnavailableError(exc.reason)
        conn.execute("DELETE FROM key_alert WHERE connection_id = ?", (row.id,))
        if force and row.kind == "provided":
            return Refused(f"renew: {name} has a provided token (never renewed, BR-085)")
        if row.state == "pending":
            return (
                Refused(f"{name} is not authorized") if force else NotAuthorized(NotAuthorized.code)
            )
        if row.state == "reauth_required":
            return (
                Refused(f"{name}: reauthentication required")
                if force
                else ReauthRequired(ReauthRequired.code)
            )
        try:
            tokens = _tokens(conn, sealer, row.id)
        except IntegrityFailure:
            _audit(
                conn,
                A_INTEGRITY,
                "refused",
                name,
                reason="ciphertext not authenticated",
                now=now,
                actor=GATEWAY,
            )
            return IntegrityErrorAccess(IntegrityErrorAccess.code)
        if tokens is None:
            return ReauthRequired(ReauthRequired.code)
        if row.kind == "provided":
            return Access(f"Bearer {tokens['access_token']}", row.generation, None)
        valid = row.expires_at is not None and row.expires_at - now >= EXPIRY_MARGIN
        if valid and not force:
            return Access(f"Bearer {tokens['access_token']}", row.generation, row.expires_at)
        refresh = tokens.get("refresh_token")
        if not isinstance(refresh, str) or not refresh:
            if force:
                return Refused(f"{name}: no refresh token (reauthorize)")
            # BR-091: no `refresh_token`, reauthentication required within the margin, no call.
            conn.execute(
                "UPDATE connection SET state = 'reauth_required', last_outcome = 'permanent',"
                " last_reason = 'no_refresh_token' WHERE id = ?",
                (row.id,),
            )
            _audit(
                conn,
                A_RENEW,
                "permanent",
                name,
                reason="no_refresh_token",
                now=now,
                actor=GATEWAY,
            )
            return ReauthRequired(ReauthRequired.code)
        lease = _lease(conn, row.id)
        if _lease_live(lease, now):
            return _Wait(row.generation, float(lease.renewing_until or now) - now)
        if lease.renewing_until is not None:
            # BR-089, BR-090: lease expired, no result written — uncertain outcome, audited here.
            conn.execute(
                "UPDATE connection SET last_outcome = 'uncertain',"
                " last_reason = 'lease_expired', renewing_until = NULL,"
                " renewal_owner = NULL WHERE id = ?",
                (row.id,),
            )
            _audit(
                conn,
                A_RENEW,
                "uncertain",
                name,
                reason="lease_expired",
                now=now,
                actor=GATEWAY,
            )
        if row.not_before is not None and row.not_before > now:
            if force:
                return Refused(f"{name}: not before {audit.utc(row.not_before)} (BR-094)")
            return TemporarilyUnavailable(float(row.not_before) - now)
        until = math.ceil(now + LEASE_S)
        conn.execute(
            "UPDATE connection SET renewing_until = ?, renewal_owner = ? WHERE id = ?",
            (until, owner, row.id),
        )
        return _Renew(row, name, owner, refresh, _upstream_of(conn, sealer, row), until)


def _peek(
    conn: sqlite3.Connection, key: ConnectionsKey, connection_id: str, generation: int, now: float
) -> Access | AccessError | None:
    """Poll of a caller waiting for another's lease: None as long as it must wait."""
    row = _live(conn, connection_id)
    if row is None:
        return NotPlaced(NotPlaced.code)
    if row.state == "reauth_required":
        return ReauthRequired(ReauthRequired.code)
    if row.state != "active":
        return NotAuthorized(NotAuthorized.code)
    lease = _lease(conn, connection_id)
    if row.generation != generation:
        if row.expires_at is None or row.expires_at - now < EXPIRY_MARGIN:
            return None
        try:
            sealer = key.sealer(conn)
            tokens = _tokens(conn, sealer, row.id)
        except KeyUnavailable as exc:
            return KeyUnavailableError(exc.reason)
        except IntegrityFailure:
            return IntegrityErrorAccess(IntegrityErrorAccess.code)
        if tokens is None:
            return ReauthRequired(ReauthRequired.code)
        return Access(f"Bearer {tokens['access_token']}", row.generation, row.expires_at)
    if lease.renewing_until is None:  # lease released without a new token: the first failed
        wait = None if row.not_before is None else max(float(row.not_before) - now, 0.0)
        return TemporarilyUnavailable(wait)
    return None


def _wait(
    run: Runner, key: ConnectionsKey, connection_id: str, step: _Wait, clock: Clock, sleep: Sleep
) -> Access:
    deadline = time.monotonic() + max(step.remaining, 0.0) + 1.0
    while True:
        sleep(POLL_S)
        found = run(lambda c: _peek(c, key, connection_id, step.generation, clock()))
        if isinstance(found, AccessError):
            raise found
        if found is not None:
            return found
        if time.monotonic() >= deadline:
            raise TemporarilyUnavailable(None)


@dataclass(frozen=True, slots=True)
class _Discarded:
    pass


def _finish(
    conn: sqlite3.Connection,
    key: ConnectionsKey,
    step: _Renew,
    outcome: upstream.TokenSet | upstream.ProviderError,
    now: float,
    lease_end: float,
) -> Access | AccessError | _Discarded:
    """Second short transaction: writes the outcome if the caller still holds the lease (BR-090).

    The lease is held if the marker and the generation are the caller's and the end of the lease
    has passed neither on the database clock nor on the process monotonic clock (a write that
    obtains the lock after the end of the lease does not write the token: uncertain outcome)."""
    with provider_outcome(), transaction(conn):
        row = _live(conn, step.row.id)
        lease = _lease(conn, step.row.id)
        mine = (
            row is not None and lease.owner == step.owner and row.generation == step.row.generation
        )
        if mine and (time.monotonic() >= lease_end or not _lease_live(lease, now)):
            _mark_uncertain(conn, step, lease.fail_count, now)
            return _Discarded()
        if not mine or row is None:
            _audit(
                conn,
                A_RENEW,
                "discarded",
                step.name,
                reason="result outside the lease discarded",
                now=now,
                actor=GATEWAY,
            )
            return _Discarded()
        after_uncertain = row.last_outcome == "uncertain"
        if isinstance(outcome, upstream.TokenSet):
            sealer = key.sealer(conn)
            refresh = outcome.refresh_token or step.refresh  # no rotation: the old one serves
            sealed = _seal_tokens(conn, sealer, row.id, outcome.access_token, refresh)
            expires_at, estimated = _expiry(outcome, now)
            # An unchanged refresh token keeps its known end; rotation without an expiry
            # makes the new token's end unknown. No secret is needed by the console read.
            refresh_end = row.refresh_expires_at
            if outcome.refresh_token is not None and outcome.refresh_token != step.refresh:
                refresh_end = None
            if outcome.refresh_token_expires_in is not None:
                refresh_end = int(now) + outcome.refresh_token_expires_in
            scopes = row.granted_scopes if outcome.scope is None else outcome.scope
            conn.execute(
                "UPDATE connection SET tokens_enc = ?, expires_at = ?, expiry_estimated = ?,"
                " granted_scopes = ?, refresh_expires_at = ?, generation = generation + 1,"
                " not_before = NULL,"
                " fail_count = 0, last_outcome = 'renewed', last_reason = NULL,"
                " renewing_until = NULL, renewal_owner = NULL WHERE id = ?",
                (sealed, expires_at, int(estimated), scopes, refresh_end, row.id),
            )
            changed = scopes != row.granted_scopes
            _audit(
                conn,
                A_RENEW,
                "ok",
                step.name,
                now=now,
                actor=GATEWAY,
                before={"scopes": row.granted_scopes} if changed else None,
                after={"scopes": scopes} if changed else None,
                detail={"generation": row.generation + 1, "estimated_expiry": estimated},
            )
            return Access(f"Bearer {outcome.access_token}", row.generation + 1, expires_at)
        reason = outcome.reason + (" after an uncertain outcome" if after_uncertain else "")
        if outcome.kind == upstream.DEFINITIVE:
            conn.execute(
                "UPDATE connection SET state = 'reauth_required', tokens_enc = NULL,"
                " generation = generation + 1, not_before = NULL, fail_count = 0,"
                " last_outcome = 'permanent', last_reason = ?, renewing_until = NULL,"
                " renewal_owner = NULL WHERE id = ?",
                (outcome.reason, row.id),
            )
            _audit(conn, A_RENEW, "permanent", step.name, reason=reason, now=now, actor=GATEWAY)
            return ReauthRequired(ReauthRequired.code)
        delay = _backoff(lease.fail_count, outcome.retry_after)
        conn.execute(
            "UPDATE connection SET not_before = ?, fail_count = fail_count + 1,"
            " last_outcome = ?, last_reason = ?, renewing_until = NULL, renewal_owner = NULL"
            " WHERE id = ?",
            (int(now) + delay, outcome.kind, outcome.reason, row.id),
        )
        _audit(
            conn,
            A_RENEW,
            outcome.kind,
            step.name,
            reason=reason,
            now=now,
            actor=GATEWAY,
            detail={"not_before": audit.utc(int(now) + delay)},
        )
        return TemporarilyUnavailable(float(delay))


def _mark_uncertain(conn: sqlite3.Connection, step: _Renew, fail_count: int, now: float) -> None:
    """Uncertain outcome (BR-090): state and ciphertext unchanged, "not before" date (BR-094)."""
    delay = _backoff(fail_count, None)
    conn.execute(
        "UPDATE connection SET not_before = ?, fail_count = fail_count + 1,"
        " last_outcome = 'uncertain', last_reason = 'lease_write_failed',"
        " renewing_until = NULL, renewal_owner = NULL WHERE id = ?",
        (int(now) + delay, step.row.id),
    )
    _audit(
        conn,
        A_RENEW,
        "uncertain",
        step.name,
        reason="lease_write_failed",
        now=now,
        actor=GATEWAY,
    )


def _record_uncertain(conn: sqlite3.Connection, step: _Renew, now: float) -> None:
    """Write impossible until the end of the lease: uncertain outcome, if nobody took over."""
    with provider_outcome(), transaction(conn):
        row = _live(conn, step.row.id)
        lease = _lease(conn, step.row.id)
        if row is None or lease.owner != step.owner or row.generation != step.row.generation:
            return
        _mark_uncertain(conn, step, lease.fail_count, now)


def _renew(
    run: Runner, key: ConnectionsKey, out: Outbound, step: _Renew, clock: Clock, sleep: Sleep
) -> Access:
    lease_end = time.monotonic() + LEASE_S
    outcome: upstream.TokenSet | upstream.ProviderError
    endpoint = step.up.endpoint("token_endpoint")
    if endpoint is None or step.up.client is None:
        outcome = upstream.ProviderError(ANOMALY, "unknown token endpoint")
    else:
        try:
            outcome = upstream.refresh(
                out,
                endpoint,
                step.up.client,
                step.refresh,
                step.up.resource,
                step.up.internal,
                clock(),
            )
        except upstream.ProviderError as exc:
            outcome = exc
    while time.monotonic() < lease_end:
        try:
            # A composite runner may open its own transaction before invoking _finish.
            # Exempt that outer outcome record too, then restore checks for later decisions.
            with provider_outcome():
                result = run(lambda c: _finish(c, key, step, outcome, clock(), lease_end))
        except (StoreUnavailable, KeyUnavailable):
            sleep(POLL_S)  # BR-090: retried as long as the caller holds the lease
            continue
        if isinstance(result, _Discarded):
            if isinstance(outcome, upstream.TokenSet):
                _discard(run, out, step.up, outcome, step.name, clock)
            raise TemporarilyUnavailable(None)
        if isinstance(result, AccessError):
            raise result
        return result
    # End of the lease without a write: nothing is returned, state and ciphertext stay as before.
    if isinstance(outcome, upstream.TokenSet):
        _discard(run, out, step.up, outcome, step.name, clock)
    with provider_outcome(), contextlib.suppress(StoreUnavailable):
        run(lambda c: _record_uncertain(c, step, clock()))
    raise TemporarilyUnavailable(None)


def access(
    run: Runner,
    key: ConnectionsKey,
    out: Outbound,
    connection_id: str,
    role_id: str | None,
    *,
    clock: Clock = time.time,
    sleep: Sleep = time.sleep,
    force: bool = False,
) -> Access:
    """Token valid for ≥ 60 s, or a typed error (§3.3). `force`: `connection renew` (BR-089),
    subject to the lease and the "not before" date, without a role; its refusals raise `Refused`."""
    owner = secrets.token_hex(8)
    step = run(lambda c: _begin(c, key, connection_id, role_id, clock(), owner, force))
    if isinstance(step, (AccessError, Refused)):
        raise step
    if isinstance(step, Access):
        return step
    if isinstance(step, _Wait):
        return _wait(run, key, connection_id, step, clock, sleep)
    return _renew(run, key, out, step, clock, sleep)


async def get_access(
    db: Database,
    key: ConnectionsKey,
    connection_id: str,
    role_id: str,
    *,
    out: Outbound,
    clock: Clock = time.time,
) -> Access:
    """`out` is the process's outbound OAuth client, built once (BR-125): never built here,
    and its absence is a programming error."""
    return await asyncio.to_thread(access, db.call, key, out, connection_id, role_id, clock=clock)


def renew_now(
    run: Runner,
    key: ConnectionsKey,
    out: Outbound,
    text: str,
    *,
    clock: Clock = time.time,
    sleep: Sleep = time.sleep,
) -> tuple[str, Access]:
    """`connection renew <c>`: forced renewal under a lease, audited; code 2 for a provided
    token or during the "not before" date (BR-089, BR-094)."""

    def find(conn: sqlite3.Connection) -> tuple[str, str]:
        tree = Tree.load(conn)
        row = _find(conn, tree, text)
        return row.id, display_name(tree.nodes, row)

    connection_id, name = run(find)
    run(lambda c: key.sealer(c))  # "refused" list without the key (§3.2): code 1, nothing written
    try:
        got = access(run, key, out, connection_id, None, clock=clock, sleep=sleep, force=True)
    except ReauthRequired:
        raise Refused(
            f"{name}: definitive refusal from the provider, reauthentication required"
        ) from None
    except TemporarilyUnavailable as exc:
        wait = "" if exc.retry_after is None else f", not before {int(exc.retry_after)} s"
        raise Unavailable(f"provider temporarily unavailable{wait}") from None
    return name, got


# ---------------------------------------------------------------------------------------------
# signal_refusal (§3.3, BR-085)
# ---------------------------------------------------------------------------------------------


def signal(
    run: Runner,
    key: ConnectionsKey,
    connection_id: str,
    generation: int,
    cause: str,
    *,
    clock: Clock = time.time,
) -> str:
    """Upstream 401 on an `Access` of `generation`. Provided token: `reauthentication required`;
    OAuth: access token of this generation marked expired (the next `get_access` renews). Stale
    generation: ignored. Audited without the raw upstream cause; returns the effect. The only
    recognized cause is `MCP_401` (relay, BR-127): it gives the `last_reason` `mcp_refusal_401`;
    any other cause is never repeated (BR-066 v2)."""
    reason = SIGNAL_MCP_REASON if cause == MCP_401 else "upstream_refusal"

    def act(conn: sqlite3.Connection) -> str:
        with transaction(conn):
            now = clock()
            row = _live(conn, connection_id)
            if row is None:
                return "unknown"
            name = display_name(Tree.load(conn).nodes, row)
            if key.status(conn) is not None:
                effect = "ignored (key unavailable)"
            elif row.generation != generation or row.state != "active":
                effect = "ignored (stale generation)"
            elif row.kind == "provided":
                conn.execute(
                    "UPDATE connection SET state = 'reauth_required', last_outcome = 'permanent',"
                    " last_reason = ? WHERE id = ?",
                    (reason, row.id),
                )
                effect = "reauthentication required"
            else:
                conn.execute(
                    "UPDATE connection SET expires_at = ? WHERE id = ?", (int(now), row.id)
                )
                effect = "access token marked expired"
            _audit(
                conn,
                A_SIGNAL,
                "ok",
                name,
                reason="upstream_refusal",
                now=now,
                actor=GATEWAY,
                detail={"generation": generation, "effect": effect},
            )
            return effect

    return run(act)


async def signal_refusal(
    db: Database,
    key: ConnectionsKey,
    connection_id: str,
    generation: int,
    cause: str,
    *,
    clock: Clock = time.time,
) -> None:
    await asyncio.to_thread(signal, db.call, key, connection_id, generation, cause, clock=clock)


# ---------------------------------------------------------------------------------------------
# authorize (BR-082, BR-096)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, repr=False)
class _Grant:
    row: Row
    name: str
    verifier: str
    code: str
    state_hash: str
    up: _Upstream
    redirect_uri: str = REDIRECT_URI  # the one chosen at opening (BR-082 v3)


@dataclass(frozen=True, slots=True)
class Authorized:
    name: str
    account: str | None
    scopes: str
    expires_at: int
    estimated: bool
    previous_account: str | None
    upstream: str  # revocation of the old tokens


@dataclass(frozen=True, slots=True, repr=False)
class _Written:
    result: Authorized
    old: dict[str, Any] | None


def _refuse_authorize(
    conn: sqlite3.Connection, name: str, reason: str, now: float, **kw: Any
) -> None:
    _audit(conn, A_AUTHORIZE_REFUSED, "refused", name, reason=reason, now=now, **kw)


def authorize(
    run: Runner,
    key: ConnectionsKey,
    out: Outbound,
    text: str,
    callback: str,
    confirm_account: bool,
    *,
    clock: Clock = time.time,
    sleep: Sleep = time.sleep,
) -> Authorized:
    """`connection authorize <c> --callback -`: the `state` is consumed in a short transaction
    before the code exchange (BR-082); the account of a reauthorization is compared (BR-096); the
    tokens are written encrypted under a lease, the old ones revoked on a best-effort basis. No
    refusal repeats the callback URL or the code (BR-086)."""
    cb = upstream.parse_callback(callback)

    def consume(conn: sqlite3.Connection) -> _Grant | Refused:
        with transaction(conn):
            now = clock()
            sealer = key.sealer(conn)
            tree = Tree.load(conn)
            row = _find(conn, tree, text)
            name = display_name(tree.nodes, row)
            if row.kind != "oauth":
                raise Refused(f"authorize: {name} has a provided token")
            pending = conn.execute(
                "SELECT state_hash, verifier_enc, expires_at, used, redirect_uri FROM pending_auth"
                " WHERE connection_id = ?",
                (row.id,),
            ).fetchone()
            cause = None
            if (
                cb.state is None
                or pending is None
                or not secrets.compare_digest(state_hash(cb.state), pending[0])
            ):
                cause = "unknown state"
            elif pending[3]:
                cause = "state already used"
            elif pending[2] <= now:
                cause = "state expired"
            if cause is not None:
                _refuse_authorize(conn, name, cause, now)
                return Refused(f"{cause}: run connection reauthorize {name} again")
            conn.execute("UPDATE pending_auth SET used = 1 WHERE connection_id = ?", (row.id,))
            if cb.error is not None:
                _refuse_authorize(conn, name, f"provider: {cb.error}", now)
                return Refused(f"refusal from the provider: {cb.error} (connection unchanged)")
            if cb.code is None:
                _refuse_authorize(conn, name, "code missing", now)
                return Refused("callback URL without a code (connection unchanged)")
            verifier = sealer.open(pending[1], "pending_auth", row.id, "verifier")
            return _Grant(
                row,
                name,
                verifier,
                cb.code,
                pending[0],
                _upstream_of(conn, sealer, row),
                pending[4] or REDIRECT_URI,
            )

    got = run(consume)
    if isinstance(got, Refused):
        raise got
    grant = got
    up = grant.up

    def refused(reason: str, **kw: Any) -> None:
        def write(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                _refuse_authorize(conn, grant.name, reason, clock(), **kw)

        with contextlib.suppress(StoreUnavailable):
            run(write)

    endpoint = up.endpoint("token_endpoint")
    if endpoint is None or up.client is None:
        refused("unknown token endpoint")
        raise Refused(f"service {up.service.id} has no token endpoint: service rediscover")
    try:
        tokens = upstream.exchange_code(
            out,
            endpoint,
            up.client,
            grant.code,
            grant.verifier,
            grant.redirect_uri,
            up.resource,
            up.internal,
            clock(),
        )
    except upstream.ProviderError as exc:
        refused(f"exchange: {exc.reason}")
        message = f"code exchange refused: {exc.reason} (run connection reauthorize again)"
        if exc.kind == upstream.DEFINITIVE:
            raise Refused(message) from None
        raise Unavailable(f"code exchange {exc.kind}: {exc.reason}") from None
    try:
        account = upstream.account(
            out,
            up.service.model,
            up.endpoint("userinfo_endpoint"),
            tokens.access_token,
            up.internal,
        )
    except upstream.ProviderError as exc:
        _discard(run, out, up, tokens, grant.name, clock)
        refused(f"account: {exc.reason}")
        raise Unavailable(f"account reading {exc.kind}: {exc.reason}") from None
    reauth = grant.row.generation > 0
    before_account = grant.row.account
    differs = reauth and (before_account is None or account is None or before_account != account)
    if differs and not confirm_account:
        _discard(run, out, up, tokens, grant.name, clock)
        refused("different account", before={"account": before_account}, after={"account": account})
        raise Refused(
            f"different account: {before_account or 'unknown'} → {account or 'unknown'}; "
            "add --confirm-account (run connection reauthorize again)"
        )
    scopes = tokens.scope if tokens.scope is not None else up.service.scopes

    def write(conn: sqlite3.Connection, force: bool) -> _Written | _Busy:
        with transaction(conn):
            now = clock()
            row = _live(conn, grant.row.id)
            if row is None:
                raise Refused(f"{grant.name} removed during the authorization")
            lease = _lease(conn, row.id)
            if not force and _lease_live(lease, now):
                return _Busy(float(lease.renewing_until or now) - now)
            sealer = key.sealer(conn)
            old: dict[str, Any] | None = None
            with contextlib.suppress(IntegrityFailure):
                old = _tokens(conn, sealer, row.id)
            sealed = _seal_tokens(conn, sealer, row.id, tokens.access_token, tokens.refresh_token)
            expires_at, estimated = _expiry(tokens, now)
            refresh_end = (
                int(now) + tokens.refresh_token_expires_in
                if tokens.refresh_token is not None and tokens.refresh_token_expires_in is not None
                else None
            )
            conn.execute(
                "UPDATE connection SET tokens_enc = ?, state = 'active', account = ?,"
                " granted_scopes = ?, expires_at = ?, expiry_estimated = ?, refresh_expires_at = ?,"
                " not_before = NULL,"
                " fail_count = 0, generation = generation + 1, last_outcome = NULL,"
                " last_reason = NULL, renewing_until = NULL, renewal_owner = NULL WHERE id = ?",
                (sealed, account, scopes, expires_at, int(estimated), refresh_end, row.id),
            )
            _audit(
                conn,
                A_AUTHORIZE,
                "ok",
                grant.name,
                now=now,
                reason="account changed, confirmed" if differs else None,
                before={"state": row.state, "account": row.account, "scopes": row.granted_scopes},
                after={"state": "active", "account": account, "scopes": scopes},
                detail={
                    "reauthorization": reauth,
                    "generation": row.generation + 1,
                    "estimated_expiry": estimated,
                },
            )
            return _Written(
                Authorized(grant.name, account, scopes, expires_at, estimated, row.account, ""),
                old,
            )

    try:
        written = _when_free(run, write, sleep)
    except Refused:
        _discard(run, out, up, tokens, grant.name, clock)
        raise
    outcome = "no_token"
    if written.old is not None:
        outcome = revoke_upstream(out, up, written.old)
        _audit_upstream(run, grant.name, outcome, clock)
    r = written.result
    return Authorized(
        r.name, r.account, r.scopes, r.expires_at, r.estimated, r.previous_account, outcome
    )


# ---------------------------------------------------------------------------------------------
# revoke (BR-095), remove (BR-075)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Revoked:
    connection: str
    upstream: str
    placements: int = 0


@dataclass(frozen=True, slots=True, repr=False)
class _Erased:
    name: str
    tokens: dict[str, Any] | None
    had_tokens: bool
    key_reason: str | None
    up: _Upstream | None
    placements: int


def _erase_tokens(
    conn: sqlite3.Connection, key: ConnectionsKey, row: Row
) -> tuple[dict[str, Any] | None, str | None, _Upstream | None]:
    """Current tokens (if they decrypt) and what is needed to revoke them; read before erasure."""
    key_reason = key.status(conn)
    if key_reason is not None:
        return None, key_reason, None
    sealer = key.sealer(conn)
    tokens: dict[str, Any] | None = None
    with contextlib.suppress(IntegrityFailure):
        tokens = _tokens(conn, sealer, row.id)
    up = _upstream_of(conn, sealer, row) if row.kind == "oauth" else None
    return tokens, None, up


def _finish_upstream(run: Runner, out: Outbound, erased: _Erased, clock: Clock) -> str:
    if not erased.had_tokens:
        return "no_token"
    if erased.key_reason is not None:
        outcome = "key_unavailable"
    elif erased.up is None:
        outcome = "unsupported"
    else:
        outcome = revoke_upstream(out, erased.up, erased.tokens)
    _audit_upstream(run, erased.name, outcome, clock)
    return outcome


def revoke(
    run: Runner,
    key: ConnectionsKey,
    out: Outbound,
    text: str,
    *,
    clock: Clock = time.time,
    sleep: Sleep = time.sleep,
    precondition: Precondition | None = None,
) -> Revoked:
    """`connection revoke <c>`: waits for a renewal in progress, erases the current tokens,
    `reauthentication required`, `generation` + 1, placements and prefix kept; revocation at the
    provider on a best-effort basis outside any transaction. Allowed without the key and outside
    the lock (BR-049). `precondition` runs in the erasing transaction, after the wait (the admin
    API compares its impact digest there, BR-214)."""

    def act(conn: sqlite3.Connection, force: bool) -> _Erased | _Busy:
        with transaction(conn):
            now = clock()
            tree = Tree.load(conn)
            row = _find(conn, tree, text)
            name = display_name(tree.nodes, row)
            lease = _lease(conn, row.id)
            if not force and _lease_live(lease, now):
                return _Busy(float(lease.renewing_until or now) - now)
            if precondition is not None:
                precondition(conn)
            tokens, key_reason, up = _erase_tokens(conn, key, row)
            conn.execute("DELETE FROM pending_auth WHERE connection_id = ?", (row.id,))
            conn.execute(
                "UPDATE connection SET tokens_enc = NULL, state = 'reauth_required',"
                " generation = generation + 1, not_before = NULL, fail_count = 0,"
                " last_outcome = NULL, last_reason = 'revoked', renewing_until = NULL,"
                " renewal_owner = NULL WHERE id = ?",
                (row.id,),
            )
            _audit(
                conn,
                A_REVOKE,
                "ok",
                name,
                now=now,
                before={"state": row.state, "generation": row.generation},
                after={"state": "reauth_required", "generation": row.generation + 1},
            )
            return _Erased(name, tokens, row.has_tokens, key_reason, up, 0)

    erased = _when_free(run, act, sleep)
    with contextlib.suppress(sqlite3.Error, StoreUnavailable):
        run(lambda c: c.execute("PRAGMA wal_checkpoint(TRUNCATE)"))
    return Revoked(erased.name, _finish_upstream(run, out, erased, clock))


def prepare_removal(
    run: Runner, text: str, *, clock: Clock = time.time, sleep: Sleep = time.sleep
) -> _Lease:
    """Wait outside the composite transaction; preserve the exact lease we observed.

    A dead owner's lease may be overridden after its deadline, as in ordinary removal.
    A different lease at write time must be refused, never waited on under the write lock.
    """

    def check(conn: sqlite3.Connection, force: bool) -> _Lease | _Busy:
        row = _find(conn, Tree.load(conn), text)
        lease = _lease(conn, row.id)
        now = clock()
        if not force and _lease_live(lease, now):
            return _Busy(float(lease.renewing_until or now) - now)
        return lease

    return _when_free(run, check, sleep)


def remove(
    run: Runner,
    key: ConnectionsKey,
    out: Outbound,
    text: str,
    confirm: str | None,
    *,
    clock: Clock = time.time,
    sleep: Sleep = time.sleep,
    precondition: Precondition | None = None,
    deferred: list[_Erased] | None = None,
    prepared_lease: _Lease | None = None,
) -> Revoked:
    """`connection remove <c> --confirm <c>` (BR-075): under a lease like `revoke`; tokens,
    placements and open authorization erased in one transaction, id reserved, name and prefix
    released; revocation at the provider on a best-effort basis outside any transaction. Allowed
    without the key (local erasure); refused while a placement is under a locked node (BR-049).
    `precondition` runs in the erasing transaction, after the wait, as for `revoke`."""

    def act(conn: sqlite3.Connection, force: bool) -> _Erased | _Busy:
        with transaction(conn):
            now = clock()
            tree = Tree.load(conn)
            row = _find(conn, tree, text)
            name = display_name(tree.nodes, row)
            if confirm is None or confirm.strip("/") != name:
                raise Refused(f"confirmation required: --confirm {name}")
            roles = [tree.nodes[i] for i in _placements(conn, row.id) if i in tree.nodes]
            for role in roles:
                locker = tree.locked_by(role)
                if locker is not None:
                    raise Refused(
                        f"locked by {tree.path(locker)}: {name} is placed on {tree.path(role)}"
                    )
            lease = _lease(conn, row.id)
            if deferred is not None:
                if prepared_lease is None or lease != prepared_lease:
                    raise Refused("renewal lease changed; preview again")
            elif not force and _lease_live(lease, now):
                return _Busy(float(lease.renewing_until or now) - now)
            if precondition is not None:
                precondition(conn)
            tokens, key_reason, up = _erase_tokens(conn, key, row)
            erase_policy(conn, row.id)  # F-015 BR-176
            conn.execute("DELETE FROM placement WHERE connection_id = ?", (row.id,))
            conn.execute("DELETE FROM pending_auth WHERE connection_id = ?", (row.id,))
            conn.execute("DELETE FROM key_alert WHERE connection_id = ?", (row.id,))
            conn.execute("DELETE FROM tool_name WHERE connection_id = ?", (row.id,))  # BR-110
            conn.execute(
                "UPDATE connection SET removed = 1, removed_at = ?, tokens_enc = NULL,"
                " service_id = NULL, application_id = NULL, generation = generation + 1,"
                " renewing_until = NULL, renewal_owner = NULL WHERE id = ?",
                (int(now), row.id),
            )
            _audit(
                conn,
                A_CONN_REMOVE,
                "ok",
                name,
                now=now,
                before={
                    "kind": row.kind,
                    "service": row.service_id,
                    "prefix": row.prefix,
                    "state": row.state,
                    "placements": sorted(tree.path(r) for r in roles),
                },
            )
            return _Erased(name, tokens, row.has_tokens, key_reason, up, len(roles))

    erased = run(lambda c: act(c, False)) if deferred is not None else _when_free(run, act, sleep)
    if isinstance(erased, _Busy):
        raise Refused("renewal lease changed; preview again")
    if deferred is not None:
        deferred.append(erased)
        return Revoked(erased.name, "deferred", erased.placements)
    # The pages that carried the ciphertext also leave the `-wal` (best effort).
    with contextlib.suppress(sqlite3.Error, StoreUnavailable):
        run(lambda c: c.execute("PRAGMA wal_checkpoint(TRUNCATE)"))
    return Revoked(erased.name, _finish_upstream(run, out, erased, clock), erased.placements)


# ---------------------------------------------------------------------------------------------
# Dynamic registration (BR-083)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Registered:
    app_id: str
    client_id: str
    has_secret: bool
    secret_expires_at: int | None
    affected: tuple[str, ...] = ()
    upstream: tuple[tuple[str, str], ...] = ()


def _registration_endpoint(conn: sqlite3.Connection, service_id: str) -> tuple[str, _Upstream]:
    service = _service(conn, service_id)
    endpoint = service.endpoints.get("registration_endpoint")
    if endpoint is None:
        raise Refused(f"service {service_id}: no registration_endpoint discovered")
    return endpoint, _Upstream(service, None, internal_of(service))


def _register(
    out: Outbound, endpoint: str, up: _Upstream, web_redirect: str | None = None
) -> upstream.Registration:
    try:
        return upstream.register(
            out, endpoint, REDIRECT_URI, up.service.scopes, up.internal, web_redirect
        )
    except upstream.ProviderError as exc:
        if exc.kind == upstream.DEFINITIVE:
            raise Refused(f"registration refused: {exc.reason}") from None
        raise Unavailable(f"registration {exc.kind}: {exc.reason}") from None


def register(
    run: Runner,
    key: ConnectionsKey,
    out: Outbound,
    app_id: str,
    service_id: str,
    *,
    clock: Clock = time.time,
    web_redirect: str | None = None,
) -> Registered:
    """`application declare <id> --service <s> --mode dynamic`: registers the gateway with the
    provider and keeps the `client_id` and the encrypted secret before any authorization. With
    `web_redirect` (the console's return URI) both URIs are registered, and the application's
    web return is set (BR-216)."""
    validate_segment(app_id)

    def check(conn: sqlite3.Connection) -> tuple[str, _Upstream]:
        key.sealer(conn)  # "refused" list without the key (§3.2)
        if conn.execute("SELECT 1 FROM application WHERE id = ?", (app_id,)).fetchone():
            raise Refused(f"application already declared: {app_id} (reregister to redo it)")
        return _registration_endpoint(conn, service_id)

    endpoint, up = run(check)
    reg = _register(out, endpoint, up, web_redirect)

    def write(conn: sqlite3.Connection) -> Registered:
        with transaction(conn):
            now = clock()
            sealer = key.sealer(conn)
            if conn.execute("SELECT 1 FROM application WHERE id = ?", (app_id,)).fetchone():
                raise Refused(f"application already declared: {app_id}")
            sealed = (
                None
                if reg.secret is None
                else sealer.seal(conn, "application", app_id, "secret", reg.secret)
            )
            conn.execute(
                "INSERT INTO application (id, service_id, mode, client_id, redirect_uri,"
                " secret_enc, secret_expires_at, auth_method, created_at, web_return)"
                " VALUES (?, ?, 'dynamic', ?, ?, ?, ?, ?, ?, ?)",
                (
                    app_id,
                    service_id,
                    reg.client_id,
                    REDIRECT_URI,
                    sealed,
                    reg.secret_expires_at,
                    reg.method,
                    int(now),
                    int(web_redirect is not None),
                ),
            )
            _audit(
                conn,
                A_APP_REGISTER,
                "ok",
                app_id,
                now=now,
                after={
                    "service": service_id,
                    "mode": "dynamic",
                    "client_id": reg.client_id,
                    "secret": sealed is not None,
                    "method": reg.method,
                    "secret_expires_at": reg.secret_expires_at,
                },
            )
            return Registered(app_id, reg.client_id, sealed is not None, reg.secret_expires_at)

    return run(write)


@dataclass(frozen=True, slots=True, repr=False)
class _Plan:
    result: Registered
    old_client: upstream.Client
    revocations: list[tuple[str, dict[str, Any] | None]]


def reregister(
    run: Runner,
    key: ConnectionsKey,
    out: Outbound,
    app_id: str,
    confirm: str | None,
    *,
    clock: Clock = time.time,
    web_redirect: str | None = None,
    precondition: Precondition | None = None,
) -> Registered:
    """`application reregister <id> --confirm <id>` (BR-083): new client at the provider; in
    the transaction that writes it, its connections move to `reauthentication required` without a
    token; their tokens are revoked on a best-effort basis with the **old** client.
    `precondition` runs before the provider is called and again in the writing transaction (the
    admin API compares its impact digest, BR-214): a change in between registers a client that is
    never used, and changes nothing here."""

    def check(conn: sqlite3.Connection) -> tuple[str, _Upstream, list[str]]:
        key.sealer(conn)
        app = next((a for a in _applications(conn) if a.id == app_id), None)
        if app is None:
            raise Refused(f"unknown application: {app_id}")
        if app.mode != "dynamic":
            raise Refused(f"reregister: {app_id} is declared by hand (mode declared)")
        tree = Tree.load(conn)
        users = sorted(
            display_name(tree.nodes, _row(r))
            for r in conn.execute(
                f"SELECT {_ROW_COLUMNS} FROM connection"  # noqa: S608
                " WHERE removed = 0 AND application_id = ?",
                (app_id,),
            ).fetchall()
        )
        if confirm is None or confirm != app_id:
            listed = ", ".join(users) or "none"
            raise Refused(
                f"confirmation required: --confirm {app_id} (connections moved to "
                f"reauthentication required: {listed})"
            )
        endpoint, up = _registration_endpoint(conn, app.service_id)
        if precondition is not None:
            precondition(conn)
        return endpoint, up, users

    endpoint, up, _users = run(check)
    reg = _register(out, endpoint, up, web_redirect)

    def write(conn: sqlite3.Connection) -> _Plan:
        with transaction(conn):
            now = clock()
            if precondition is not None:
                precondition(conn)
            sealer = key.sealer(conn)
            old_client = _client(conn, sealer, app_id)
            tree = Tree.load(conn)
            rows = [
                _row(r)
                for r in conn.execute(
                    f"SELECT {_ROW_COLUMNS} FROM connection"  # noqa: S608
                    " WHERE removed = 0 AND application_id = ?",
                    (app_id,),
                ).fetchall()
            ]
            revocations: list[tuple[str, dict[str, Any] | None]] = []
            for row in sorted(rows, key=lambda r: display_name(tree.nodes, r)):
                tokens: dict[str, Any] | None = None
                with contextlib.suppress(IntegrityFailure):
                    tokens = _tokens(conn, sealer, row.id)
                conn.execute("DELETE FROM pending_auth WHERE connection_id = ?", (row.id,))
                conn.execute(
                    "UPDATE connection SET tokens_enc = NULL, state = 'reauth_required',"
                    " generation = generation + 1, not_before = NULL, fail_count = 0,"
                    " last_outcome = NULL, last_reason = 'application_reregistered',"
                    " renewing_until = NULL, renewal_owner = NULL WHERE id = ?",
                    (row.id,),
                )
                revocations.append((display_name(tree.nodes, row), tokens))
            sealed = (
                None
                if reg.secret is None
                else sealer.seal(conn, "application", app_id, "secret", reg.secret)
            )
            before = conn.execute(
                "SELECT client_id FROM application WHERE id = ?", (app_id,)
            ).fetchone()[0]
            conn.execute(
                "UPDATE application SET client_id = ?, secret_enc = ?, secret_expires_at = ?,"
                " auth_method = ?, web_return = ? WHERE id = ?",
                (
                    reg.client_id,
                    sealed,
                    reg.secret_expires_at,
                    reg.method,
                    int(web_redirect is not None),
                    app_id,
                ),
            )
            _audit(
                conn,
                A_APP_REREGISTER,
                "ok",
                app_id,
                now=now,
                before={"client_id": before},
                after={"client_id": reg.client_id, "secret": sealed is not None},
                detail={"connections": [n for n, _ in revocations]},
            )
            result = Registered(
                app_id,
                reg.client_id,
                sealed is not None,
                reg.secret_expires_at,
                tuple(n for n, _ in revocations),
            )
            return _Plan(result, old_client, revocations)

    plan = run(write)
    outcomes: list[tuple[str, str]] = []
    old_up = _Upstream(up.service, plan.old_client, up.internal)
    for name, tokens in plan.revocations:
        outcome = revoke_upstream(out, old_up, tokens)
        if tokens is not None:
            _audit_upstream(run, name, outcome, clock)
        outcomes.append((name, outcome))
    r = plan.result
    return Registered(
        r.app_id, r.client_id, r.has_secret, r.secret_expires_at, r.affected, tuple(outcomes)
    )
