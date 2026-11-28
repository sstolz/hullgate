"""Fake MCP upstream (F-008 lot A): two local listeners (HTTP and HTTPS), several hosts.

Like the fake OAuth provider, a single server per scheme serves every upstream, routed by the
`Host` header. The gateway's bounded client (`hullgate.upstream_mcp.Transport`) reaches it
through an injected resolver — `*.mcp.test` resolves to a fictitious public address,
`int-*.mcp.test` to a fictitious private address — and a `dial` function that redirects the
verified address to the scheme's listener: the address policy of BR-106/BR-107 is played as is.

Generations served (BR-118): `modern` (2026-07-28, sessionless; `Mcp-Method`, `Mcp-Name`,
`Mcp-Param-*` headers checked by the SDK's validators), `session` (handshake, `Mcp-Session-Id`),
`stateless` (handshake without session, like gitea-mcp), `none` (no common version). Each
upstream keeps a journal (`journal`) of everything it receives, headers included, and a count of
executions per tool; scripted faults (`faults`) are consumed in order.
"""

from __future__ import annotations

import contextlib
import json
import secrets
import select
import ssl
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from mcp.shared.inbound import classify_inbound_request, validate_mcp_param_headers

from hullgate.upstream_mcp import Clock, Transport, UpstreamClient
from tests.fake_provider import make_cert

MODERN = "2026-07-28"
HANDSHAKE = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
DOMAIN = ".mcp.test"


@dataclass
class Req:
    http: str  # POST, DELETE, GET
    method: str | None  # JSON-RPC method
    headers: dict[str, str]  # lower-case names
    body: dict[str, Any] | None
    at: float


@dataclass
class Fault:
    """Scripted fault, consumed by the first request whose JSON-RPC method matches (`on` None:
    any)."""

    kind: str  # status, redirect, big, big_sse, drip_headers, drip_body, drip_sse, hang, cut,
    #            bad_session, expire, sse, ask, ask_flood (`seconds` requests), progress,
    #            progress_hang
    on: str | None = None
    status: int = 500
    headers: dict[str, str] = field(default_factory=dict)
    seconds: float = 70.0


# F-015 (§12): fixtures declare a class rather than switch the policy off. By default a fake tool
# is `write` by its annotations (BR-171), exposed under the organisation's default mode on a
# non-sensitive placement; `READ`, `DESTRUCTIVE` or `NONE` (no annotation: `destructive` by
# default) are passed where a test needs another class.
WRITE: dict[str, Any] = {"readOnlyHint": False, "destructiveHint": False}
READ: dict[str, Any] = {"readOnlyHint": True}
DESTRUCTIVE: dict[str, Any] = {"destructiveHint": True}
NONE: dict[str, Any] | None = None


def tool(
    name: str,
    schema: dict[str, Any] | None = None,
    annotations: dict[str, Any] | None = WRITE,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": name,
        "description": f"tool {name}",
        "inputSchema": schema or {"type": "object", "properties": {}},
    }
    if annotations is not None:
        out["annotations"] = dict(annotations)
    return out


class Upstream:
    def __init__(self, host: str, generation: str = "modern") -> None:
        self.host = host
        self.generation = generation
        self.tools: list[dict[str, Any]] = [tool("read"), tool("write")]
        self.page_size: int | None = None
        self.journal: list[Req] = []
        self.faults: list[Fault] = []
        self.executions: dict[str, int] = {}
        self.sessions: set[str] = set()
        self.version_chosen: str | None = None
        self.lock = threading.Lock()
        # F-008 lot B: imposed tool result (AC-20), 401 to every request (AC-32), answers
        # received to the requests the upstream addresses to the client (BR-121)
        self.results: dict[str, dict[str, Any]] = {}
        self.reject = False
        self.answers: list[dict[str, Any]] = []
        # F-008 lot C: (monotonic) instants at which the upstream saw a held stream cut
        self.cut_at: list[float] = []

    def calls(self, method: str | None = None) -> list[Req]:
        with self.lock:
            return [r for r in self.journal if method is None or r.method == method]

    def fault_for(self, method: str | None) -> Fault | None:
        with self.lock:
            for i, f in enumerate(self.faults):
                if f.on is None or f.on == method:
                    return self.faults.pop(i)
        return None


class UpNet:
    """The two listeners and the "DNS resolution" of the `*.mcp.test` hosts."""

    def __init__(self) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="fake-upstream-"))
        cert, key = make_cert(self.folder, ["*.mcp.test", "localhost"])
        self.upstreams: dict[str, Upstream] = {}
        self.addresses: dict[str, list[str]] = {}
        self.dialed: list[tuple[str, int]] = []
        self.refused_ports: set[int] = set()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        net = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_: object) -> None:
                return

            def handle_one_request(self) -> None:
                try:
                    super().handle_one_request()
                except (ConnectionError, ssl.SSLError, OSError):
                    self.close_connection = True

            def do_POST(self) -> None:
                net._post(self)

            def do_DELETE(self) -> None:
                net._delete(self)

            def do_GET(self) -> None:
                net._record(self, "GET", None, None)
                self._plain(405, b"")

            def _plain(self, status: int, data: bytes, ctype: str = "application/json") -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.handler = Handler
        self.plain = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.plain.daemon_threads = True
        self.tls = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.tls.daemon_threads = True
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        self.tls.socket = ctx.wrap_socket(self.tls.socket, server_side=True)
        for server in (self.plain, self.tls):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        self.context = ssl.create_default_context(cafile=str(cert))

    # -- network -------------------------------------------------------------------------------

    def upstream(self, host: str, generation: str = "modern") -> Upstream:
        host = host.lower()
        with self.lock:
            if host not in self.upstreams:
                self.upstreams[host] = Upstream(host, generation)
            return self.upstreams[host]

    def resolve(self, host: str, port: int) -> list[str]:
        with self.lock:
            if host in self.addresses:
                return list(self.addresses[host])
        if host.endswith(DOMAIN):
            n = int.from_bytes(host.encode()[:2].ljust(2, b"x"), "big") % 250 + 1
            return [f"10.9.{n}.7"] if host.startswith("int-") else [f"11.9.{n}.7"]
        raise OSError("unknown host")

    def dial(self, address: str, port: int) -> tuple[str, int]:
        with self.lock:
            self.dialed.append((address, port))
        if port in self.refused_ports:
            return "127.0.0.1", 1  # closed port: connection refused
        server = self.tls if port == 443 else self.plain
        return "127.0.0.1", int(server.server_address[1])

    def transport(self, clock: Clock | None = None) -> Transport:
        return Transport(context=self.context, resolve=self.resolve, dial=self.dial, clock=clock)

    def client(self, clock: Clock | None = None) -> UpstreamClient:
        return UpstreamClient(self.transport(clock))

    def reset(self) -> None:
        with self.lock:
            self.upstreams.clear()
            self.addresses.clear()
            self.dialed.clear()
            self.refused_ports.clear()

    # -- service -------------------------------------------------------------------------------

    def _upstream_of(self, h: BaseHTTPRequestHandler) -> Upstream | None:
        host = (h.headers.get("Host") or "").split(":")[0].lower()
        with self.lock:
            return self.upstreams.get(host)

    def _record(
        self, h: BaseHTTPRequestHandler, verb: str, method: str | None, body: dict[str, Any] | None
    ) -> Upstream | None:
        up = self._upstream_of(h)
        if up is not None:
            with up.lock:
                up.journal.append(
                    Req(
                        verb,
                        method,
                        {k.lower(): v for k, v in h.headers.items()},
                        body,
                        time.monotonic(),
                    )
                )
        return up

    def _delete(self, h: BaseHTTPRequestHandler) -> None:
        up = self._record(h, "DELETE", None, None)
        if up is not None:
            with up.lock:
                up.sessions.discard(h.headers.get("Mcp-Session-Id") or "")
        h.send_response(200)
        h.send_header("Content-Length", "0")
        h.end_headers()

    def _post(self, h: BaseHTTPRequestHandler) -> None:
        length = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(length)
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        method = body.get("method") if isinstance(body, dict) else None
        up = self._record(h, "POST", method, body if isinstance(body, dict) else None)
        if up is None or not isinstance(body, dict):
            self._send(h, 404, None)
            return
        if up.reject:
            self._send(h, 401, {"error": "invalid_token"}, {"WWW-Authenticate": "Bearer"})
            return
        if method is None and ("result" in body or "error" in body):
            with up.lock:
                up.answers.append(body)  # the client's answer to an upstream request
            self._send(h, 202, None)
            return
        fault = up.fault_for(method)
        if fault is not None and self._fault(h, up, fault, body):
            return
        if fault is not None and fault.kind == "delay":
            self.stop.wait(fault.seconds)
        status, doc, extra = self._answer(up, h, body)
        if fault is not None and fault.kind == "sse":
            self._sse(h, doc, notify=True)
            return
        if fault is not None and fault.kind == "ask":
            self._sse(h, doc, notify=False, ask=True)
            return
        if fault is not None and fault.kind == "ask_flood":
            self._sse(h, doc, notify=False, ask=True, asks=int(fault.seconds))
            return
        if fault is not None and fault.kind in ("progress", "progress_hang"):
            self._progress(h, up, body, doc, fault)
            return
        self._send(h, status, doc, extra)

    def _send(
        self,
        h: BaseHTTPRequestHandler,
        status: int,
        doc: dict[str, Any] | None,
        extra: dict[str, str] | None = None,
    ) -> None:
        data = b"" if doc is None else json.dumps(doc).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        for k, v in (extra or {}).items():
            h.send_header(k, v)
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)

    def _sse(
        self,
        h: BaseHTTPRequestHandler,
        doc: dict[str, Any] | None,
        notify: bool,
        ask: bool = False,
        asks: int = 1,
    ) -> None:
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Connection", "close")
        h.end_headers()
        if ask:  # upstream-to-client requests, before the response (BR-121)
            params = {"messages": [], "maxTokens": 1, "marker": "ASK-MARKER"}
            for i in range(1, asks + 1):
                req = {"jsonrpc": "2.0", "id": f"ask-{i}", "method": "sampling/createMessage",
                       "params": params}  # fmt: skip
                h.wfile.write(b"event: message\ndata: " + json.dumps(req).encode() + b"\n\n")
            h.wfile.flush()
            time.sleep(0.3)
        if notify:
            note = {"jsonrpc": "2.0", "method": "notifications/message", "params": {"data": "x"}}
            h.wfile.write(b"event: message\ndata: " + json.dumps(note).encode() + b"\n\n")
        h.wfile.write(b"event: message\ndata: " + json.dumps(doc).encode() + b"\n\n")
        h.wfile.flush()
        h.close_connection = True

    def _progress(
        self,
        h: BaseHTTPRequestHandler,
        up: Upstream,
        body: dict[str, Any],
        doc: dict[str, Any] | None,
        f: Fault,
    ) -> None:
        """BR-120: SSE stream of three progress notifications under the received token
        (message carrying the call's arguments), interleaved with a progress of another token and
        a `notifications/message`; then the response (`progress`), or a held stream, a comment
        every 50 ms, until the gateway cuts it (`progress_hang`, `cut_at`)."""
        raw = body.get("params")
        params: dict[str, Any] = raw if isinstance(raw, dict) else {}
        meta = params.get("_meta")
        token = meta.get("progressToken") if isinstance(meta, dict) else None
        marker = json.dumps(params.get("arguments") or {}, sort_keys=True)
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Connection", "close")
        h.end_headers()

        def event(message: dict[str, Any]) -> None:
            h.wfile.write(b"event: message\ndata: " + json.dumps(message).encode() + b"\n\n")
            h.wfile.flush()

        def progress(tok: Any, value: int, text: str) -> dict[str, Any]:
            p = {"progressToken": tok, "progress": value, "total": 3, "message": text}
            return {"jsonrpc": "2.0", "method": "notifications/progress", "params": p}

        sock = h.connection

        def cut() -> None:
            with up.lock:
                up.cut_at.append(time.monotonic())

        try:
            for i in (1, 2, 3):
                if token is not None:
                    event(progress(token, i, f"{marker} {i}"))
                event(progress("other-token", 99, "INTRUDER-PROGRESS"))
                note = {"jsonrpc": "2.0", "method": "notifications/message",
                        "params": {"level": "info", "data": "INTRUDER-MESSAGE"}}  # fmt: skip
                event(note)
                time.sleep(0.02)
            if f.kind == "progress":
                event(doc or {})
                h.close_connection = True
                return
        except (OSError, ValueError):  # cut during progress (fast 2026-07-28 client)
            cut()
            h.close_connection = True
            return
        end = time.monotonic() + f.seconds
        while time.monotonic() < end and not self.stop.is_set():
            try:
                h.wfile.write(b": held\n\n")
                h.wfile.flush()
                readable, _, _ = select.select([sock], [], [], 0.05)
                if readable and not sock.recv(1):
                    raise ConnectionError("end of stream")
            except (OSError, ValueError):
                cut()
                break
        h.close_connection = True

    def _fault(
        self, h: BaseHTTPRequestHandler, up: Upstream, f: Fault, body: dict[str, Any]
    ) -> bool:
        if f.kind == "status":
            if f.status == 200:
                return False
            self._send(h, f.status, {"error": "boom"}, f.headers)
            return True
        if f.kind == "rpc_error":
            doc = {"jsonrpc": "2.0", "id": body.get("id"),
                   "error": {"code": -32603, "message": "UPSTREAM-SECRET-MESSAGE"}}  # fmt: skip
            self._send(h, 200, doc)
            return True
        if f.kind == "redirect":
            self._send(h, 302, None, {"Location": "https://other.mcp.test/mcp"})
            return True
        if f.kind in ("big_chunked", "big_close", "exact_chunked"):
            # JSON body without `Content-Length`: the 1 MiB bound plays out in the read
            size = 1024 * 1024 + (0 if f.kind == "exact_chunked" else 1)
            head = b'{"jsonrpc":"2.0","id":' + json.dumps(body.get("id")).encode()
            head += b',"result":{"tools":[],"pad":"'
            data = head + b"a" * (size - len(head) - 3) + b'"}}'
            assert len(data) == size
            h.send_response(200)
            h.send_header("Content-Type", "application/json")
            if f.kind == "big_close":
                h.send_header("Connection", "close")
                h.end_headers()
                with contextlib.suppress(OSError):
                    h.wfile.write(data)
                h.close_connection = True
                return True
            h.send_header("Transfer-Encoding", "chunked")
            h.end_headers()
            with contextlib.suppress(OSError):
                for i in range(0, len(data), 65536):
                    chunk = data[i : i + 65536]
                    h.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                h.wfile.write(b"0\r\n\r\n")
            return True
        if f.kind == "big":
            data = b'{"jsonrpc":"2.0","id":' + json.dumps(body.get("id")).encode() + b',"x":"'
            data += b"a" * (1024 * 1024 + 1 - len(data) - 2) + b'"}'
            h.send_response(200)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(data)))
            h.end_headers()
            h.wfile.write(data)
            return True
        if f.kind == "big_sse":
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream")
            h.end_headers()
            note = b'data: {"jsonrpc":"2.0","method":"notifications/message","params":{}}\n\n'
            for _ in range(1024 * 1024 // len(note) + 2):
                h.wfile.write(note)
            h.close_connection = True
            return True
        if f.kind in ("hang", "drip_headers"):
            if f.kind == "drip_headers":
                h.wfile.write(b"HTTP/1.1 200 OK\r\n")
                for _ in range(int(f.seconds / 0.4)):
                    if self.stop.wait(0.4):
                        break
                    h.wfile.write(b"X")
                    h.wfile.flush()
            else:
                self.stop.wait(f.seconds)
            h.close_connection = True
            return True
        if f.kind in ("drip_body", "drip_sse"):
            sse = f.kind == "drip_sse"
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream" if sse else "application/json")
            if not sse:
                h.send_header("Content-Length", "1000")
            h.end_headers()
            for _ in range(int(f.seconds / 0.4)):
                if self.stop.wait(0.4):
                    break
                h.wfile.write(b":" if sse else b" ")
                h.wfile.flush()
            h.close_connection = True
            return True
        if f.kind == "cut":
            h.send_response(200)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", "1000")
            h.end_headers()
            h.wfile.write(b'{"jsonrpc"')
            h.wfile.flush()
            h.close_connection = True
            return True
        if f.kind == "bad_session":
            status, doc, _ = self._answer(up, h, body)
            self._send(h, status, doc, {"Mcp-Session-Id": "bad id"})
            return True
        if f.kind == "expire":
            with up.lock:
                up.sessions.clear()
            return False
        return False

    def _answer(self, up: Upstream, h: BaseHTTPRequestHandler, body: dict[str, Any]) -> Any:
        method = body.get("method")
        rid = body.get("id")
        raw = body.get("params")
        params: dict[str, Any] = raw if isinstance(raw, dict) else {}
        headers = {k.lower(): v for k, v in h.headers.items()}

        def ok(result: dict[str, Any]) -> tuple[int, dict[str, Any], dict[str, str]]:
            return 200, {"jsonrpc": "2.0", "id": rid, "result": result}, {}

        def err(code: int, message: str, status: int = 200, data: Any = None) -> Any:
            e: dict[str, Any] = {"code": code, "message": message}
            if data is not None:
                e["data"] = data
            return status, {"jsonrpc": "2.0", "id": rid, "error": e}, {}

        if up.generation == "none":
            if method == "server/discover":
                return err(-32022, "Unsupported protocol version", 400,
                           {"supported": ["2099-01-01"], "requested": MODERN})  # fmt: skip
            if method == "initialize":
                return ok({"protocolVersion": "1999-01-01", "capabilities": {},
                           "serverInfo": {"name": "x", "version": "0"}})  # fmt: skip
            return err(-32600, "no")
        if up.generation == "modern":
            route = classify_inbound_request(body, headers=headers)
            if not hasattr(route, "protocol_version"):
                return err(getattr(route, "code", -32600), getattr(route, "message", "bad"), 400)
            if method == "server/discover":
                with up.lock:
                    up.version_chosen = MODERN
                return ok({"supportedVersions": [MODERN], "capabilities": {"tools": {}},
                           "serverInfo": {"name": "fake", "version": "1"},
                           "cacheScope": "private"})  # fmt: skip
            return self._method(up, method, params, headers, ok, err)
        # handshake generations
        if method == "server/discover":
            if up.generation == "session":
                return err(-32600, "Bad Request: Missing session ID", 400)
            return err(-32601, "Method not found")
        if method == "initialize":
            offered = params.get("protocolVersion")
            chosen = offered if offered in HANDSHAKE else HANDSHAKE[-1]
            with up.lock:
                up.version_chosen = chosen
            status, doc, extra = ok({"protocolVersion": chosen, "capabilities": {"tools": {}},
                                     "serverInfo": {"name": "fake", "version": "1"},
                                     "instructions": "INSTRUCTIONS-MARKER"})  # fmt: skip
            if up.generation == "session":
                sid = secrets.token_hex(8)
                with up.lock:
                    up.sessions.add(sid)
                extra = {"Mcp-Session-Id": sid}
            return status, doc, extra
        if up.generation == "session":
            given = headers.get("mcp-session-id")
            with up.lock:
                known = given in up.sessions
            if given is None:
                return err(-32600, "Bad Request: Missing session ID", 400)
            if not known:
                gone = {"code": -32001, "message": "Session not found"}
                return 404, {"jsonrpc": "2.0", "id": rid, "error": gone}, {}
        if method == "notifications/initialized":
            return 202, None, {}
        return self._method(up, method, params, headers, ok, err)

    @staticmethod
    def _method(
        up: Upstream,
        method: Any,
        params: dict[str, Any],
        headers: dict[str, str],
        ok: Callable[[dict[str, Any]], Any],
        err: Callable[..., Any],
    ) -> Any:
        if method == "tools/list":
            start = int(params.get("cursor") or 0)
            size = up.page_size or len(up.tools) or 1
            page = up.tools[start : start + size]
            result: dict[str, Any] = {"tools": page}
            if start + size < len(up.tools):
                result["nextCursor"] = str(start + size)
            return ok(result)
        if method == "tools/call":
            name = str(params.get("name"))
            found = next((t for t in up.tools if t["name"] == name), None)
            if found is None:
                return err(-32602, f"Unknown tool: {name}")
            arguments = params.get("arguments") or {}
            if up.generation == "modern":
                bad = validate_mcp_param_headers(found.get("inputSchema"), arguments, headers)
                if bad is not None:
                    return err(bad.code, bad.message, 400)
            with up.lock:
                up.executions[name] = up.executions.get(name, 0) + 1
                imposed = up.results.get(name)
            if imposed is not None:
                return ok(imposed)
            text = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
            return ok({"content": [{"type": "text", "text": text}], "isError": False})
        return err(-32601, "Method not found")


_NET: list[UpNet] = []


def shared() -> UpNet:
    if not _NET:
        _NET.append(UpNet())
    return _NET[0]


def reset_shared() -> None:
    if _NET:
        _NET[0].reset()


class FakeClock:
    """Injected clock of the upstream client (AC-05 amended: the 60 s deadline on the same code
    path). A healthy exchange is waited for until its end (wide real cap); time advances by the
    requested duration only for a fault announced by `expect_hang`, once the request has reached
    the upstream (review 448, m6: no false red on a loaded machine)."""

    REAL_CAP_S = 30.0

    def __init__(self) -> None:
        self.mono = 1000.0
        self.now = float(int(time.time()))
        self.waits: list[float] = []
        self._arrived: Callable[[], bool] | None = None

    def expect_hang(self, arrived: Callable[[], bool]) -> None:
        """The next wait is on an upstream that will not answer: as soon as `arrived()` is true
        (request received), the deadline is crossed without waiting in real time."""
        self._arrived = arrived

    def monotonic(self) -> float:
        return self.mono

    def wall(self) -> float:
        return self.now

    def wait(self, worker: threading.Thread, seconds: float) -> None:
        self.waits.append(seconds)
        arrived, self._arrived = self._arrived, None
        if arrived is None:
            worker.join(self.REAL_CAP_S)
        else:
            limit = time.monotonic() + self.REAL_CAP_S
            while not arrived() and worker.is_alive() and time.monotonic() < limit:
                time.sleep(0.01)
            worker.join(0.05)
        if worker.is_alive():
            self.mono += seconds
            self.now += seconds

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.now += seconds
