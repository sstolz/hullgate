"""Client ID metadata documents (CIMD) fetched under bounds (BR-062).

Written by hand: SDK 2.3.0 does not serve CIMD on the server side. A `client_id` that is an
`https` URL designates a JSON document; fetching is bounded to 5 s, 64 KiB, no redirect, and
reaches no private, loopback or link-local address. Every resolved address is verified, then the
connection is made to a verified address (DNS rebinding protection), the name serving only for
SNI and certificate verification: the verified addresses are tried one after the other, in the
resolver's order, up to the first TCP connection established, under the single deadline of the
fetch (HULL-32). A success is cached 10 min at most; a failure is not cached.

No environment variable opens the bounds: tests build a `Fetcher` with a TLS context and an
address filter of their own (no way to do so from the image).

F-020 (BR-309): besides the 16 slots, a live worker counts against the limit key of the request
that started it; a third live fetch for one key answers `cimd_busy` without taking a slot.
"""

from __future__ import annotations

import contextlib
import http.client
import ipaddress
import json
import socket
import ssl
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from hullgate.limits import Slots

TIMEOUT_S = 5.0
MAX_BYTES = 64 * 1024
CACHE_TTL_S = 600
CACHE_MAX = 256  # m3 (review 411): entries at most, the oldest evicted
MAX_WORKERS = 16  # simultaneous fetches at most (worker threads still alive included)
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]", "::1")


class CimdError(Exception):
    """Document unretrievable or invalid; `reason` is a short cause for the audit."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def is_cimd_client_id(client_id: str) -> bool:
    try:
        parts = urlsplit(client_id)
    except ValueError:  # "https://[": not a URL, hence not a document's address
        return False
    return parts.scheme == "https" and bool(parts.hostname) and not parts.fragment


def public_address(address: str) -> bool:
    """True for a globally routable address; false for private, loopback, link-local, etc."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_global) and not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def valid_redirect_uri(uri: str) -> bool:
    """Absolute `https`, or `http` to `localhost` / `127.0.0.1` only (BR-061), without fragment.

    Same rule for pre-registered clients, registered (DCR) clients and documents (CIMD).
    An unreadable URL ("https://[") is refused like any other.
    """
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    if parts.fragment or not parts.netloc:
        return False
    if parts.scheme == "https":
        return bool(parts.hostname)
    if parts.scheme == "http":
        return parts.hostname in ("localhost", "127.0.0.1", "::1")
    return False


Resolver = Callable[[str, int], Sequence[str]]


def system_resolve(host: str, port: int) -> list[str]:
    """Addresses from `getaddrinfo`, in its order (the system's RFC 6724 policy), duplicates
    removed. Never sorted: a lexicographic sort puts IPv6 first (HULL-32)."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


class _PinnedHTTPS(http.client.HTTPSConnection):
    """HTTPS connection to an already verified address; the name serves only for SNI and the
    certificate.

    The connection keeps its own references to the sockets (raw, then TLS): `http.client` sets
    `self.sock` to None as soon as a response announces its closing (HTTP/1.0,
    `Connection: close`), while the descriptor still lives in the response's file; `cut()` closes
    those references. The TLS handshake is played after the TLS socket is recorded, so that it
    can be cut too (review 413, B2).
    """

    def __init__(
        self,
        host: str,
        addresses: Sequence[str],
        allowed: Callable[[str], bool],
        port: int,
        context: ssl.SSLContext,
        deadline: float,
    ) -> None:
        super().__init__(host, port, timeout=TIMEOUT_S, context=context)
        self._addresses = tuple(addresses)
        self._allowed = allowed
        self._deadline = deadline
        self._context = context
        self._socks: list[socket.socket] = []
        self._cut = threading.Event()

    def _open(self) -> socket.socket:
        """First TCP connection established among the verified addresses, in their order; each
        address goes through the filter again just before it is dialled, each attempt has only
        the time left of the single deadline (HULL-32). All failing: the last error."""
        last: OSError = ConnectionError("no address")
        for address in self._addresses:
            if not self._allowed(address):
                raise CimdError("cimd_forbidden_address")
            remaining = self._deadline - time.monotonic()
            if self._cut.is_set() or remaining <= 0:
                raise TimeoutError("fetch deadline")
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            try:  # family absent from the host (IPv6 disabled): next address too
                raw = socket.socket(family, socket.SOCK_STREAM)
            except OSError as exc:
                last = exc
                continue
            self._socks.append(raw)
            try:
                raw.settimeout(remaining)
                raw.connect((address, self.port))
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
        if self._cut.is_set():  # deadline passed during the connection
            raise TimeoutError("fetch deadline")
        tls.do_handshake()
        self.sock = tls

    def cut(self) -> None:
        """Closes the connection under the reading thread's feet, whatever its state."""
        self._cut.set()
        for sock in self._socks:
            with contextlib.suppress(OSError, ValueError):
                if sock.fileno() >= 0:
                    socket.socket.shutdown(sock, socket.SHUT_RDWR)


@dataclass
class _Exchange:
    """State shared between the caller and the worker thread of a fetch."""

    conn: _PinnedHTTPS | None = None
    body: bytes | None = None
    error: CimdError | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def attach(self, conn: _PinnedHTTPS, expired: threading.Event) -> None:
        with self.lock:
            self.conn = conn
            if expired.is_set():
                conn.cut()

    def cut(self) -> None:
        with self.lock:
            if self.conn is not None:
                self.conn.cut()


@dataclass
class Fetcher:
    """Fetches and validates a document; `allowed` decides which addresses are reachable.

    Hard deadline (BR-062, reviews 411 and 413): the whole fetch — name resolution, connection,
    TLS handshake, headers, body — runs in a worker thread; the caller waits for it at most
    `TIMEOUT_S`, then cuts the connection and answers `cimd_timeout`, whatever the server's
    connection mode or the resolver's slowness. Worker threads still alive (a resolution that
    does not return) are bounded to `MAX_WORKERS`: beyond that, immediate refusal (`cimd_busy`),
    never a wait.
    """

    context: ssl.SSLContext = field(default_factory=ssl.create_default_context)
    resolve: Resolver = system_resolve
    allowed: Callable[[str], bool] = public_address
    clock: Callable[[], float] = time.monotonic
    _cache: OrderedDict[str, tuple[float, dict[str, Any]]] = field(default_factory=OrderedDict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _workers: threading.BoundedSemaphore = field(
        default_factory=lambda: threading.BoundedSemaphore(MAX_WORKERS)
    )
    _per_key: Slots = field(default_factory=Slots)  # BR-309

    def document(self, client_id: str, key: str | None = None) -> dict[str, Any]:
        """Validated document (its `client_id` field equals the URL); `CimdError` otherwise.
        Blocking. `key`: the limit key of the request (BR-309); None counts against no key."""
        now = self.clock()
        with self._lock:
            hit = self._cache.get(client_id)
            if hit is not None and hit[0] > now:
                return hit[1]
        doc = self._fetch(client_id, key)
        with self._lock:
            for key in [k for k, (exp, _) in self._cache.items() if exp <= now]:
                del self._cache[key]
            self._cache[client_id] = (now + CACHE_TTL_S, doc)
            self._cache.move_to_end(client_id)
            while len(self._cache) > CACHE_MAX:
                self._cache.popitem(last=False)
        return doc

    def _fetch(self, client_id: str, key: str | None) -> dict[str, Any]:
        if not is_cimd_client_id(client_id):
            raise CimdError("cimd_not_https")
        if key is not None and not self._per_key.acquire(key):
            raise CimdError("cimd_busy")  # BR-309: before, and without, one of the 16 slots
        if not self._workers.acquire(blocking=False):
            if key is not None:
                self._per_key.release(key)
            raise CimdError("cimd_busy")
        exchange = _Exchange()
        expired = threading.Event()
        deadline = time.monotonic() + TIMEOUT_S  # one deadline for the whole fetch
        worker = threading.Thread(
            target=self._work, args=(client_id, exchange, expired, deadline, key), daemon=True
        )
        try:
            worker.start()
        except RuntimeError as exc:
            self._workers.release()
            if key is not None:
                self._per_key.release(key)
            raise CimdError("cimd_busy") from exc
        worker.join(TIMEOUT_S)  # the deadline runs from before name resolution (I2)
        if worker.is_alive():
            expired.set()
            exchange.cut()
            raise CimdError("cimd_timeout")
        if exchange.error is not None:
            raise exchange.error
        if exchange.body is None:
            raise CimdError("cimd_unreachable")
        return validate(client_id, exchange.body)

    def _work(
        self,
        client_id: str,
        exchange: _Exchange,
        expired: threading.Event,
        deadline: float,
        key: str | None = None,
    ) -> None:
        """Worker thread: returns its result in `exchange`; releases its slots on exit, the one
        of its key included (a worker outliving the deadline stays counted, BR-309)."""
        try:
            exchange.body = self._exchange(client_id, exchange, expired, deadline)
        except CimdError as exc:
            exchange.error = exc
        except Exception as exc:  # connection cut by the deadline, or network error
            if expired.is_set() or isinstance(exc, TimeoutError):
                exchange.error = CimdError("cimd_timeout")
            else:
                exchange.error = CimdError("cimd_unreachable")
        finally:
            self._workers.release()
            if key is not None:
                self._per_key.release(key)

    def _exchange(
        self, client_id: str, exchange: _Exchange, expired: threading.Event, deadline: float
    ) -> bytes:
        parts = urlsplit(client_id)
        host = parts.hostname or ""
        port = parts.port or 443
        try:
            addresses = list(dict.fromkeys(self.resolve(host, port)))
        except OSError as exc:
            raise CimdError("cimd_dns") from exc
        if not addresses:
            raise CimdError("cimd_dns")
        if not all(self.allowed(a) for a in addresses):
            raise CimdError("cimd_forbidden_address")
        if expired.is_set():
            raise CimdError("cimd_timeout")
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        conn = _PinnedHTTPS(host, addresses, self.allowed, port, self.context, deadline)
        exchange.attach(conn, expired)
        try:
            conn.request(
                "GET", target, headers={"Accept": "application/json", "Host": parts.netloc}
            )
            response = conn.getresponse()
            if response.status != 200:  # 3xx included: no redirect followed
                raise CimdError(f"cimd_http_{response.status}")
            length = response.getheader("Content-Length")
            if length is not None and length.isdigit() and int(length) > MAX_BYTES:
                raise CimdError("cimd_too_large")
            body = b""
            while True:
                chunk = response.read(8192)
                if not chunk:
                    break
                body += chunk
                if len(body) > MAX_BYTES:
                    raise CimdError("cimd_too_large")
            return body
        finally:
            conn.cut()
            conn.close()


def validate(client_id: str, body: bytes) -> dict[str, Any]:
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise CimdError("cimd_not_json") from exc
    if not isinstance(doc, dict):
        raise CimdError("cimd_not_object")
    if doc.get("client_id") != client_id:
        raise CimdError("cimd_client_id_mismatch")
    uris = doc.get("redirect_uris")
    if not isinstance(uris, list) or not uris or not all(isinstance(u, str) for u in uris):
        raise CimdError("cimd_redirect_uris")
    if not all(valid_redirect_uri(u) for u in uris):
        raise CimdError("cimd_redirect_uris")
    method = doc.get("token_endpoint_auth_method", "none")
    if method != "none":  # public client only: a document carries no secret
        raise CimdError("cimd_auth_method")
    name = doc.get("client_name")
    if name is not None and not isinstance(name, str):
        raise CimdError("cimd_client_name")
    return doc
