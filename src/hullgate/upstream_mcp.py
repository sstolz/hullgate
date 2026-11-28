"""Bounded upstream MCP client (F-008 lot A): BR-107, BR-108, BR-117 to BR-119 (upstream side),
BR-124, BR-125.

**Transport** (BR-107). Same mechanism as the outbound OAuth client (`hullgate.outbound`, BR-084):
`http.client`, `socket`, `ssl`, one worker thread per exchange, connection to the verified address
(the name serves only for SNI and the certificate), no redirect followed, no environment variable
read (neither proxy nor certificate store). Its own bound profile: a hard deadline on the whole
exchange, name resolution included, of 15 s for the handshake and `tools/list`, of 60 s for
`tools/call`; 1 MiB read at most, SSE stream included. The outbound headers are those of BR-107's
closed list (see `UpstreamClient._headers`), plus the `Host` and `Content-Length` that HTTP/1.1
requires; no header of the inbound client arrives here.

**Addresses** (BR-106, BR-107). Every resolved address is public and the URL is https; the only
exception, when the operator named the host of the MCP URL internal at declaration: every address
is private (10/8, 172.16/12, 192.168/16, fc00::/7; no CGNAT, loopback, link-local, unspecified or
multicast), and `http://` is then allowed. A mix is refused. The policy is replayed at each
exchange, with no connection when it refuses.

**Concurrency** (BR-108). At most 4 simultaneous exchanges per connection and 32 in total, in a
thread pool of the relay's own (`hullgate-relay`), distinct from the OAuth client's; beyond that,
`upstream_busy` at once, with no queue and no exchange.

**Classification** (BR-116, BR-117). Request not fully sent → `upstream_unavailable`; sent and
deadline reached → `deadline_exceeded`; sent and cut before the deadline → `uncertain_outcome`;
more than 1 MiB → `response_too_large`. No exchange is ever replayed, except the request an
upstream refused for an unknown session before executing it (BR-119).

**Generations** (BR-118). The gateway probes `server/discover` in 2026-07-28 (sessionless: `_meta`
envelope, `Mcp-Method`, `Mcp-Name`, `Mcp-Param-*` headers drawn from the body); without positive
proof, it falls back to the `initialize` handshake in 2025-11-25 and accepts the upstream's
counter-proposal among 2024-11-05 to 2025-11-25. No client capability is announced to the
upstream.

**Sessions** (BR-119). One session per (role, connection), never shared; a session the upstream
declares unknown (404) is reopened once, a single reopening per key even if several calls are in
flight; explicit close (`DELETE`, 2 s at most, without waiting); 64 sessions at most per
connection, 15 min of inactivity.

**"Not before" date** (BR-124). A 429 from the upstream sets a date per connection
(`Retry-After`, otherwise fallback 1 s, 2 s, … 60 s, reset by a success);
before it, `upstream_busy` with no exchange.
It does not touch the OAuth renewal.

**Progress and cancellation** (BR-120, lot C). A `tools/call` whose client carries a progress
token leaves with a token of the relay's own, unique per request; only the upstream's
`notifications/progress` carrying that token are returned (`progress`), in order, any other
notification is discarded; they count in the 1 MiB bound and in the deadline. When the client
closes the stream (`Cancel`), the relay cuts the exchange at once and sends
`notifications/cancelled` to the upstream for that request, on the BR-108 slot of the cancelled
call (separate thread, 2 s at most): `cancelled`, never replayed.

Tests inject their resolver, their TLS context, their `dial` and their clock; the address policy
and the bounds are not injectable (no option lifts them).
"""

from __future__ import annotations

import contextlib
import email.utils
import http.client
import ipaddress
import itertools
import json
import re
import ssl
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol
from urllib.parse import urlsplit

from mcp.shared.inbound import (
    MCP_PARAM_HEADER_PREFIX,
    encode_header_value,
    find_invalid_x_mcp_header,
    x_mcp_header_map,
)

from hullgate import __version__
from hullgate.cimd import public_address, system_resolve
from hullgate.outbound import Failure, _Pinned, default_context, ip_literal, normalize_host

HANDSHAKE_S = 15.0
LIST_S = 15.0
CALL_S = 60.0
CLOSE_S = 2.0
MAX_BYTES = 1024 * 1024
PER_CONNECTION = 4
TOTAL = 32
MAX_TOOLS = 500
MAX_PAGES = 10
SESSIONS_PER_CONNECTION = 64
SESSION_IDLE_S = 15 * 60
HEADER_VALUE_MAX = 8 * 1024
REFUSALS_MAX = 16  # upstream-to-client requests answered per exchange (BR-121, review 462 m4)
BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 60.0
USER_AGENT = "hullgate"
THREAD_NAME = "hullgate-relay"

MODERN = "2026-07-28"
HANDSHAKE_VERSIONS = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)  # most recent first
UNSUPPORTED_PROTOCOL_VERSION = -32022

PROTOCOL_VERSION_META = "io.modelcontextprotocol/protocolVersion"
CLIENT_INFO_META = "io.modelcontextprotocol/clientInfo"
CLIENT_CAPABILITIES_META = "io.modelcontextprotocol/clientCapabilities"

# Closed codes of BR-116 produced by this module.
UNAVAILABLE = "upstream_unavailable"
BUSY = "upstream_busy"
DEADLINE = "deadline_exceeded"
UNCERTAIN = "uncertain_outcome"
TOO_LARGE = "response_too_large"
REQUEST_TOO_LARGE = "request_too_large"
REAUTHORIZE = "connection_reauth_required"
INTERNAL = "internal_error"
# Audit outcome of a call the client cancelled by closing the stream (BR-120, BR-122); never
# returned to the client, which no longer waits for an answer.
CANCELLED = "cancelled"

_SESSION_ID = re.compile(r"^[\x21-\x7e]+$")
_CONTROL = re.compile(r"[\r\n\x00]")
_PRIVATE_NETS = tuple(
    ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)

Resolver = Callable[[str, int], Sequence[str]]
Dial = Callable[[str, int], tuple[str, int]]


class UpstreamError(Exception):
    """Failed exchange with the upstream; `code` is from BR-116's closed list, `reason` a short
    cause with no URL, token or upstream body, `retry_after` the delay of `upstream_busy`."""

    def __init__(self, code: str, reason: str, retry_after: float | None = None) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason
        self.retry_after = retry_after


class Unauthorized(UpstreamError):
    """Upstream 401 received before any content: the tool was not executed (BR-127, lot B)."""

    def __init__(self) -> None:
        super().__init__(REAUTHORIZE, "401 from the MCP server")


class RpcError(Exception):
    """JSON-RPC error returned by the upstream (upstream content, never repeated in a gateway
    message)."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"JSON-RPC error {code}")
        self.code = code
        self.message = message
        self.data = data


# ---------------------------------------------------------------------------------------------
# Address policy (BR-106, BR-107)
# ---------------------------------------------------------------------------------------------


class Cancel:
    """Cancellation of a call by the client (BR-120): set once, from any thread; each registered
    hook is called when it is set (at once if registered after), without blocking."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._set = False
        self._hooks: list[Callable[[], None]] = []

    def is_set(self) -> bool:
        with self._lock:
            return self._set

    def set(self) -> None:
        with self._lock:
            if self._set:
                return
            self._set = True
            hooks, self._hooks = self._hooks, []
        for hook in hooks:
            with contextlib.suppress(Exception):
                hook()

    def hook(self, fn: Callable[[], None]) -> Callable[[], None]:
        """Registers `fn`; returns what unregisters it (end of the exchange)."""
        with self._lock:
            if not self._set:
                self._hooks.append(fn)

                def remove() -> None:
                    with self._lock, contextlib.suppress(ValueError):
                        self._hooks.remove(fn)

                return remove
        with contextlib.suppress(Exception):
            fn()
        return lambda: None


Progress = Callable[[float, float | None, str | None], None]


def private_address(address: str) -> bool:
    """Private address allowed for an internal host: 10/8, 172.16/12, 192.168/16, fc00::/7."""
    text = ip_literal(address)
    if text is None:
        return False
    ip = ipaddress.ip_address(text)
    return any(ip.version == net.version and ip in net for net in _PRIVATE_NETS)


@dataclass(frozen=True, slots=True)
class Target:
    """MCP URL of a service and its internal-host exception (fixed at declaration)."""

    url: str
    internal: bool = False

    def parts(self) -> tuple[str, str, int, str]:
        """(scheme, host, port, path); refusal without repeating the URL."""
        try:
            parts = urlsplit(self.url)
            port = parts.port
        except ValueError:
            raise UpstreamError(UNAVAILABLE, "unreadable MCP URL") from None
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise UpstreamError(UNAVAILABLE, "invalid MCP URL")
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            raise UpstreamError(UNAVAILABLE, "MCP URL with credentials")
        if parts.query or parts.fragment:
            raise UpstreamError(UNAVAILABLE, "MCP URL with query or fragment")
        default = 443 if parts.scheme == "https" else 80
        return parts.scheme, parts.hostname, port or default, parts.path or "/"


def admit(scheme: str, addresses: Sequence[str], internal: bool) -> Callable[[str], bool]:
    """BR-106 guard over the set of resolved addresses; returns the per-address guard replayed
    before each connection, or raises `UpstreamError` (cause "address refused")."""
    if addresses and all(public_address(a) for a in addresses):
        if scheme != "https":
            raise UpstreamError(UNAVAILABLE, "public address: https required")
        return public_address
    if internal and addresses and all(private_address(a) for a in addresses):
        return private_address
    raise UpstreamError(UNAVAILABLE, "address refused")


# ---------------------------------------------------------------------------------------------
# Clock (injectable by tests, AC-05 amended)
# ---------------------------------------------------------------------------------------------


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def wall(self) -> float: ...

    def wait(self, worker: threading.Thread, seconds: float) -> None: ...


class RealClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def wall(self) -> float:
        return time.time()

    def wait(self, worker: threading.Thread, seconds: float) -> None:
        worker.join(seconds)


# ---------------------------------------------------------------------------------------------
# Plain HTTP connection to the verified address (internal host only)
# ---------------------------------------------------------------------------------------------


class _PinnedPlain(_Pinned):
    """Same opening as BR-084's HTTPS connection (addresses in the resolver's order, guard
    replayed, global deadline), without TLS: reserved to an internal host with private addresses."""

    default_port = 80

    def connect(self) -> None:
        self.sock = self._open()


@dataclass(frozen=True, slots=True)
class Reply:
    status: int
    headers: Mapping[str, str]  # lower-case names
    messages: tuple[dict[str, Any], ...]
    size: int

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    def response(self, request_id: int) -> dict[str, Any] | None:
        return next((m for m in self.messages if m.get("id") == request_id), None)


@dataclass
class _Work:
    conn: _Pinned | None = None
    sent: bool = False
    reply: Reply | None = None
    error: UpstreamError | None = None
    cancelled: bool = False
    finished: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def attach(self, conn: _Pinned, expired: threading.Event) -> None:
        with self.lock:
            self.conn = conn
            if expired.is_set() or self.cancelled:
                conn.cut()

    def cancel(self) -> None:
        """The client closed the stream (BR-120): the exchange is cut at once; no effect once
        the exchange is finished (a whole response stays a known outcome)."""
        with self.lock:
            if self.finished:
                return
            self.cancelled = True
            if self.conn is not None:
                self.conn.cut()

    def finish(self) -> bool:
        """Closes the exchange; returns true if it was cancelled after the request was sent,
        without a whole response: the upstream must then receive `notifications/cancelled`
        (BR-120)."""
        with self.lock:
            self.finished = True
            return self.cancelled and self.sent and self.reply is None

    def cut(self) -> None:
        with self.lock:
            if self.conn is not None:
                self.conn.cut()


class _TooLarge(Exception):
    pass


def _sse_messages(buffer: bytes) -> tuple[list[dict[str, Any]], bytes]:
    """Complete SSE events of `buffer` (`data:` lines joined); returns the unfinished rest."""
    out: list[dict[str, Any]] = []
    text = buffer.replace(b"\r\n", b"\n")
    while b"\n\n" in text:
        event, text = text.split(b"\n\n", 1)
        data = [line[5:].lstrip(b" ") for line in event.split(b"\n") if line.startswith(b"data:")]
        if not data:
            continue
        try:
            doc = json.loads(b"\n".join(data))
        except (ValueError, UnicodeDecodeError):
            raise UpstreamError(UNAVAILABLE, "invalid SSE stream") from None
        out.extend(_as_messages(doc))
    return out, text


def _is_request(message: Mapping[str, Any]) -> bool:
    """JSON-RPC request (method and id), as opposed to a response or a notification."""
    return isinstance(message.get("method"), str) and message.get("id") is not None


def _is_notification(message: Mapping[str, Any]) -> bool:
    """JSON-RPC notification: a method, no id."""
    return isinstance(message.get("method"), str) and "id" not in message


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _as_messages(doc: Any) -> list[dict[str, Any]]:
    if isinstance(doc, dict):
        return [doc]
    if isinstance(doc, list) and all(isinstance(m, dict) for m in doc):
        return list(doc)
    raise UpstreamError(UNAVAILABLE, "invalid JSON-RPC response")


@dataclass
class _Pace:
    not_before: float = 0.0
    step: int = 0


class Pacer:
    """ "Not before" date of a connection's calls after an upstream 429 (BR-124); shared by every
    role, in memory, separate from the OAuth renewal's (BR-094)."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._paces: dict[str, _Pace] = {}

    def check(self, connection_id: str) -> None:
        with self._lock:
            pace = self._paces.get(connection_id)
            if pace is None:
                return
            remaining = pace.not_before - self._clock.wall()
        if remaining > 0:
            raise UpstreamError(BUSY, "429 from the upstream", retry_after=remaining)

    def limited(self, connection_id: str, retry_after: float | None) -> float:
        with self._lock:
            pace = self._paces.setdefault(connection_id, _Pace())
            if retry_after is None:
                delay = min(BACKOFF_MAX_S, BACKOFF_MIN_S * float(2 ** min(pace.step, 16)))
                pace.step += 1
            else:
                delay = max(0.0, retry_after)
            pace.not_before = self._clock.wall() + delay
            return delay

    def succeeded(self, connection_id: str) -> None:
        with self._lock:
            self._paces.pop(connection_id, None)


def retry_after(value: str | None, now: float) -> float | None:
    """`Retry-After` in seconds or as an HTTP date; None if absent or unreadable."""
    if value is None:
        return None
    text = value.strip()
    if text.isdigit():
        return float(int(text))
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - now)


class Transport:
    """Bounded HTTP exchanges toward MCP upstreams, a single one per process (BR-125)."""

    built: ClassVar[int] = 0  # construction counter (AC-08)

    def __init__(
        self,
        *,
        context: ssl.SSLContext | None = None,
        resolve: Resolver = system_resolve,
        dial: Dial | None = None,
        clock: Clock | None = None,
    ) -> None:
        Transport.built += 1
        self.context = context if context is not None else default_context()
        self.resolve = resolve
        self.dial: Dial = dial if dial is not None else (lambda a, p: (a, p))
        self.clock: Clock = clock if clock is not None else RealClock()
        self.pacer = Pacer(self.clock)
        self._total = threading.BoundedSemaphore(TOTAL)
        self._per: dict[str, threading.BoundedSemaphore] = {}
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return "Transport()"

    # -- addresses -----------------------------------------------------------------------------

    def addresses(self, target: Target) -> list[str]:
        """Resolved and admitted addresses (all verified); serves `service upstream`."""
        scheme, host, port, _ = target.parts()
        addresses = self._resolve(host, port)
        admit(scheme, addresses, target.internal)
        return addresses

    def _resolve(self, host: str, port: int) -> list[str]:
        literal = ip_literal(host)
        if literal is not None:
            return [literal]
        try:
            found = list(dict.fromkeys(self.resolve(normalize_host(host), port)))
        except (OSError, UnicodeError):
            raise UpstreamError(UNAVAILABLE, "name resolution failed") from None
        if not found:
            raise UpstreamError(UNAVAILABLE, "empty name resolution")
        return found

    # -- concurrency (BR-108) -----------------------------------------------------------------

    def _acquire(self, connection_id: str) -> threading.BoundedSemaphore:
        with self._lock:
            per = self._per.setdefault(connection_id, threading.BoundedSemaphore(PER_CONNECTION))
        if not per.acquire(blocking=False):
            raise UpstreamError(BUSY, "per-connection cap")
        if not self._total.acquire(blocking=False):
            per.release()
            raise UpstreamError(BUSY, "global cap")
        return per

    # -- exchange ------------------------------------------------------------------------------

    def exchange(
        self,
        connection_id: str,
        target: Target,
        method: str,
        headers: Mapping[str, str],
        body: bytes | None,
        bound: float,
        expect_id: int | None = None,
        on_request: Callable[[dict[str, Any]], None] | None = None,
        on_message: Callable[[dict[str, Any]], None] | None = None,
        cancel: Cancel | None = None,
        notice: tuple[Mapping[str, str], bytes] | None = None,
    ) -> Reply:
        """One bounded exchange; every HTTP response is returned (classified by the caller),
        every departure from BR-107 is an `UpstreamError`. `on_request` receives, as the stream
        goes, each request the upstream addresses to the client (BR-121), `on_message` each
        notification (BR-120); `cancel` cuts the exchange as soon as it is set (`cancelled`,
        BR-120) and `notice` (headers, body of `notifications/cancelled`) then leaves toward the
        upstream **on the slot of the cancelled exchange**, handed to the sending thread then
        released: no extra slot, no notice lost at 4/32 (BR-108, review 470 B1). Blocking."""
        for name, value in headers.items():
            if _CONTROL.search(name) or _CONTROL.search(value):
                raise UpstreamError(INTERNAL, "control byte in a header")
        scheme, host, port, path = target.parts()
        if cancel is not None and cancel.is_set():
            raise UpstreamError(CANCELLED, "cancelled by the client")
        self.pacer.check(connection_id)
        per = self._acquire(connection_id)
        work = _Work()
        expired = threading.Event()
        deadline = time.monotonic() + bound  # socket deadline; the wait goes through the clock

        def run() -> None:
            try:
                work.reply = self._exchange(
                    scheme, host, port, path, method, headers, body, target.internal,
                    work, expired, deadline, expect_id, on_request, on_message,
                )  # fmt: skip
            except UpstreamError as exc:
                work.error = exc
            except _TooLarge:
                work.error = UpstreamError(TOO_LARGE, "response over 1 MiB")
            except Failure as exc:  # address guard replayed at connection
                work.error = UpstreamError(UNAVAILABLE, exc.reason.split(":")[0])
            except Exception:  # cut, deadline, connection refused, TLS
                work.error = self._classify(work, expired)
            finally:
                if work.finish() and notice is not None:
                    self._notify(
                        connection_id, scheme, host, port, path, target.internal, notice, per
                    )
                else:
                    self._total.release()
                    per.release()

        worker = threading.Thread(target=run, name=THREAD_NAME, daemon=True)
        try:
            worker.start()
        except RuntimeError:
            self._total.release()
            per.release()
            raise UpstreamError(BUSY, "thread unavailable") from None
        remove = cancel.hook(work.cancel) if cancel is not None else None
        try:
            self.clock.wait(worker, bound)
        finally:
            if remove is not None:
                remove()
        if work.cancelled and work.reply is None:  # cut by the client: no outcome, no retry
            raise UpstreamError(CANCELLED, "cancelled by the client")
        if worker.is_alive():
            expired.set()
            work.cut()
            raise self._classify(work, expired)
        if work.error is not None:
            raise work.error
        if work.reply is None:
            raise UpstreamError(UNAVAILABLE, "no response")
        return work.reply

    def _notify(
        self,
        connection_id: str,
        scheme: str,
        host: str,
        port: int,
        path: str,
        internal: bool,
        notice: tuple[Mapping[str, str], bytes],
        per: threading.BoundedSemaphore,
    ) -> None:
        """`notifications/cancelled` sent in a separate thread, on the slots (connection and
        total) of the cancelled exchange; best effort, response ignored. A **hard** deadline of
        2 s on the whole notice (BR-107, review 473 B2): past `CLOSE_S`, the notice's exchange is
        cut; the slots are released exactly once, at the end of the notice or at its cut. Worst
        case: if the cut has no effect (blocked name resolution, which `cut()` does not
        interrupt), the cut is waited for at most `CLOSE_S` more: the slots are released at
        2 × `CLOSE_S` (4 s) at most, the sending thread possibly surviving but no longer able to
        open a connection (`expired`, review 475 n1)."""
        headers, body = notice
        released = threading.Lock()

        def release() -> None:
            if released.acquire(blocking=False):  # once, whatever the path
                self._total.release()
                per.release()

        def send(work: _Work, expired: threading.Event, deadline: float) -> None:
            with contextlib.suppress(Exception):  # best effort: the call is already cut
                self._exchange(scheme, host, port, path, "POST", headers, body, internal,
                               work, expired, deadline, None)  # fmt: skip

        def run() -> None:
            try:
                # same checks as an exchange: headers, "not before" date (review 473, n1)
                if any(_CONTROL.search(k) or _CONTROL.search(v) for k, v in headers.items()):
                    return
                try:
                    self.pacer.check(connection_id)
                except UpstreamError:
                    return
                work, expired = _Work(), threading.Event()
                deadline = time.monotonic() + CLOSE_S
                sender = threading.Thread(
                    target=send, args=(work, expired, deadline), name=THREAD_NAME, daemon=True
                )
                sender.start()
                sender.join(CLOSE_S)
                if sender.is_alive():  # hard deadline of the notice: cut
                    expired.set()
                    work.cut()
                    sender.join(CLOSE_S)
            except Exception:  # noqa: S110 - best effort; the slots are released below
                pass
            finally:
                release()

        try:
            threading.Thread(target=run, name=THREAD_NAME, daemon=True).start()
        except RuntimeError:
            release()

    @staticmethod
    def _classify(work: _Work, expired: threading.Event) -> UpstreamError:
        if not work.sent:
            return UpstreamError(UNAVAILABLE, "request not sent")
        if expired.is_set():
            return UpstreamError(DEADLINE, "deadline exceeded")
        return UpstreamError(UNCERTAIN, "response interrupted")

    def _exchange(
        self,
        scheme: str,
        host: str,
        port: int,
        path: str,
        method: str,
        headers: Mapping[str, str],
        body: bytes | None,
        internal: bool,
        work: _Work,
        expired: threading.Event,
        deadline: float,
        expect_id: int | None,
        on_request: Callable[[dict[str, Any]], None] | None = None,
        on_message: Callable[[dict[str, Any]], None] | None = None,
    ) -> Reply:
        addresses = self._resolve(host, port)
        guard = admit(scheme, addresses, internal)
        if expired.is_set():
            raise UpstreamError(UNAVAILABLE, "deadline exceeded")
        kind = _Pinned if scheme == "https" else _PinnedPlain
        conn = kind(normalize_host(host), port, addresses, guard, self.dial, self.context, deadline)
        work.attach(conn, expired)
        try:
            conn.putrequest(method, path, skip_accept_encoding=True)
            for name, value in headers.items():
                conn.putheader(name, value)
            if body is not None:
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
            work.sent = True
            response = conn.getresponse()
            got = {k.lower(): v for k, v in response.getheaders()}
            if response.status != 200:
                return Reply(response.status, got, (), 0)  # body never read nor repeated
            length = got.get("content-length")
            if length is not None and length.isdigit() and int(length) > MAX_BYTES:
                raise _TooLarge
            ctype = got.get("content-type", "").split(";")[0].strip().lower()
            if ctype == "text/event-stream":
                messages, size = self._read_sse(response, expect_id, on_request, on_message)
            elif ctype == "application/json":
                data = self._read_all(response)
                size = len(data)
                if length is not None and length.isdigit() and size < int(length):
                    raise ConnectionError("body interrupted")  # sent: uncertain outcome
                try:
                    messages = _as_messages(json.loads(data)) if data.strip() else []
                except (ValueError, UnicodeDecodeError):
                    raise UpstreamError(UNAVAILABLE, "non-JSON response") from None
            else:
                raise UpstreamError(UNAVAILABLE, "unexpected response type")
            return Reply(response.status, got, tuple(messages), size)
        finally:
            conn.cut()
            conn.close()

    @staticmethod
    def _read_all(response: http.client.HTTPResponse) -> bytes:
        data = b""
        while True:
            chunk = response.read1(8192)
            if not chunk:
                return data
            data += chunk
            if len(data) > MAX_BYTES:
                raise _TooLarge

    @staticmethod
    def _read_sse(
        response: http.client.HTTPResponse,
        expect_id: int | None,
        on_request: Callable[[dict[str, Any]], None] | None = None,
        on_message: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Reads the stream up to the expected response (or the end of the stream), under the
        size bound. The upstream's notifications are read and counted, passed to `on_message` in
        stream order (which returns only the call's progress, BR-120); an upstream-to-client
        request is passed to `on_request` as soon as it is read (BR-121)."""
        messages: list[dict[str, Any]] = []
        pending = b""
        size = 0
        while True:
            chunk = response.read1(8192)
            if not chunk:
                if expect_id is not None:
                    raise ConnectionError("stream interrupted")  # sent: uncertain outcome
                return messages, size
            size += len(chunk)
            if size > MAX_BYTES:
                raise _TooLarge
            found, pending = _sse_messages(pending + chunk)
            messages.extend(found)
            for m in found:
                if on_request is not None and _is_request(m) and m.get("id") != expect_id:
                    on_request(m)
                elif on_message is not None and _is_notification(m):
                    on_message(m)
            if expect_id is not None and any(m.get("id") == expect_id for m in found):
                return messages, size


# ---------------------------------------------------------------------------------------------
# Headers of the 2026-07-28 generation (BR-107 amended)
# ---------------------------------------------------------------------------------------------


def _render(value: Any) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        try:
            return str(value)
        except ValueError:  # integer beyond the conversion limit
            return None
    return None


def _value_at(arguments: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = arguments
    for key in path:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


def param_headers(input_schema: Any, arguments: Mapping[str, Any]) -> dict[str, str]:
    """`Mcp-Param-<name>` of a `tools/call`, drawn from the arguments sent; a schema with invalid
    annotations emits none (as a conforming client). An annotated argument that does not encode
    raises `request_too_large` (BR-107 amended)."""
    if find_invalid_x_mcp_header(input_schema) is not None:
        return {}
    headers: dict[str, str] = {}
    for path, token in x_mcp_header_map(input_schema).items():
        value = _value_at(arguments, path)
        if value is None:
            continue
        rendered = _render(value)
        if rendered is None:
            raise UpstreamError(REQUEST_TOO_LARGE, "annotated argument not encodable as a header")
        encoded = encode_header_value(rendered)
        if len(encoded) > HEADER_VALUE_MAX:
            raise UpstreamError(REQUEST_TOO_LARGE, "annotated argument too long for a header")
        headers[f"{MCP_PARAM_HEADER_PREFIX}{token}"] = encoded
    return headers


# ---------------------------------------------------------------------------------------------
# Sessions and generations (BR-118, BR-119)
# ---------------------------------------------------------------------------------------------


Key = tuple[str, str]  # (role, connection)


@dataclass(eq=False)
class Session:
    key: Key
    target: Target
    version: str
    session_id: str | None
    epoch: int
    authorization: str = field(repr=False)
    last_used: float = 0.0

    @property
    def url(self) -> str:
        return self.target.url


@dataclass(frozen=True, slots=True)
class ToolList:
    version: str
    tools: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class CallResult:
    version: str
    result: dict[str, Any]


class _SessionGone(Exception):
    pass


def _client_info() -> dict[str, Any]:
    return {"name": "hullgate", "version": __version__}


class UpstreamClient:
    """The process's upstream MCP client (BR-125): sessions per (role, connection), generations,
    bounded pagination. Blocking; the server calls it outside the event loop."""

    def __init__(self, transport: Transport) -> None:
        self.transport = transport
        self._sessions: dict[Key, Session] = {}
        self._opening: dict[Key, threading.Lock] = {}
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._epochs = itertools.count(1)
        self.closed: list[Key] = []  # closed keys (read by tests and audit)
        # BR-121: called once per exchange where the upstream addressed requests to the client,
        # with the method of the first and their count (review 462, m4)
        self.on_upstream_request: Callable[[Key, str, int], None] | None = None

    def __repr__(self) -> str:
        return "UpstreamClient()"

    # -- API -----------------------------------------------------------------------------------

    def list_tools(
        self, key: Key, target: Target, authorization: str, *, max_tools: int = MAX_TOOLS
    ) -> ToolList:
        """Complete upstream list (pagination followed up to 500 tools and 10 pages, BR-109); an
        overflow raises `UpstreamError(upstream_unavailable, "pagination")`."""
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        session: Session | None = None
        for _ in range(MAX_PAGES):
            params: dict[str, Any] = {} if cursor is None else {"cursor": cursor}
            session, result = self._request(
                key, target, authorization, "tools/list", params, LIST_S
            )
            page = result.get("tools")
            if not isinstance(page, list) or not all(isinstance(t, dict) for t in page):
                raise UpstreamError(UNAVAILABLE, "invalid tool list")
            tools.extend(page)
            if len(tools) > max_tools:
                raise UpstreamError(UNAVAILABLE, "pagination")
            nxt = result.get("nextCursor")
            if not nxt:
                return ToolList(session.version, tuple(tools))
            if not isinstance(nxt, str):
                raise UpstreamError(UNAVAILABLE, "invalid tool list")
            cursor = nxt
        raise UpstreamError(UNAVAILABLE, "pagination")

    def call_tool(
        self,
        key: Key,
        target: Target,
        authorization: str,
        name: str,
        arguments: Mapping[str, Any],
        input_schema: Any = None,
        *,
        progress: Progress | None = None,
        cancel: Cancel | None = None,
    ) -> CallResult:
        """`progress`: the client carries a progress token; the upstream's progress for this
        call is returned to it (BR-120). `cancel`: the client may close the stream (BR-120)."""
        params = {"name": name, "arguments": dict(arguments)}
        session, result = self._request(
            key, target, authorization, "tools/call", params, CALL_S, input_schema=input_schema,
            progress=progress, cancel=cancel,
        )  # fmt: skip
        return CallResult(session.version, result)

    def close(self, key: Key, *, wait: bool = False) -> None:
        """Closes the key's session (explicit end sent, 2 s at most). `wait`: wait for the end
        to be sent (at most `CLOSE_S`), for a process that exits right after (CLI)."""
        with self._lock:
            session = self._sessions.pop(key, None)
        if session is not None:
            self._end(session, wait=wait)

    def close_where(self, keep: Callable[[Key, str], bool]) -> list[Key]:
        """Closes every session for which `keep(key, url)` is false; returns the closed keys."""
        with self._lock:
            gone = [s for k, s in self._sessions.items() if not keep(k, s.url)]
            for s in gone:
                del self._sessions[s.key]
        for s in gone:
            self._end(s)
        return [s.key for s in gone]

    def close_all(self) -> None:
        self.close_where(lambda _k, _u: False)

    def held(self) -> list[tuple[Key, str]]:
        with self._lock:
            return [(k, s.url) for k, s in self._sessions.items()]

    def session(self, key: Key) -> Session | None:
        with self._lock:
            return self._sessions.get(key)

    # -- request with session retry --------------------------------------------------------

    def _request(
        self,
        key: Key,
        target: Target,
        authorization: str,
        method: str,
        params: dict[str, Any],
        bound: float,
        *,
        input_schema: Any = None,
        progress: Progress | None = None,
        cancel: Cancel | None = None,
    ) -> tuple[Session, dict[str, Any]]:
        self._sweep()
        session = self._session(key, target, authorization, None)
        try:
            return session, self._send(session, target, authorization, method, params, bound,
                                       input_schema, progress, cancel)  # fmt: skip
        except _SessionGone:
            session = self._session(key, target, authorization, session.epoch)
            try:
                return session, self._send(
                    session, target, authorization, method, params, bound, input_schema,
                    progress, cancel,
                )  # fmt: skip
            except _SessionGone:
                with self._lock:
                    if self._sessions.get(key) is session:
                        del self._sessions[key]
                raise UpstreamError(UNAVAILABLE, "session") from None

    def _session(self, key: Key, target: Target, authorization: str, stale: int | None) -> Session:
        """Live session of the key; opened only once per key (concurrent calls wait for the
        same opening). `stale`: epoch the upstream declared unknown."""
        with self._lock:
            opening = self._opening.setdefault(key, threading.Lock())
        with opening:
            with self._lock:
                current = self._sessions.get(key)
            if current is not None and current.target == target and current.epoch != stale:
                current.authorization = authorization
                current.last_used = self.transport.clock.monotonic()
                return current
            if current is not None and current.epoch == stale:
                with self._lock:  # the upstream declared it unknown: nothing to close on its side
                    self._sessions.pop(key, None)
            elif current is not None:
                self._drop(current)
            opened = self._open(key, target, authorization)
            with self._lock:
                self._sessions[key] = opened
                self._cap(key[1])
            return opened

    def _drop(self, session: Session) -> None:
        with self._lock:
            if self._sessions.get(session.key) is session:
                del self._sessions[session.key]
        if session.session_id is not None:
            self._end(session)

    def _cap(self, connection_id: str) -> None:
        """64 sessions at most per connection: the oldest is closed (under `_lock`)."""
        mine = sorted(
            (s for k, s in self._sessions.items() if k[1] == connection_id),
            key=lambda s: s.last_used,
        )
        for s in mine[: max(0, len(mine) - SESSIONS_PER_CONNECTION)]:
            del self._sessions[s.key]
            threading.Thread(target=self._end, args=(s,), name=THREAD_NAME, daemon=True).start()

    def _sweep(self) -> None:
        now = self.transport.clock.monotonic()
        with self._lock:
            idle = [s for s in self._sessions.values() if now - s.last_used > SESSION_IDLE_S]
            for s in idle:
                del self._sessions[s.key]
        for s in idle:
            self._end(s)

    # -- opening: generation (BR-118) --------------------------------------------------------

    def _open(self, key: Key, target: Target, authorization: str) -> Session:
        now = self.transport.clock.monotonic()
        epoch = next(self._epochs)
        try:
            self._discover(key[1], target, authorization)
            return Session(key, target, MODERN, None, epoch, authorization, now)
        except _Legacy:
            pass
        version, session_id = self._initialize(key[1], target, authorization)
        session = Session(key, target, version, session_id, epoch, authorization, now)
        reply = self._post(session, target, authorization,
                           {"jsonrpc": "2.0", "method": "notifications/initialized"},
                           HANDSHAKE_S, None)  # fmt: skip
        if reply.status not in (200, 202):
            self._status(reply, key[1])
        return session

    def _discover(self, connection_id: str, target: Target, authorization: str) -> None:
        rid = next(self._ids)
        body = {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "server/discover",
            "params": {"_meta": self._envelope(MODERN)},
        }
        headers = self._headers(authorization, MODERN, None, "server/discover", None)
        reply = self.transport.exchange(
            connection_id, target, "POST", headers, _dump(body), HANDSHAKE_S, rid
        )
        if reply.status in (401, 429):
            self._status(reply, connection_id)
        if reply.status != 200:
            raise _Legacy  # any HTTP refusal of the probe: handshake generation
        message = reply.response(rid)
        if message is None:
            raise _Legacy
        error = message.get("error")
        if isinstance(error, dict):
            if error.get("code") == UNSUPPORTED_PROTOCOL_VERSION:
                data = error.get("data")
                supported = data.get("supported") if isinstance(data, dict) else None
                if isinstance(supported, list) and not any(
                    v in HANDSHAKE_VERSIONS or v == MODERN for v in supported
                ):
                    raise UpstreamError(UNAVAILABLE, "version")
            raise _Legacy
        result = message.get("result")
        versions = result.get("supportedVersions") if isinstance(result, dict) else None
        if not isinstance(versions, list) or MODERN not in versions:
            raise _Legacy
        self.transport.pacer.succeeded(connection_id)

    def _initialize(
        self, connection_id: str, target: Target, authorization: str
    ) -> tuple[str, str | None]:
        rid = next(self._ids)
        body = {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "initialize",
            "params": {
                "protocolVersion": HANDSHAKE_VERSIONS[0],
                "capabilities": {},  # no client capability announced (BR-121)
                "clientInfo": _client_info(),
            },
        }
        headers = self._headers(authorization, None, None, None, None)
        reply = self.transport.exchange(
            connection_id, target, "POST", headers, _dump(body), HANDSHAKE_S, rid
        )
        if reply.status != 200:
            self._status(reply, connection_id)
        message = reply.response(rid)
        if message is None:
            raise UpstreamError(UNAVAILABLE, "invalid response")
        result = message.get("result")
        version = result.get("protocolVersion") if isinstance(result, dict) else None
        if version not in HANDSHAKE_VERSIONS:
            raise UpstreamError(UNAVAILABLE, "version")
        session_id = reply.header("mcp-session-id")
        if session_id is not None and not _SESSION_ID.match(session_id):
            raise UpstreamError(UNAVAILABLE, "session")
        self.transport.pacer.succeeded(connection_id)
        return str(version), session_id

    # -- send ---------------------------------------------------------------------------------

    @staticmethod
    def _envelope(version: str) -> dict[str, Any]:
        return {
            PROTOCOL_VERSION_META: version,
            CLIENT_INFO_META: _client_info(),
            CLIENT_CAPABILITIES_META: {},
        }

    @staticmethod
    def _headers(
        authorization: str,
        version: str | None,
        session_id: str | None,
        method: str | None,
        name: str | None,
    ) -> dict[str, str]:
        """BR-107's closed list (amended); `Mcp-Method` and `Mcp-Name` come from the body."""
        headers = {
            "Authorization": authorization,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": USER_AGENT,
        }
        if version is not None and version != "2024-11-05":
            headers["MCP-Protocol-Version"] = version
        if session_id is not None:
            headers["Mcp-Session-Id"] = session_id
        if version == MODERN and method is not None:
            headers["Mcp-Method"] = method
            if name is not None:
                headers["Mcp-Name"] = encode_header_value(name)
        return headers

    def _send(
        self,
        session: Session,
        target: Target,
        authorization: str,
        method: str,
        params: dict[str, Any],
        bound: float,
        input_schema: Any,
        progress: Progress | None = None,
        cancel: Cancel | None = None,
    ) -> dict[str, Any]:
        rid = next(self._ids)
        sent = dict(params)
        name: str | None = None
        extra: dict[str, str] = {}
        if session.version == MODERN:
            sent["_meta"] = self._envelope(MODERN)
            if method == "tools/call":
                name = str(params["name"])
                extra = param_headers(input_schema, params.get("arguments") or {})
        # BR-120: progress token of the relay's own, unique per request, sent only when the
        # client carries one; outside the arguments (BR-115)
        own = f"hullgate-{rid}" if progress is not None and method == "tools/call" else None
        if own is not None:
            sent["_meta"] = {**sent.get("_meta", {}), "progressToken": own}
        body = {"jsonrpc": "2.0", "id": rid, "method": method, "params": sent}
        refuser = _Refuser(self, session, target, authorization)

        def note(message: dict[str, Any]) -> None:
            if own is not None and progress is not None:
                _relay_progress(message, own, progress)

        notice = None
        if cancel is not None and method == "tools/call":
            # BR-120: when the client closes the stream, the exchange is cut and
            # `notifications/cancelled` leaves for `rid`, on the slot of the cancelled call
            notice = self._cancel_notice(session, authorization, rid)
        try:
            reply = self._post(session, target, authorization, body, bound, rid, name, extra,
                               refuser, note, cancel, notice)  # fmt: skip
            if not (reply.header("content-type") or "").startswith("text/event-stream"):
                for m in reply.messages:  # JSON response: read afterwards (BR-120, BR-121)
                    if _is_request(m) and m.get("id") != rid:
                        refuser(m)
                    elif _is_notification(m):
                        note(m)
        finally:
            refuser.finish()
        connection_id = session.key[1]
        if reply.status == 404 and session.session_id is not None:
            raise _SessionGone
        if reply.status != 200:
            self._status(reply, connection_id)
        message = reply.response(rid)
        if message is None:
            raise UpstreamError(UNAVAILABLE, "invalid response")
        self.transport.pacer.succeeded(connection_id)
        error = message.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            raise RpcError(code if isinstance(code, int) else 0, str(error.get("message", "")),
                           error.get("data"))  # fmt: skip
        result = message.get("result")
        if not isinstance(result, dict):
            raise UpstreamError(UNAVAILABLE, "invalid response")
        return result

    def _post(
        self,
        session: Session,
        target: Target,
        authorization: str,
        body: dict[str, Any],
        bound: float,
        rid: int | None,
        name: str | None = None,
        extra: Mapping[str, str] | None = None,
        on_request: Callable[[dict[str, Any]], None] | None = None,
        on_message: Callable[[dict[str, Any]], None] | None = None,
        cancel: Cancel | None = None,
        notice: tuple[Mapping[str, str], bytes] | None = None,
    ) -> Reply:
        headers = self._headers(
            authorization, session.version, session.session_id, str(body["method"]), name
        )
        headers.update(extra or {})
        return self.transport.exchange(
            session.key[1], target, "POST", headers, _dump(body), bound, rid, on_request,
            on_message, cancel, notice,
        )  # fmt: skip

    def _answer(
        self, session: Session, target: Target, authorization: str, request: Mapping[str, Any]
    ) -> None:
        """BR-121: error answer to an upstream-to-client request (2 s at most, best effort);
        played by the exchange's answer thread (`_Refuser`)."""
        answer = {
            "jsonrpc": "2.0",
            "id": request.get("id"),
            "error": {"code": -32601, "message": "Method not found"},
        }
        headers = self._headers(authorization, session.version, session.session_id, None, None)
        with contextlib.suppress(UpstreamError):
            self.transport.exchange(session.key[1], target, "POST", headers, _dump(answer), CLOSE_S)

    def _cancel_notice(
        self, session: Session, authorization: str, rid: int
    ) -> tuple[Mapping[str, str], bytes]:
        """BR-120: headers and body of `notifications/cancelled` for request `rid` (same
        session, BR-107's closed list); in 2026-07-28, cutting the stream is what cancels, and
        the notice leaves all the same."""
        note = {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": rid, "reason": "client disconnected"},
        }
        headers = self._headers(authorization, session.version, session.session_id,
                                "notifications/cancelled", None)  # fmt: skip
        return headers, _dump(note)

    def _status(self, reply: Reply, connection_id: str) -> None:
        """HTTP status other than 200: 401 before any content, 429 ("not before" date),
        otherwise `upstream_unavailable` (3xx never followed, 403, 404 outside a session, 5xx)."""
        if reply.status == 401:
            raise Unauthorized
        if reply.status == 429:
            wait = retry_after(reply.header("retry-after"), self.transport.clock.wall())
            delay = self.transport.pacer.limited(connection_id, wait)
            raise UpstreamError(BUSY, "429 from the upstream", retry_after=delay)
        raise UpstreamError(UNAVAILABLE, f"status {reply.status}")

    def _end(self, session: Session, *, wait: bool = False) -> None:
        """Explicit end of session (`DELETE`), 2 s at most, without waiting for the response
        beyond; best effort: an unavailable slot or a failure blocks nothing. `wait` joins the
        sending thread (bounded by `CLOSE_S`, the exchange being itself bounded to that time)."""
        self.closed.append(session.key)
        if session.session_id is None:
            return
        headers = self._headers(session.authorization, session.version, session.session_id,
                                None, None)  # fmt: skip
        del headers["Content-Type"]
        target = session.target

        def run() -> None:
            with contextlib.suppress(UpstreamError):
                self.transport.exchange(session.key[1], target, "DELETE", headers, None, CLOSE_S)

        sender = threading.Thread(target=run, name=THREAD_NAME, daemon=True)
        sender.start()
        if wait:
            sender.join(CLOSE_S + 0.5)


class _Refuser:
    """Upstream-to-client requests read during an exchange (BR-121): never forwarded; the
    first `REFUSALS_MAX` receive an error answer, in order, from a single thread per exchange; a
    single audit entry per exchange, with the method of the first and their count (review 462,
    m4: an upstream that emits thousands opens neither thousands of threads nor thousands of
    audit lines, and holds only one BR-108 slot at a time)."""

    def __init__(
        self, client: UpstreamClient, session: Session, target: Target, authorization: str
    ) -> None:
        self.client = client
        self.session = session
        self.target = target
        self.authorization = authorization
        self.count = 0
        self.first: str | None = None
        self.pending: list[Mapping[str, Any]] = []
        self.lock = threading.Lock()
        self.draining = False

    def __call__(self, request: Mapping[str, Any]) -> None:
        with self.lock:
            self.count += 1
            if self.first is None:
                self.first = str(request.get("method"))
            if self.count > REFUSALS_MAX:
                return
            self.pending.append(request)
            if self.draining:
                return
            self.draining = True
        threading.Thread(target=self._drain, name=THREAD_NAME, daemon=True).start()

    def _drain(self) -> None:
        while True:
            with self.lock:
                if not self.pending:
                    self.draining = False
                    return
                request = self.pending.pop(0)
            self.client._answer(self.session, self.target, self.authorization, request)

    def finish(self) -> None:
        hook = self.client.on_upstream_request
        if self.count and hook is not None:
            with contextlib.suppress(Exception):
                hook(self.session.key, self.first or "", self.count)


def _relay_progress(message: Mapping[str, Any], own: str, progress: Progress) -> None:
    """BR-120: only a `notifications/progress` carrying the relay's token for this call is
    returned, values read and typed; any other notification is discarded."""
    if message.get("method") != "notifications/progress":
        return
    params = message.get("params")
    if not isinstance(params, dict) or params.get("progressToken") != own:
        return
    value = _number(params.get("progress"))
    if value is None:
        return
    total = _number(params.get("total"))
    text = params.get("message")
    with contextlib.suppress(Exception):  # a client gone does not interrupt reading the stream
        progress(value, total, text if isinstance(text, str) else None)


class _Legacy(Exception):
    """The `server/discover` probe brings no proof of a 2026-07-28 upstream."""


def _dump(body: Mapping[str, Any]) -> bytes:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
