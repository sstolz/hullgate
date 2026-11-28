"""Append-only audit log (BR-066): issuances, revocations, refusals, tree changes.

No entry carries a token value, a code, a verifier, a secret or a full fingerprint: callers pass
only identifiers, paths, reasons and setting values.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import re
import sqlite3
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

OPERATOR_CLI = "operator-cli"
ANONYMOUS = "anonymous"

# The actor of the domain operations run in this context: `operator-cli` from the CLI, the
# administrator (`admin:<id>`, BR-211, BR-066 v5) when the admin API runs the same operation.
_ACTOR: contextvars.ContextVar[str] = contextvars.ContextVar("hullgate_actor", default=OPERATOR_CLI)


def actor() -> str:
    """The acting actor: `operator-cli` unless `acting_as` says otherwise."""
    return _ACTOR.get()


@contextlib.contextmanager
def acting_as(who: str) -> Iterator[None]:
    """Runs the enclosed domain operations as `who` (the admin API's administrator)."""
    token = _ACTOR.set(who)
    try:
        yield
    finally:
        _ACTOR.reset(token)


_CONTEXT: contextvars.ContextVar[dict[str, object] | None] = contextvars.ContextVar(
    "hullgate_change_context", default=None
)


@contextlib.contextmanager
def change_context(**values: object) -> Iterator[None]:
    """Tag the new entries of a domain act; never rewrite an existing audit row."""
    token = _CONTEXT.set({**(_CONTEXT.get() or {}), **values})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


@dataclass(frozen=True, slots=True)
class Entry:
    seq: int
    at: str
    actor: str
    node_id: str | None
    path: str | None
    action: str
    result: str
    reason: str | None
    before: str | None
    after: str | None
    detail: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at": self.at,
            "actor": self.actor,
            "node_id": self.node_id,
            "path": self.path,
            "action": self.action,
            "result": self.result,
            "reason": self.reason,
            "before": self.before,
            "after": self.after,
            "detail": self.detail,
        }


def utc(ts: float) -> str:
    """UTC ISO 8601 timestamp, to the second."""
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def write(
    conn: sqlite3.Connection,
    *,
    actor: str,
    action: str,
    result: str,
    node_id: str | None = None,
    path: str | None = None,
    reason: str | None = None,
    before: object = None,
    after: object = None,
    detail: Mapping[str, object] | None = None,
    now: float | None = None,
) -> None:
    """Appends an entry; call it inside the transaction of the act it records (BR-065)."""
    conn.execute(
        "INSERT INTO audit"
        " (at, actor, node_id, path, action, result, reason, before, after, detail)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            utc(time.time() if now is None else now),
            actor,
            node_id,
            path,
            action,
            result,
            reason,
            _text(before),
            _text(after),
            _text({**(detail or {}), **(_CONTEXT.get() or {})})
            if detail or _CONTEXT.get()
            else None,
        ),
    )


FILTER_KEYS = ("since", "until", "actor", "path", "action", "result")
_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


@dataclass(frozen=True, slots=True)
class Filter:
    """A reading of the journal (BR-218): time bounds (UTC ISO 8601, inclusive), exact actor and
    result, path and action prefixes."""

    since: str | None = None
    until: str | None = None
    actor: str | None = None
    path: str | None = None
    action: str | None = None
    result: str | None = None

    def __post_init__(self) -> None:
        for name in ("since", "until"):
            value = getattr(self, name)
            if value is not None and not _TIME.match(value):
                raise ValueError(f"{name}: UTC ISO 8601 to the second, as 2026-11-14T09:00:00Z")

    def as_dict(self) -> dict[str, str]:
        return {k: v for k in FILTER_KEYS if (v := getattr(self, k)) is not None}


def _prefix(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def query(
    conn: sqlite3.Connection, where: Filter, *, before: int | None = None, limit: int = 100
) -> tuple[list[Entry], int | None]:
    """Entries matching `where`, newest first, `seq` below `before`; the second value is the
    `before` of the next page, or None on the last one. Reads only."""
    clauses: list[str] = []
    values: list[object] = []
    for column, op, value in (
        ("at", ">=", where.since),
        ("at", "<=", where.until),
        ("actor", "=", where.actor),
        ("result", "=", where.result),
        ("seq", "<", before),
    ):
        if value is not None:
            clauses.append(f"{column} {op} ?")
            values.append(value)
    for column, prefix in (("path", where.path), ("action", where.action)):
        if prefix is not None:
            clauses.append(f"{column} LIKE ? ESCAPE '\\'")
            values.append(_prefix(prefix))
    # fixed columns and operators; every value is a parameter
    sql = (
        "SELECT seq, at, actor, node_id, path, action, result, reason, before, after, detail"  # noqa: S608
        " FROM audit"
        + (" WHERE " + " AND ".join(clauses) if clauses else "")
        + " ORDER BY seq DESC LIMIT ?"
    )
    rows = conn.execute(sql, [*values, limit + 1]).fetchall()
    entries = [Entry(*row) for row in rows[:limit]]
    following = entries[-1].seq if len(rows) > limit and entries else None
    return entries, following


def tail(conn: sqlite3.Connection, n: int) -> list[Entry]:
    """The last `n` entries, oldest to newest."""
    rows = conn.execute(
        "SELECT seq, at, actor, node_id, path, action, result, reason, before, after, detail"
        " FROM audit ORDER BY seq DESC LIMIT ?",
        (max(n, 0),),
    ).fetchall()
    return [Entry(*row) for row in reversed(rows)]
