"""BR-107 (F-008 lot A, AC-05 amended, AC-06): every exchange with an MCP upstream is bounded.

The hard deadline of 15 s played at its real value by `tools/list`; the 60 s one checked by its
constant and by the injected clock on the same code path (post-freeze amendment of 2026-10-30).
Size of 1 MiB, redirect never followed, environment ignored, `Mcp-Session-Id` checked, addresses
all verified before the connection, closed list of outbound headers (with `Mcp-Method`,
`Mcp-Name`, `Mcp-Param-*` drawn from the body)."""

from __future__ import annotations

import base64
import http.client
import time
from collections.abc import Iterator

import pytest

from hullgate import upstream_mcp
from hullgate.upstream_mcp import (
    CALL_S,
    LIST_S,
    MODERN,
    Target,
    Transport,
    UpstreamClient,
    UpstreamError,
)
from tests import fake_upstream
from tests.fake_upstream import FakeClock, Fault, UpNet, tool

AUTH = "Bearer tok-bounds"
PUBLIC = Target("https://pub.mcp.test/mcp")
INTERNAL = Target("http://int-a.mcp.test/mcp", internal=True)
ALLOWED = {
    "host",
    "content-length",
    "authorization",
    "content-type",
    "accept",
    "user-agent",
    "mcp-protocol-version",
    "mcp-session-id",
    "mcp-method",
    "mcp-name",
}


@pytest.fixture
def net() -> Iterator[UpNet]:
    net = fake_upstream.shared()
    net.reset()
    net.upstream("pub.mcp.test", "modern")
    net.upstream("int-a.mcp.test", "session")
    yield net
    net.stop.set()  # releases the hung server threads (review 448, n2)
    time.sleep(0.2)
    net.stop.clear()
    net.reset()


def _error(fn: object) -> UpstreamError:
    with pytest.raises(UpstreamError) as exc:
        fn()  # type: ignore[operator]
    return exc.value


# -- AC-05: time and size bounds ----------------------------------------------------


def test_br_107_bounds_are_the_v1_ceilings() -> None:
    assert (upstream_mcp.HANDSHAKE_S, LIST_S, CALL_S) == (15.0, 15.0, 60.0)
    assert upstream_mcp.MAX_BYTES == 1024 * 1024


def test_br_107_redirect_is_never_followed(net: UpNet) -> None:
    other = net.upstream("other.mcp.test")
    net.upstreams["pub.mcp.test"].faults.append(Fault("redirect", on="tools/list"))
    err = _error(lambda: net.client().list_tools(("r", "c"), PUBLIC, AUTH))
    assert (err.code, err.reason) == ("upstream_unavailable", "status 302")
    assert other.calls() == []


@pytest.mark.parametrize("kind", ["big", "big_sse", "big_chunked", "big_close"])
def test_br_107_response_over_one_mib_is_cut(
    net: UpNet, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1 MiB + 1 byte: announced (`Content-Length`), in an SSE stream, and without
    `Content-Length` (`chunked`, close) — the last two play the bounded `_read_all` loop
    (review 448, M1); the bytes read never exceed 1 MiB + one read block (8 KiB)."""
    read = [0]
    original = http.client.HTTPResponse.read1

    def counted(self: http.client.HTTPResponse, n: int = -1) -> bytes:
        data = original(self, n)
        read[0] += len(data)
        return data

    client = net.client()
    client.list_tools(("r", "c"), PUBLIC, AUTH)  # generation negotiated first
    monkeypatch.setattr(http.client.HTTPResponse, "read1", counted)
    net.upstreams["pub.mcp.test"].faults.append(Fault(kind, on="tools/list"))
    err = _error(lambda: client.list_tools(("r", "c"), PUBLIC, AUTH))
    assert err.code == "response_too_large"
    assert read[0] <= upstream_mcp.MAX_BYTES + 8192


def test_br_107_exactly_one_mib_without_length_is_read(net: UpNet) -> None:
    """Boundary: a body of exactly 1 MiB, without `Content-Length`, is read and accepted."""
    net.upstreams["pub.mcp.test"].faults.append(Fault("exact_chunked", on="tools/list"))
    client = net.client()
    assert client.list_tools(("r", "c"), PUBLIC, AUTH).tools == ()


def test_br_107_list_deadline_played_at_real_value(net: UpNet) -> None:
    """The only deadline played in real time: 15 s, on `tools/list` (≤ 16 s)."""
    client = net.client()
    client.list_tools(("r", "c"), PUBLIC, AUTH)  # generation negotiated first
    net.upstreams["pub.mcp.test"].faults.append(Fault("hang", on="tools/list", seconds=30))
    started = time.monotonic()
    err = _error(lambda: client.list_tools(("r", "c"), PUBLIC, AUTH))
    elapsed = time.monotonic() - started
    assert err.code == "deadline_exceeded"
    assert LIST_S <= elapsed < LIST_S + 1.0


def test_br_107_call_deadline_is_60_s_on_the_same_path(net: UpNet) -> None:
    """60 s by the injected clock: the requested wait is 60 s, the exchange is cut."""
    up = net.upstreams["pub.mcp.test"]
    clock = FakeClock()
    client = net.client(clock)
    client.list_tools(("r", "c"), PUBLIC, AUTH)
    up.faults.append(Fault("hang", on="tools/call", seconds=30))
    clock.expect_hang(lambda: len(up.calls("tools/call")) == 1)
    err = _error(lambda: client.call_tool(("r", "c"), PUBLIC, AUTH, "read", {}))
    assert err.code == "deadline_exceeded"
    assert clock.waits[-1] == CALL_S == 60.0
    assert len(up.calls("tools/call")) == 1


@pytest.mark.parametrize("kind", ["drip_headers", "drip_body", "drip_sse"])
def test_br_107_drip_is_cut_at_the_deadline(net: UpNet, kind: str) -> None:
    up = net.upstreams["pub.mcp.test"]
    clock = FakeClock()
    client = net.client(clock)
    client.list_tools(("r", "c"), PUBLIC, AUTH)
    up.faults.append(Fault(kind, on="tools/list", seconds=20))
    lists = len(up.calls("tools/list"))
    clock.expect_hang(lambda: len(up.calls("tools/list")) > lists)
    started = time.monotonic()
    err = _error(lambda: client.list_tools(("r", "c"), PUBLIC, AUTH))
    assert err.code == "deadline_exceeded"
    assert clock.waits[-1] == LIST_S
    assert time.monotonic() - started < 3.0


def test_br_107_environment_is_ignored(net: UpNet, monkeypatch: pytest.MonkeyPatch) -> None:
    """`HTTPS_PROXY` is not followed; `SSL_CERT_FILE` does not make a certificate trusted.

    The designated proxy is a reachable fake upstream: a client that followed it would leave a
    request there (CONNECT or POST) and would dial its address (review 448, m3)."""
    proxy = net.upstream("proxy.mcp.test", "modern")
    # Mocked routable address; no external connection is made.
    net.addresses["proxy.mcp.test"] = ["11.66.66.66"]
    # Mocked routable address; no external connection is made.
    net.addresses["pub.mcp.test"] = ["11.55.55.55"]
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://proxy.mcp.test:443")
    net.client().list_tools(("r", "c"), PUBLIC, AUTH)
    assert proxy.calls() == []
    # Mocked routable address; no external connection is made.
    assert {address for address, _ in net.dialed} == {"11.55.55.55"}
    monkeypatch.setenv("SSL_CERT_FILE", str(net.folder / "cert.pem"))
    untrusted = UpstreamClient(Transport(resolve=net.resolve, dial=net.dial))
    err = _error(lambda: untrusted.list_tools(("r", "x"), PUBLIC, AUTH))
    assert err.code == "upstream_unavailable"


def test_br_107_invalid_session_id_is_refused(net: UpNet) -> None:
    net.upstreams["int-a.mcp.test"].faults.append(Fault("bad_session", on="initialize"))
    client = net.client()
    err = _error(lambda: client.list_tools(("r", "c"), INTERNAL, AUTH))
    assert (err.code, err.reason) == ("upstream_unavailable", "session")
    assert client.session(("r", "c")) is None


def test_br_107_token_with_control_byte_is_refused_before_sending(net: UpNet) -> None:
    before = len(net.upstreams["pub.mcp.test"].calls())
    err = _error(lambda: net.client().list_tools(("r", "c"), PUBLIC, "Bearer a\r\nX-Evil: 1"))
    assert err.code == "internal_error"
    assert len(net.upstreams["pub.mcp.test"].calls()) == before


# -- AC-06: addresses ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "internal", "addresses"),
    [
        ("https://x.mcp.test/mcp", False, ["10.0.0.5"]),  # private without exception
        ("https://x.mcp.test/mcp", False, ["127.0.0.1"]),  # loopback
        ("https://x.mcp.test/mcp", False, ["169.254.1.1"]),  # link-local
        # Mocked routable address; no external connection is made.
        ("https://x.mcp.test/mcp", False, ["11.1.1.1", "10.0.0.5"]),  # mixed
        # Mocked routable address; no external connection is made.
        ("http://x.mcp.test/mcp", True, ["11.1.1.1"]),  # http to public, flag set
        # Mocked routable address; no external connection is made.
        ("http://x.mcp.test/mcp", True, ["10.0.0.5", "11.1.1.1"]),  # mixed, flag set
        ("http://x.mcp.test/mcp", True, ["100.64.0.9"]),  # CGNAT
        ("http://x.mcp.test/mcp", True, ["127.0.0.1"]),  # loopback, flag set
        ("https://127.1/mcp", False, []),
        ("https://0x7f000001/mcp", False, []),
        ("https://[::ffff:127.0.0.1]/mcp", False, []),
    ],
)
def test_br_107_refused_addresses_open_no_connection(
    net: UpNet, url: str, internal: bool, addresses: list[str]
) -> None:
    if addresses:
        net.addresses["x.mcp.test"] = addresses
    err = _error(lambda: net.client().list_tools(("r", "c"), Target(url, internal), AUTH))
    assert err.code == "upstream_unavailable"
    assert net.dialed == []


def test_br_107_ipv6_unreachable_falls_back_to_ipv4(net: UpNet) -> None:
    """HULL-32 fixture: IPv6 first and unreachable, IPv4 reachable: the call succeeds."""
    # Mocked routable address; no external connection is made.
    net.addresses["pub.mcp.test"] = ["2a00:1450:4007::1", "11.2.3.4"]
    port = int(net.tls.server_address[1])

    def dial(address: str, _port: int) -> tuple[str, int]:
        net.dialed.append((address, _port))
        return ("127.0.0.1", 1) if ":" in address else ("127.0.0.1", port)

    client = UpstreamClient(Transport(context=net.context, resolve=net.resolve, dial=dial))
    started = time.monotonic()
    assert client.list_tools(("r", "c"), PUBLIC, AUTH).version == MODERN
    assert time.monotonic() - started < LIST_S
    # Mocked routable address; no external connection is made.
    assert net.dialed[0][0] == "2a00:1450:4007::1"


def test_br_107_resolution_turned_public_is_refused_in_http(net: UpNet) -> None:
    client = net.client()
    client.list_tools(("r", "c"), INTERNAL, AUTH)
    dialed = len(net.dialed)
    # Mocked routable address; no external connection is made.
    net.addresses["int-a.mcp.test"] = ["11.7.7.7"]
    err = _error(lambda: client.call_tool(("r", "c"), INTERNAL, AUTH, "read", {}))
    assert err.code == "upstream_unavailable"
    assert len(net.dialed) == dialed


def test_br_107_only_listed_headers_reach_the_upstream(net: UpNet) -> None:
    for target in (PUBLIC, INTERNAL):
        client = net.client()
        client.list_tools(("r", "c"), target, AUTH)
        client.call_tool(("r", "c"), target, AUTH, "read", {"a": 1})
        client.close(("r", "c"))
    for up in (net.upstreams["pub.mcp.test"], net.upstreams["int-a.mcp.test"]):
        for req in up.calls():
            assert set(req.headers) <= ALLOWED, req.headers
            # `Host`: the normalized host of the record's URL, never the address reached (H24)
            assert req.headers["host"] == up.host
            assert req.headers.get("authorization") in (None, AUTH)
            assert req.headers.get("user-agent") == "hullgate"


# -- BR-107 amended: headers of the 2026-07-28 generation ---------------------------------

SCHEMA = {
    "type": "object",
    "properties": {
        "repo": {"type": "string", "x-mcp-header": "Repo"},
        "flag": {"type": "boolean", "x-mcp-header": "Flag"},
    },
}


def test_br_107_modern_headers_come_from_the_body(net: UpNet) -> None:
    up = net.upstreams["pub.mcp.test"]
    up.tools.append(tool("edit.файл", SCHEMA))
    client = net.client()
    client.list_tools(("r", "c"), PUBLIC, AUTH)
    client.call_tool(("r", "c"), PUBLIC, AUTH, "edit.файл", {"repo": "репо", "flag": True}, SCHEMA)
    call = up.calls("tools/call")[-1]
    extra = {h for h in call.headers if h.startswith("mcp-param-")}
    assert extra == {"mcp-param-repo", "mcp-param-flag"}
    assert set(call.headers) - extra <= ALLOWED
    assert call.headers["mcp-method"] == "tools/call"
    encoded = base64.b64encode("edit.файл".encode()).decode()
    assert call.headers["mcp-name"] == f"=?base64?{encoded}?="
    assert (
        call.headers["mcp-param-repo"] == f"=?base64?{base64.b64encode('репо'.encode()).decode()}?="
    )
    assert call.headers["mcp-param-flag"] == "true"
    assert up.executions == {"edit.файл": 1}


@pytest.mark.parametrize(
    "arguments", [{"repo": {"object": 1}}, {"repo": ["list"]}, {"repo": "x" * 9000}]
)
def test_br_107_unencodable_annotated_argument_is_refused(
    net: UpNet, arguments: dict[str, object]
) -> None:
    up = net.upstreams["pub.mcp.test"]
    up.tools.append(tool("w", SCHEMA))
    client = net.client()
    client.list_tools(("r", "c"), PUBLIC, AUTH)
    err = _error(lambda: client.call_tool(("r", "c"), PUBLIC, AUTH, "w", arguments, SCHEMA))
    assert err.code == "request_too_large"
    assert up.calls("tools/call") == []


def test_br_107_missing_param_header_is_what_the_upstream_rejects(net: UpNet) -> None:
    """Control: without the schema (so without `Mcp-Param-*`), the conforming upstream refuses the
    call (400)."""
    up = net.upstreams["pub.mcp.test"]
    up.tools.append(tool("w", SCHEMA))
    client = net.client()
    client.list_tools(("r", "c"), PUBLIC, AUTH)
    err = _error(lambda: client.call_tool(("r", "c"), PUBLIC, AUTH, "w", {"repo": "a"}))
    assert (err.code, err.reason) == ("upstream_unavailable", "status 400")
    assert up.executions == {}
