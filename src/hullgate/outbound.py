"""Bounded outbound calls toward an OAuth provider (BR-084, F-006 lot B).

Discovery, registration, code exchange, account reading, renewal and revocation go through here.
Same mechanism as BR-062's fetcher (`cimd.py`), extended to POST and to headers: `http.client`,
`socket`, `ssl`, one worker thread per exchange, connection to the verified address (the name
serves only for SNI and the certificate), a hard deadline of 10 s on the whole exchange (name
resolution included), 256 KiB of response at most, no redirect followed.

Dialled addresses (HULL-32): the admitted addresses are tried one after the other, in the order
returned by the resolver (`getaddrinfo`, the system's RFC 6724 policy), up to the first TCP
connection established; each attempt is bounded by the time left of the global deadline, never
by a deadline of its own. No re-resolution between admission and dialling.

Addresses: every resolved address of a name must be public. The only exception, named by the
operator at the service's declaration (`--internal-host`): the host **and** the port of the
record's base URL, compared after normalization, may resolve to a private address, never to
loopback, link-local, the unspecified address or multicast.

No environment variable is read: neither proxy (`http.client` reads none) nor certificate store
(`SSL_CERT_FILE`, `SSL_CERT_DIR`: the default context loads OpenSSL's compiled paths, not the
environment's). Tests inject their resolver, their TLS context and a `dial` function that
redirects the verified address to their local server; the address policy is not injectable.

Classification of failures (BR-092's table): `transient` when the request was not fully sent
(DNS, connection, TLS), `uncertain` when it was sent and the response is missing (deadline, cut),
`anomaly` for a refused address, an `http://`, a response too large.
"""

from __future__ import annotations

import contextlib
import http.client
import ipaddress
import json
import os
import socket
import ssl
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar
from urllib.parse import urlencode, urlsplit

from hullgate.cimd import public_address, system_resolve

DEADLINE_S = 10.0
MAX_BYTES = 256 * 1024
MAX_WORKERS = 16
THREAD_NAME = "hullgate-oauth"  # OAuth exchange threads, distinct from the relay's (BR-108)

TRANSIENT = "transient"
UNCERTAIN = "uncertain"
ANOMALY = "anomaly"

Resolver = Callable[[str, int], Sequence[str]]
Dial = Callable[[str, int], tuple[str, int]]


class Failure(Exception):
    """Failed outbound call; `kind` is the outcome of BR-092's table, `reason` a short cause with
    no URL and no value."""

    def __init__(self, kind: str, reason: str) -> None:
        super().__init__(f"{kind}: {reason}")
        self.kind = kind
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    headers: Mapping[str, str]  # lower-case names
    body: bytes

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    def json(self) -> dict[str, Any]:
        try:
            doc = json.loads(self.body)
        except (ValueError, UnicodeDecodeError):
            raise Failure(ANOMALY, "non-JSON response") from None
        if not isinstance(doc, dict):
            raise Failure(ANOMALY, "JSON response that is not an object")
        return doc


# ---------------------------------------------------------------------------------------------
# Normalization and address policy
# ---------------------------------------------------------------------------------------------


def ip_literal(host: str) -> str | None:
    """Canonical form of an IP literal, including the abbreviated or hexadecimal spellings of IPv4
    (`127.1`, `0x7f000001`) and IPv4 inside IPv6 (`::ffff:127.0.0.1`); None for a name."""
    text = host.strip().strip("[]").rstrip(".")
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        if not text or not all(c in "0123456789abcdefABCDEFxX." for c in text):
            return None
        try:  # forms accepted by inet_aton: 127.1, 0x7f000001, 0177.0.0.1
            return socket.inet_ntoa(socket.inet_aton(text))
        except OSError:
            return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    return str(ip)


def normalize_host(host: str) -> str:
    """Lower case, trailing dot removed, IDNA, canonical IP literal (BR-084)."""
    text = host.strip().lower().rstrip(".")
    literal = ip_literal(text)
    if literal is not None:
        return literal
    try:
        return text.encode("idna").decode("ascii")
    except UnicodeError:
        return text


@dataclass(frozen=True, slots=True)
class InternalHost:
    """Internal-host exception of a service record: normalized host and port."""

    host: str
    port: int

    @classmethod
    def of(cls, url: str) -> InternalHost:
        parts = urlsplit(url)
        return cls(normalize_host(parts.hostname or ""), parts.port or 443)


def internal_address(address: str) -> bool:
    """Address allowed for an internal host: private permitted, never loopback, link-local,
    unspecified, multicast or reserved."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not (
        ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved
    )


def default_context() -> ssl.SSLContext:
    """Verifying TLS context, loaded from OpenSSL's compiled paths and not from the environment
    (`SSL_CERT_FILE` and `SSL_CERT_DIR` are ignored, BR-084)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    paths = ssl.get_default_verify_paths()
    cafile = paths.openssl_cafile if os.path.isfile(paths.openssl_cafile) else None
    capath = paths.openssl_capath if os.path.isdir(paths.openssl_capath) else None
    if cafile is not None or capath is not None:
        ctx.load_verify_locations(cafile=cafile, capath=capath)
    return ctx


def _identity(address: str, port: int) -> tuple[str, int]:
    return address, port


# ---------------------------------------------------------------------------------------------
# Connection to the verified address, cuttable at the deadline
# ---------------------------------------------------------------------------------------------


class _Pinned(http.client.HTTPSConnection):
    """HTTPS connection to an already verified address; keeps its own references to the sockets
    (raw then TLS) to close them at the deadline whatever the response's mode
    (learnings F-005: `Connection: close`, HTTP/1.0)."""

    def __init__(
        self,
        host: str,
        port: int,
        addresses: Sequence[str],
        admit: Callable[[str], bool],
        dial: Dial,
        context: ssl.SSLContext,
        deadline: float,
    ) -> None:
        super().__init__(host, port, timeout=DEADLINE_S, context=context)
        self._addresses = tuple(addresses)
        self._admit = admit
        self._dial = dial
        self._deadline = deadline
        self._context = context
        self._socks: list[socket.socket] = []
        self._cut = threading.Event()

    def _open(self) -> socket.socket:
        """First TCP connection established among the admitted addresses, in their order (HULL-32).

        Each address goes through the admission guard again just before it is dialled; each
        attempt has only the time left of the global deadline. All failing: the last error."""
        last: OSError = ConnectionError("no address")
        for address in self._addresses:
            if not self._admit(address):
                raise Failure(ANOMALY, f"address refused: {address}")
            remaining = self._deadline - time.monotonic()
            if self._cut.is_set() or remaining <= 0:
                raise TimeoutError("deadline")
            target = self._dial(address, self.port)
            family = socket.AF_INET6 if ":" in target[0] else socket.AF_INET
            try:  # family absent from the host (IPv6 disabled): next address too
                raw = socket.socket(family, socket.SOCK_STREAM)
            except OSError as exc:
                last = exc
                continue
            self._socks.append(raw)
            try:
                raw.settimeout(remaining)
                raw.connect(target)
            except OSError as exc:
                raw.close()
                last = exc
                continue
            return raw
        raise last

    def connect(self) -> None:
        raw = self._open()
        tls = self._context.wrap_socket(
            raw, server_hostname=self.host, do_handshake_on_connect=False
        )
        self._socks.append(tls)
        if self._cut.is_set():
            raise TimeoutError("deadline")
        tls.do_handshake()
        self.sock = tls

    def cut(self) -> None:
        self._cut.set()
        for sock in self._socks:
            with contextlib.suppress(OSError, ValueError):
                if sock.fileno() >= 0:
                    socket.socket.shutdown(sock, socket.SHUT_RDWR)


@dataclass
class _Exchange:
    conn: _Pinned | None = None
    sent: bool = False
    response: Response | None = None
    error: Failure | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def attach(self, conn: _Pinned, expired: threading.Event) -> None:
        with self.lock:
            self.conn = conn
            if expired.is_set():
                conn.cut()

    def cut(self) -> None:
        with self.lock:
            if self.conn is not None:
                self.conn.cut()


@dataclass
class Outbound:
    """Bounded outbound client. `resolve`, `context` and `dial` are injectable by tests; the
    address policy is not."""

    context: ssl.SSLContext = field(default_factory=default_context)
    resolve: Resolver = system_resolve
    dial: Dial = _identity
    _workers: threading.BoundedSemaphore = field(
        default_factory=lambda: threading.BoundedSemaphore(MAX_WORKERS)
    )
    # Construction counter (BR-125, AC-08): a single client per process and per use.
    built: ClassVar[int] = 0

    def __post_init__(self) -> None:
        Outbound.built += 1

    def __repr__(self) -> str:
        return "Outbound()"

    # -- addresses -----------------------------------------------------------------------------

    def addresses(self, url: str, internal: InternalHost | None) -> list[str]:
        """Resolved and admitted addresses for `url` (all verified), or `Failure`. Blocking:
        serves `service declare` to display the admitted addresses of an internal host."""
        host, port = self._host_port(url)
        return self._admitted(host, port, internal)

    @staticmethod
    def _host_port(url: str) -> tuple[str, int]:
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise Failure(ANOMALY, "non-https URL")
        if not parts.hostname or parts.username is not None or parts.password is not None:
            raise Failure(ANOMALY, "URL without host or with credentials")
        try:
            port = parts.port or 443
        except ValueError:
            raise Failure(ANOMALY, "invalid port") from None
        return parts.hostname, port

    @staticmethod
    def _policy(host: str, port: int, internal: InternalHost | None) -> Callable[[str], bool]:
        """Admission guard of an address for (host, port): public, or named internal host."""
        exempt = internal is not None and (normalize_host(host), port) == (
            internal.host,
            internal.port,
        )
        return internal_address if exempt else public_address

    def _admitted(self, host: str, port: int, internal: InternalHost | None) -> list[str]:
        """Every resolved address, all verified before any connection, in the resolver's order
        (duplicates removed) — never sorted: sorting put IPv6 first (HULL-32)."""
        normalized = normalize_host(host)
        literal = ip_literal(host)
        if literal is not None:
            addresses = [literal]
        else:
            try:
                addresses = list(dict.fromkeys(self.resolve(normalized, port)))
            except (OSError, UnicodeError):
                raise Failure(TRANSIENT, "name resolution failed") from None
            if not addresses:
                raise Failure(TRANSIENT, "empty name resolution")
        allowed = self._policy(host, port, internal)
        for address in addresses:
            if not allowed(address):
                raise Failure(ANOMALY, f"address refused: {address}")
        return addresses

    # -- request ------------------------------------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        form: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        internal: InternalHost | None = None,
    ) -> Response:
        """One bounded exchange; every HTTP response is returned (the status is classified by the
        caller), every departure from BR-084 is a `Failure`. Blocking."""
        host, port = self._host_port(url)
        if not self._workers.acquire(blocking=False):
            raise Failure(TRANSIENT, "too many simultaneous outbound calls")
        body: bytes | None = None
        sent_headers = {"Accept": "application/json", "User-Agent": "hullgate"}
        if form is not None:
            body = urlencode(dict(form)).encode("ascii")
            sent_headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif json_body is not None:
            body = json.dumps(dict(json_body)).encode("utf-8")
            sent_headers["Content-Type"] = "application/json"
        sent_headers.update(headers or {})
        parts = urlsplit(url)
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        exchange = _Exchange()
        expired = threading.Event()
        deadline = time.monotonic() + DEADLINE_S  # one deadline for the whole exchange

        def work() -> None:
            try:
                exchange.response = self._exchange(
                    method,
                    host,
                    port,
                    target,
                    sent_headers,
                    body,
                    internal,
                    exchange,
                    expired,
                    deadline,
                )
            except Failure as exc:
                exchange.error = exc
            except Exception:  # cut, deadline, connection refused, TLS
                kind = UNCERTAIN if exchange.sent else TRANSIENT
                reason = "deadline exceeded" if expired.is_set() else "connection interrupted"
                exchange.error = Failure(kind, reason)
            finally:
                self._workers.release()

        worker = threading.Thread(target=work, name=THREAD_NAME, daemon=True)
        try:
            worker.start()
        except RuntimeError:
            self._workers.release()
            raise Failure(TRANSIENT, "too many simultaneous outbound calls") from None
        worker.join(DEADLINE_S)
        if worker.is_alive():
            expired.set()
            exchange.cut()
            raise Failure(UNCERTAIN if exchange.sent else TRANSIENT, "deadline exceeded")
        if exchange.error is not None:
            raise exchange.error
        if exchange.response is None:
            raise Failure(TRANSIENT, "no response")
        return exchange.response

    def _exchange(
        self,
        method: str,
        host: str,
        port: int,
        target: str,
        headers: Mapping[str, str],
        body: bytes | None,
        internal: InternalHost | None,
        exchange: _Exchange,
        expired: threading.Event,
        deadline: float,
    ) -> Response:
        addresses = self._admitted(host, port, internal)
        if expired.is_set():
            raise Failure(TRANSIENT, "deadline exceeded")
        conn = _Pinned(
            normalize_host(host),
            port,
            addresses,
            self._policy(host, port, internal),
            self.dial,
            self.context,
            deadline,
        )
        exchange.attach(conn, expired)
        try:
            conn.request(method, target, body=body, headers=dict(headers))
            exchange.sent = True
            response = conn.getresponse()
            length = response.getheader("Content-Length")
            if length is not None and length.isdigit() and int(length) > MAX_BYTES:
                raise Failure(ANOMALY, "response too large")
            data = b""
            while True:
                chunk = response.read(8192)
                if not chunk:
                    break
                data += chunk
                if len(data) > MAX_BYTES:
                    raise Failure(ANOMALY, "response too large")
            return Response(response.status, {k.lower(): v for k, v in response.getheaders()}, data)
        finally:
            conn.cut()
            conn.close()
