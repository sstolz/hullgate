"""BR-084 — every outbound call toward a provider is bounded (AC-13).

The bounded client (`hullgate.outbound.Outbound`) is played with a test resolver, TLS context and
`dial` function; its address policy is not injectable. Public resolutions are fictitious: `dial`
redirects the verified address to the test's local server and logs the address the client meant
to reach.

Deadline: the 10 s bound is replaced by 1 s for the dripping modes (same mechanism, without
waiting 10 s six times); one case keeps the real bound.
"""

from __future__ import annotations

import contextlib
import os
import socket
import ssl
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from hullgate import outbound
from hullgate.cimd import public_address
from hullgate.outbound import ANOMALY, Failure, InternalHost, Outbound, default_context
from tests import fake_provider
from tests.support import Gateway

# Mocked routable address; no external connection is made.
PUBLIC = "11.1.2.3"


class Drip:
    """TLS server that drips its response (headers included) byte by byte."""

    def __init__(self, folder: Path, mode: str, slow_head: bool) -> None:
        cert, key = fake_provider.make_cert(folder, ["drip.example.test"])
        self.cert = cert
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        self.sock = tls.wrap_socket(socket.create_server(("127.0.0.1", 0)), server_side=True)
        self.port = int(self.sock.getsockname()[1])
        version = "HTTP/1.0" if mode == "http10" else "HTTP/1.1"
        conn_header = "Connection: close\r\n" if mode == "close" else ""
        body = b'{"access_token": "x", "token_type": "bearer"}'
        self.raw = (
            f"{version} 200 OK\r\nContent-Type: application/json\r\n{conn_header}"
            f"Content-Length: {len(body)}\r\n\r\n"
        ).encode() + body
        self.slow_head = slow_head
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.answer, args=(conn,), daemon=True).start()

    def answer(self, conn: socket.socket) -> None:
        try:
            conn.recv(65536)
            head, _, body = self.raw.partition(b"\r\n\r\n")
            if not self.slow_head:
                conn.sendall(head + b"\r\n\r\n")
            for byte in self.raw if self.slow_head else body:
                conn.sendall(bytes([byte]))
                time.sleep(0.2)
        except OSError:
            return
        finally:
            conn.close()

    def client(self) -> Outbound:
        return Outbound(
            context=ssl.create_default_context(cafile=str(self.cert)),
            resolve=lambda _h, _p: [PUBLIC],
            dial=lambda _a, _p: ("127.0.0.1", self.port),
        )


@pytest.mark.parametrize("mode", ["keepalive", "close", "http10"])
@pytest.mark.parametrize("slow_head", [True, False], ids=["headers", "body"])
def test_br_084_trickle_cut_at_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, slow_head: bool
) -> None:
    monkeypatch.setattr(outbound, "DEADLINE_S", 1.0)
    drip = Drip(tmp_path, mode, slow_head)
    started = time.monotonic()
    with pytest.raises(Failure) as err:
        drip.client().request("POST", "https://drip.example.test/token", form={"a": "b"})
    assert time.monotonic() - started < 1.0 + 0.5
    assert err.value.kind == "uncertain"  # request sent, response missing


def test_br_084_real_ten_second_deadline(tmp_path: Path) -> None:
    drip = Drip(tmp_path, "keepalive", True)
    started = time.monotonic()
    with pytest.raises(Failure):
        drip.client().request("GET", "https://drip.example.test/")
    assert 9.5 <= time.monotonic() - started < 10.0 + 1.0


@pytest.fixture
def net(gw: Gateway) -> fake_provider.Net:
    return gw.net


def _hits(net: fake_provider.Net, host: str) -> int:
    provider = net.providers.get(host)
    return 0 if provider is None else len(provider.journal)


@pytest.mark.parametrize(
    "addresses",
    [
        ["10.0.0.5"],
        ["127.0.0.1"],
        ["169.254.169.254"],
        ["0.0.0.0"],  # noqa: S104 (resolved address refused, nothing listening)
        [PUBLIC, "10.0.0.5"],
    ],
    ids=["privee", "bouclage", "lien-local", "non-specifiee", "mixte"],
)
def test_br_084_non_public_addresses_refused_before_any_request(
    net: fake_provider.Net, addresses: list[str]
) -> None:
    net.addresses["p.example.test"] = addresses
    with pytest.raises(Failure) as err:
        net.outbound().request("GET", "https://p.example.test/.well-known/openid-configuration")
    assert err.value.kind == ANOMALY
    assert "address refused" in err.value.reason
    assert net.dialed == []
    assert _hits(net, "p.example.test") == 0


@pytest.mark.parametrize(
    "url", ["https://127.1/", "https://0x7f000001/", "https://[::ffff:127.0.0.1]/"]
)
def test_br_084_ip_literals_normalized_and_refused(net: fake_provider.Net, url: str) -> None:
    internal = InternalHost.of(url)
    assert internal.host == "127.0.0.1"
    with pytest.raises(Failure):
        net.outbound().request("GET", url, internal=internal)
    assert net.resolved == []
    assert net.dialed == []


def test_br_084_plain_http_redirect_and_size(net: fake_provider.Net) -> None:
    with pytest.raises(Failure) as err:
        net.outbound().request("GET", "http://p.example.test/")
    assert err.value.kind == ANOMALY
    assert net.resolved == []
    provider = net.provider("p.example.test")
    provider.script.append(fake_provider.Reply(size=300 * 1024))
    with pytest.raises(Failure) as err:
        net.outbound().request("POST", f"{provider.base}{fake_provider.TOKEN}", form={})
    assert (err.value.kind, err.value.reason) == (ANOMALY, "response too large")
    provider.script.append(
        fake_provider.Reply(status=302, headers={"Location": "https://q.example.test/"})
    )
    response = net.outbound().request("POST", f"{provider.base}{fake_provider.TOKEN}", form={})
    assert response.status == 302  # returned, never followed (classified "anomaly" by BR-092)
    assert "q.example.test" not in net.providers


def test_br_084_connects_to_verified_address_despite_rebinding(net: fake_provider.Net) -> None:
    answers = [[PUBLIC], ["10.0.0.5"]]

    def resolve(host: str, port: int) -> list[str]:
        net.resolved.append(host)
        return answers.pop(0)

    client = Outbound(context=net.context, resolve=resolve, dial=net.dial)
    response = client.request("GET", "https://p.example.test/.well-known/openid-configuration")
    assert response.status == 200
    assert net.resolved == ["p.example.test"]  # one resolution per call, verified then reached
    assert net.dialed == [(PUBLIC, 443)]
    with pytest.raises(Failure):
        client.request("GET", "https://p.example.test/.well-known/openid-configuration")
    assert net.dialed == [(PUBLIC, 443)]


def test_br_084_internal_host_exception_named_at_declaration(gw: Gateway) -> None:
    gw.net.addresses["gitea.example.test"] = ["10.0.0.5", "10.0.0.6"]
    refused = gw.cli(
        "service",
        "declare",
        "gitea",
        "--server",
        "https://gitea.example.test",
        "--model",
        "gitea",
    )
    assert refused.code == 2
    assert "address refused" in refused.err
    out = gw.ok(
        "service",
        "declare",
        "gitea",
        "--server",
        "https://gitea.example.test",
        "--model",
        "gitea",
        "--internal-host",
    )
    assert "addresses admitted at declaration 10.0.0.5, 10.0.0.6" in out
    entry = [e for e in gw.audit() if e["action"] == "service.declare"][-1]
    assert '"admitted_addresses": ["10.0.0.5", "10.0.0.6"]' in entry["after"]
    internal = InternalHost.of("https://gitea.example.test")
    client = gw.outbound
    # same host to loopback or link-local: refused despite the exception
    for bad in (["127.0.0.1"], ["169.254.169.254"]):
        gw.net.addresses["gitea.example.test"] = bad
        with pytest.raises(Failure):
            client.request("GET", "https://gitea.example.test/", internal=internal)
    # same host, other port: the exception does not apply
    gw.net.addresses["gitea.example.test"] = ["10.0.0.5"]
    with pytest.raises(Failure):
        client.request("GET", "https://gitea.example.test:8443/", internal=internal)
    assert client.request("GET", "https://GITEA.example.test./x", internal=internal).status == 404
    # loopback literals declared as internal host: refused at declaration
    for literal in ("127.1", "0x7f000001", "[::ffff:127.0.0.1]"):
        result = gw.cli(
            "service", "declare", "loop", "--server", f"https://{literal}", "--internal-host"
        )
        assert result.code == 2, literal


def test_br_084_environment_proxy_and_ca_ignored(
    net: fake_provider.Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = socket.create_server(("127.0.0.1", 0))
    proxy.settimeout(0.5)
    port = proxy.getsockname()[1]
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{port}")
    monkeypatch.setenv("SSL_CERT_FILE", str(net.cert_path))
    response = net.outbound().request(
        "GET", "https://p.example.test/.well-known/openid-configuration"
    )
    assert response.status == 200  # the call reaches the intended server
    with pytest.raises(TimeoutError):
        proxy.accept()  # the fake proxy receives no connection
    proxy.close()
    # the default context does not read SSL_CERT_FILE: the test certificate is not in it
    assert os.environ["SSL_CERT_FILE"] == str(net.cert_path)
    client = Outbound(context=default_context(), resolve=net.resolve, dial=net.dial)
    with pytest.raises(Failure) as err:
        client.request("GET", "https://p.example.test/.well-known/openid-configuration")
    assert err.value.kind == "transient"


# ---------------------------------------------------------------------------------------------
# HULL-32 — fallback to the next admitted addresses, under the global deadline
# ---------------------------------------------------------------------------------------------

# Mocked routable address; no external connection is made.
PUBLIC_V6 = "2606:4700::1111"  # fictitious public, never reached: `dial` redirects it


def _closed_port() -> int:
    sock = socket.create_server(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()  # nothing listens any more: connection refused
    return port


def _dial_failing_first(net: fake_provider.Net, failing: set[str]) -> outbound.Dial:
    dead = _closed_port()

    def dial(address: str, port: int) -> tuple[str, int]:
        target = net.dial(address, port)  # logs the address the client meant to reach
        return ("127.0.0.1", dead) if address in failing else target

    return dial


def test_br_084_falls_back_to_next_admitted_address(net: fake_provider.Net) -> None:
    """Dual-stack production server with unreachable IPv6: the next IPv4 is reached."""
    net.addresses["p.example.test"] = [PUBLIC_V6, PUBLIC]
    client = Outbound(
        context=net.context, resolve=net.resolve, dial=_dial_failing_first(net, {PUBLIC_V6})
    )
    response = client.request("GET", "https://p.example.test/.well-known/openid-configuration")
    assert response.status == 200
    assert net.dialed == [(PUBLIC_V6, 443), (PUBLIC, 443)]  # resolver order, never sorted
    assert net.resolved == ["p.example.test"]  # a single resolution: pinning kept


def test_br_084_all_addresses_failing_same_refusal(net: fake_provider.Net) -> None:
    net.addresses["p.example.test"] = [PUBLIC_V6, PUBLIC]
    client = Outbound(
        context=net.context,
        resolve=net.resolve,
        dial=_dial_failing_first(net, {PUBLIC_V6, PUBLIC}),
    )
    with pytest.raises(Failure) as err:
        client.request("GET", "https://p.example.test/.well-known/openid-configuration")
    assert (err.value.kind, err.value.reason) == ("transient", "connection interrupted")
    assert net.dialed == [(PUBLIC_V6, 443), (PUBLIC, 443)]
    assert _hits(net, "p.example.test") == 0


@pytest.mark.parametrize(
    "addresses",
    [[PUBLIC_V6, "10.0.0.5"], ["10.0.0.5", PUBLIC], [PUBLIC, PUBLIC_V6, "fd00::5"]],
    ids=["private-second", "private-first", "private-ipv6-last"],
)
def test_br_084_private_address_in_list_still_refused_without_dial(
    net: fake_provider.Net, addresses: list[str]
) -> None:
    net.addresses["p.example.test"] = addresses
    client = Outbound(
        context=net.context, resolve=net.resolve, dial=_dial_failing_first(net, set())
    )
    with pytest.raises(Failure) as err:
        client.request("GET", "https://p.example.test/.well-known/openid-configuration")
    assert err.value.kind == ANOMALY
    assert "address refused" in err.value.reason
    assert net.dialed == []


def test_br_084_fallback_never_dials_an_unadmitted_address() -> None:
    """The guard runs again before each dialled address, even if the list came from elsewhere."""
    dialed: list[str] = []

    def dial(address: str, port: int) -> tuple[str, int]:
        dialed.append(address)
        return "127.0.0.1", port

    conn = outbound._Pinned(
        "p.example.test",
        443,
        ["10.0.0.5"],
        public_address,
        dial,
        ssl.create_default_context(),
        time.monotonic() + 1.0,
    )
    with pytest.raises(Failure) as err:
        conn.connect()
    assert err.value.kind == ANOMALY
    assert dialed == []


@pytest.fixture
def saturated() -> Iterator[int]:
    """Listener whose backlog is full: a new SYN stays unanswered (pending connection)."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(0)
    port = int(listener.getsockname()[1])
    fillers = []
    for _ in range(4):
        filler = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        filler.setblocking(False)
        with contextlib.suppress(BlockingIOError):
            filler.connect(("127.0.0.1", port))
        fillers.append(filler)
    time.sleep(0.1)
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.3)
    try:
        probe.connect(("127.0.0.1", port))
        pytest.skip("the listen backlog does not hold SYNs on this host")
    except TimeoutError:
        pass
    finally:
        probe.close()
    try:
        yield port
    finally:
        for filler in fillers:
            filler.close()
        listener.close()


def test_br_084_fallback_bounded_by_global_deadline(
    net: fake_provider.Net, monkeypatch: pytest.MonkeyPatch, saturated: int
) -> None:
    """Three addresses whose connection hangs: the bound stays the global deadline, not 3 × 1 s."""
    monkeypatch.setattr(outbound, "DEADLINE_S", 1.0)
    # Mocked routable address; no external connection is made.
    first, second, third = PUBLIC_V6, PUBLIC, "11.9.9.9"
    net.addresses["p.example.test"] = [first, second, third]
    attempts: list[float] = []

    def dial(address: str, port: int) -> tuple[str, int]:
        attempts.append(time.monotonic())
        return "127.0.0.1", saturated

    client = Outbound(context=net.context, resolve=net.resolve, dial=dial)
    started = time.monotonic()
    with pytest.raises(Failure) as err:
        client.request("GET", "https://p.example.test/")
    assert time.monotonic() - started < 1.0 + 0.5
    assert err.value.kind == "transient"  # nothing was sent
    time.sleep(0.3)  # the worker thread has finished: no attempt after the deadline
    assert all(t - started < 1.0 + 0.1 for t in attempts)
