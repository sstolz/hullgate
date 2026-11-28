"""SQLite database of the gateway (`${DATA_DIR}/hullgate.db`, WAL): probe and transactions."""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from hullgate import migrations

log = logging.getLogger("hullgate.db")

READY_TIMEOUT_S = 1.0


class StoreUnavailable(Exception):
    """The vault cannot be read or written (BR-064); nothing was written."""


@dataclass(frozen=True, slots=True)
class Readiness:
    ok: bool
    reason: str = ""


def connect(path: str, *, create: bool = True) -> sqlite3.Connection:
    """Autocommit connection, bounded lock wait (1 s), foreign keys on.

    `secure_delete`: an erased value (encrypted token of a removed connection, BR-075) is
    overwritten with zeros in its page instead of staying in the free space of the file.
    """
    if not create and not os.path.exists(path):
        raise StoreUnavailable(f"database absent: {path}")
    conn = sqlite3.connect(
        path,
        timeout=READY_TIMEOUT_S,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA secure_delete = ON")
    return conn


_PRECONDITION: contextvars.ContextVar[Callable[[sqlite3.Connection], None] | None] = (
    contextvars.ContextVar("transaction_precondition", default=None)
)


@contextlib.contextmanager
def transaction_precondition(check: Callable[[sqlite3.Connection], None]) -> Iterator[None]:
    """Check request authorization after each outer write lock unless explicitly admitted.
    The context follows the request into its worker threads."""
    token = _PRECONDITION.set(check)
    try:
        yield
    finally:
        _PRECONDITION.reset(token)


@contextlib.contextmanager
def admitted_operation(db: Database, *, recheck: bool = False) -> Iterator[None]:
    """Admit a multi-transaction operation before it can call a provider.

    Resolve the request precondition under a short write lock. By default, the operation
    then runs without another session check (the admission-only routes). With `recheck`, keep
    checking decision transactions; only explicit provider outcome records are exempt. Domain
    checks remain in force. Admission releases both locks before yielding. Outside an admin
    request there is no precondition to admit or suppress.
    """
    if _PRECONDITION.get() is None:
        yield
        return

    def admit(conn: sqlite3.Connection) -> None:
        with transaction(conn):
            pass

    db.call(admit)
    if recheck:
        yield
    else:
        with provider_outcome():
            yield


@contextlib.contextmanager
def provider_outcome() -> Iterator[None]:
    """Record an already performed provider exchange without a new session decision.

    The deciding transaction must have checked authorization before the exchange. Restore
    that check afterwards so subsequent decisions in the request still require a live session.
    """
    token = _PRECONDITION.set(None)
    try:
        yield
    finally:
        _PRECONDITION.reset(token)


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """`BEGIN IMMEDIATE` write transaction (R-06, BR-065): all or nothing.

    Any SQLite error becomes `StoreUnavailable` after rollback; another exception (a business
    refusal) rolls back too and propagates as is.

    Inside an open transaction (the admin API compares an impact digest and runs the act in one
    transaction, BR-214) the block is a savepoint: an exception rolls back the block only, and the
    outer transaction commits or rolls back the whole.
    """
    if conn.in_transaction:
        yield from _savepoint(conn)
        return
    try:
        main = conn.execute("PRAGMA database_list").fetchone()
        # In WAL, a read-only file accepts the lock and even the write (into -wal): the right
        # to write the file is checked separately, as for /readyz.
        if main is not None and main[2] and not os.access(main[2], os.W_OK):
            raise StoreUnavailable("database read-only")
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        raise StoreUnavailable(str(exc)) from exc
    try:
        check = _PRECONDITION.get()
        if check is not None:
            check(conn)
        yield conn
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        _rollback(conn)
        raise StoreUnavailable(str(exc)) from exc
    except BaseException:
        _rollback(conn)
        raise


def _savepoint(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        conn.execute("SAVEPOINT nested")
    except sqlite3.Error as exc:
        raise StoreUnavailable(str(exc)) from exc
    try:
        yield conn
        conn.execute("RELEASE nested")
    except sqlite3.Error as exc:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK TO nested")
            conn.execute("RELEASE nested")
        raise StoreUnavailable(str(exc)) from exc
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK TO nested")
            conn.execute("RELEASE nested")
        raise


def _rollback(conn: sqlite3.Connection) -> None:
    with contextlib.suppress(sqlite3.Error):
        if conn.in_transaction:
            conn.execute("ROLLBACK")


class Database:
    """Single connection opened at startup; a failure to open does not stop the process.

    `open()` applies the migrations (BR-069); a failure leaves the gateway not ready.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._schema_reason = ""

    @property
    def opened(self) -> bool:
        return self._conn is not None

    def open(self) -> bool:
        """Opens (creating if absent) the database in WAL mode, then migrates it; False if not."""
        try:
            conn = connect(self.path)
            version = migrations.user_version(conn)
            if version > migrations.SCHEMA_VERSION:
                # Database newer than the code: left intact, not even switched to WAL.
                log.error(
                    "schema: database at version %d, code at version %d",
                    version,
                    migrations.SCHEMA_VERSION,
                )
                self._schema_reason = "schema_too_new"
                self._conn = conn
                return True
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error as exc:
            log.warning("database unavailable (%s): %s", self.path, exc)
            return False
        try:
            migrations.migrate(conn)
        except migrations.SchemaError as exc:
            log.error("schema: %s", exc)
            self._schema_reason = exc.reason
        except sqlite3.Error as exc:
            log.error("schema unreadable: %s", exc)
            self._schema_reason = "schema_error"
        self._conn = conn
        return True

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def call[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Runs `fn` on the connection, under the process lock. Blocking call.

        Database not open or SQLite error: `StoreUnavailable`.
        """
        with self._lock:
            conn = self._conn
            if conn is None:
                raise StoreUnavailable("database not open")
            try:
                return fn(conn)
            except sqlite3.Error as exc:
                raise StoreUnavailable(str(exc)) from exc

    def readiness(self) -> Readiness:
        """Database open, at the code's schema, accepting a write lock, without writing anything.

        BR-033, BR-064, BR-069. `BEGIN IMMEDIATE` then `ROLLBACK`, wait bounded to 1 s. In WAL
        mode, the write lock lives in `-shm` and is obtained even on a read-only file: the right
        to write the file is therefore checked separately (`os.access`). Evaluated on every
        call, without cache. Blocking call: to run outside the loop.
        """
        with self._lock:
            conn = self._conn
            if conn is None:
                return Readiness(False, "db_unavailable")
            if not os.path.exists(self.path):
                return Readiness(False, "db_unavailable")
            if not os.access(self.path, os.W_OK):
                return Readiness(False, "db_read_only")
            if self._schema_reason:
                return Readiness(False, self._schema_reason)
            try:
                version = migrations.user_version(conn)
            except sqlite3.Error:
                return Readiness(False, "db_unavailable")
            if version > migrations.SCHEMA_VERSION:
                return Readiness(False, "schema_too_new")
            if version != migrations.SCHEMA_VERSION:
                return Readiness(False, "schema_error")
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                text = str(exc).lower()
                if "locked" in text or "busy" in text:
                    return Readiness(False, "db_locked")
                if "readonly" in text or "read-only" in text:
                    return Readiness(False, "db_read_only")
                return Readiness(False, "db_unavailable")
            except sqlite3.Error:
                return Readiness(False, "db_unavailable")
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                return Readiness(False, "db_unavailable")
            return Readiness(True)
