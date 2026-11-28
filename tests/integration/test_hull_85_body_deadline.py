"""HULL-85: a stalled upload gets 408 and EOF on a real keep-alive TCP connection."""

from __future__ import annotations

import contextlib
import functools
import json
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
import uvicorn
from starlette.testclient import TestClient
from starlette.types import ASGIApp, Message, Receive, Scope

from hullgate import audit, body
from hullgate.admin import sessions
from hullgate.db import StoreUnavailable
from tests.admin_support import Console
from tests.support import ACCEPT, Gateway, ap, oauth_path, standard_tree

ROLE = "mpx/cairn/staging/dev"
OAUTH_ROUTES = ("authorize", "consent", "token", "revoke", "register")


@contextlib.contextmanager
def served(app: Any) -> Iterator[int]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
        thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        thread.start()
        until = time.monotonic() + 5
        while not server.started and time.monotonic() < until:
            time.sleep(0.01)
        assert server.started
        try:
            yield port
        finally:
            server.should_exit = True
            thread.join(5)
            assert not thread.is_alive()


@pytest.mark.parametrize("surface", ["admin", "ceremony", "mcp", "unsupported"])
@pytest.mark.parametrize("chunked", [False, True])
def test_stalled_body_gets_408_and_connection_closes(
    gw: Gateway,
    surface: str,
    chunked: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    standard_tree(gw)
    monkeypatch.setattr(body, "BODY_READ_TIMEOUT", 1.0)
    console = Console(gw)
    token_id, token = gw.issue(ROLE)
    with console.client() as admin, gw.client() as public:
        key = console.admin(admin)
        assert console.fresh(admin, key).status_code == 200
        if surface in ("admin", "ceremony"):
            app: ASGIApp = console.app
            host = f"{console.origin.host}:{console.origin.port}"
            path = "/admin/api/v1/allowlist" if surface == "admin" else "/admin/api/v1/auth/options"
            method = "PUT" if surface == "admin" else "POST"
            headers = {
                **console.headers(),
                "Host": host,
                "Cookie": f"{sessions.COOKIE}={admin.cookies.get(sessions.COOKIE)}",
            }
        else:
            app = public.app
            method, path = "POST", ap(ROLE)
            headers = {
                "Host": "localhost",
                "Authorization": f"Bearer {token}",
                "Accept": ACCEPT,
                "MCP-Protocol-Version": "bogus" if surface == "unsupported" else "2025-06-18",
            }
        headers.update({"Content-Type": "application/json", "Connection": "keep-alive"})
        headers.update({"Transfer-Encoding": "chunked"} if chunked else {"Content-Length": "100"})
        head = f"{method} {path} HTTP/1.1\r\n" + "".join(
            f"{k}: {v}\r\n" for k, v in headers.items()
        )
        with served(app) as port, socket.create_connection(("127.0.0.1", port), timeout=4) as sock:
            # An unfinished body: keep-alive must be closed by the server, not by this client.
            sock.sendall((head + "\r\n").encode() + (b"1\r\n{\r\n" if chunked else b"{"))
            data = bytearray()
            while chunk := sock.recv(65536):
                data += chunk
        assert bytes(data).startswith(b"HTTP/1.1 408 "), data
        header, raw = bytes(data).split(b"\r\n\r\n", 1)
        assert b"connection: close" in header.lower()
        assert (
            json.loads(raw).get("code" if surface in ("admin", "ceremony") else "error")
            == "request_timeout"
        )
    entries = [e for e in gw.audit() if e["action"] == "access.body_refused"]
    if surface in ("mcp", "unsupported"):
        assert len(entries) == 1
        assert entries[0]["actor"] == token_id
        assert entries[0]["reason"] == "request_timeout"
        assert json.loads(entries[0]["detail"]) == {"timeout_seconds": 1.0, "address": "127.0.0.1"}
    else:
        assert entries == []
    assert not [e for e in gw.audit() if e["action"] in ("admin.allowlist", "relay.tool_call")]


@pytest.mark.parametrize("action", OAUTH_ROUTES)
@pytest.mark.parametrize("chunked", [False, True])
def test_oauth_stalled_body_gets_408_eof_and_one_audit(
    gw: Gateway, action: str, chunked: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-024 AC-11/12/13: read the response and then observe server EOF within two seconds."""
    standard_tree(gw)
    monkeypatch.setattr(body, "BODY_READ_TIMEOUT", 1.0)
    before = gw.audit()
    headers = {
        "Host": "localhost",
        "Connection": "keep-alive",
        "Content-Type": "application/x-www-form-urlencoded",
        # Naming a client does not authenticate it before the body (F-024 §3.3).
        "Authorization": "Basic Zml4dHVyZTo=",
        **({"Transfer-Encoding": "chunked"} if chunked else {"Content-Length": "100"}),
    }
    head = f"POST {oauth_path(ROLE, action)} HTTP/1.1\r\n" + "".join(
        f"{k}: {v}\r\n" for k, v in headers.items()
    )
    with (
        gw.client() as public,
        served(public.app) as port,
        socket.create_connection(("127.0.0.1", port), timeout=4) as sock,
    ):
        sock.sendall((head + "\r\n").encode() + (b"1\r\ng\r\n" if chunked else b"g"))
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(65536)
            assert chunk, data
            data += chunk
        header, raw = bytes(data).split(b"\r\n\r\n", 1)
        assert header.startswith(b"HTTP/1.1 408 "), header
        fields = dict(line.lower().split(b": ", 1) for line in header.split(b"\r\n")[1:])
        assert fields[b"connection"] == b"close"
        length = int(fields[b"content-length"])
        while len(raw) < length:
            chunk = sock.recv(65536)
            assert chunk, raw
            raw += chunk
        assert len(raw) == length
        sock.settimeout(2)
        assert sock.recv(1) == b""
    if action in ("authorize", "consent"):
        assert fields[b"content-type"].startswith(b"text/html")
        assert b"The request did not arrive in time" in raw
        assert b"location" not in fields
    else:
        assert fields[b"content-type"].startswith(b"application/json")
        assert json.loads(raw) == {
            "error": "invalid_request",
            "error_description": "request_timeout",
        }
    entries = gw.audit()
    assert len(entries) == len(before) + 1
    entry = entries[-1]
    assert (entry["action"], entry["result"], entry["reason"], entry["actor"], entry["path"]) == (
        f"oauth.{action}",
        "refused",
        "request_timeout",
        "anonymous",
        ROLE,
    )
    assert json.loads(entry["detail"]) == {"timeout_seconds": 1.0, "address": "127.0.0.1"}


def oauth_request(
    client: TestClient,
    action: str,
    receive: Receive,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    method: str = "POST",
    role: str = ROLE,
) -> list[Message]:
    """Observe ASGI sends, including the absence of a response on disconnect."""
    path = oauth_path(role, action)
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "server": ("localhost", 80),
        "client": ("127.0.0.1", 50000),
        "headers": headers or [],
    }
    messages: list[Message] = []

    async def send(message: Message) -> None:
        messages.append(message)

    assert client.portal is not None
    client.portal.call(functools.partial(client.app, scope, receive, send))
    return messages


@pytest.mark.parametrize("action", OAUTH_ROUTES)
def test_oauth_disconnect_sends_and_audits_nothing(gw: Gateway, action: str) -> None:
    standard_tree(gw)
    before = gw.audit()
    sequence: list[Message] = [
        {"type": "http.request", "body": b"x", "more_body": True},
        {"type": "http.disconnect"},
    ]
    events = iter(sequence)

    async def receive() -> Message:
        return next(events)

    with gw.client() as client:
        assert oauth_request(client, action, receive) == []
    assert gw.audit() == before


@pytest.mark.parametrize("action", OAUTH_ROUTES)
@pytest.mark.parametrize("declared", [False, True])
def test_oauth_body_over_bound_keeps_413_without_audit(
    gw: Gateway, action: str, declared: bool
) -> None:
    standard_tree(gw)
    before = gw.audit()
    reads = 0

    async def receive() -> Message:
        nonlocal reads
        reads += 1
        assert not declared, "An oversized declared body must not be read"
        assert reads <= 2, "Stop at the first chunk beyond the bound"
        return {
            "type": "http.request",
            "body": b"x" * (65536 if reads == 1 else 1),
            "more_body": True,
        }

    with gw.client() as client:
        messages = oauth_request(
            client,
            action,
            receive,
            headers=[(b"content-length", b"65537")] if declared else [],
        )
    assert messages[0]["status"] == 413
    assert json.loads(messages[1]["body"]) == {
        "error": "invalid_request",
        "error_description": "body too large",
    }
    assert reads == (0 if declared else 2)
    assert gw.audit() == before


@pytest.mark.parametrize("action", OAUTH_ROUTES)
def test_oauth_body_at_bound_reaches_the_route(gw: Gateway, action: str) -> None:
    standard_tree(gw)
    with gw.client() as client:
        # A malformed but complete body must reach the same handler as a small malformed one.
        small = client.post(oauth_path(ROLE, action), content=b"x")
        boundary = client.post(oauth_path(ROLE, action), content=b"x" * 65536)
    assert boundary.status_code == small.status_code
    assert boundary.content == small.content
    assert boundary.status_code not in (408, 413)


@pytest.mark.parametrize("action", OAUTH_ROUTES)
def test_oauth_timeout_survives_failed_audit(
    gw: Gateway, action: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    standard_tree(gw)
    attempted: list[dict[str, Any]] = []

    def fail_write(*args: Any, **kwargs: Any) -> None:
        attempted.append(kwargs)
        raise StoreUnavailable("fixture audit unavailable")

    async def timeout(*args: Any, **kwargs: Any) -> bytes:
        raise TimeoutError

    async def receive() -> Message:
        pytest.fail("The patched reader does not consume the body")

    monkeypatch.setattr(audit, "write", fail_write)
    monkeypatch.setattr(body, "read_body", timeout)
    with gw.client() as client:
        messages = oauth_request(client, action, receive)
    assert messages[0]["status"] == 408
    assert (b"connection", b"close") in messages[0]["headers"]
    assert len(attempted) == 1
    assert attempted[0]["action"] == f"oauth.{action}"


@pytest.mark.parametrize("action", OAUTH_ROUTES)
@pytest.mark.parametrize("case", ["method", "role", "unavailable"])
def test_oauth_pre_body_refusals_do_not_read(gw: Gateway, action: str, case: str) -> None:
    standard_tree(gw)

    async def receive() -> Message:
        pytest.fail("Pre-body checks must not read the request")

    with gw.client(key=case != "unavailable") as client:
        messages = oauth_request(
            client,
            action,
            receive,
            method="DELETE" if case == "method" else "POST",
            role="missing/role" if case == "role" else ROLE,
        )
    assert messages[0]["status"] == {"method": 405, "role": 404, "unavailable": 503}[case]
