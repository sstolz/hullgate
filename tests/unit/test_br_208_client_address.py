"""BR-208 — a forwarded client address is believed only from a declared proxy."""

from __future__ import annotations

import ipaddress

import pytest

from hullgate.admin.client_address import client_address
from hullgate.config import Network

PROXIES: tuple[Network, ...] = (
    ipaddress.ip_network("172.18.0.0/16"),
    ipaddress.ip_network("10.0.0.1/32"),
)


def test_br_208_no_proxy_declared_ignores_forwarded_headers() -> None:
    assert client_address("172.18.0.5", ["198.51.100.9"], ()) == "172.18.0.5"


def test_br_208_undeclared_peer_header_is_ignored() -> None:
    assert client_address("192.0.2.4", ["198.51.100.9"], PROXIES) == "192.0.2.4"


def test_br_208_declared_proxy_gives_the_rightmost_untrusted_entry() -> None:
    assert client_address("172.18.0.5", ["198.51.100.9"], PROXIES) == "198.51.100.9"
    # a client-supplied leftmost entry is not believed: the proxy appended the real one
    # Mocked routable address; no external connection is made.
    assert client_address("172.18.0.5", ["6.6.6.6, 198.51.100.9"], PROXIES) == "198.51.100.9"
    # chained declared proxies are skipped from the right
    assert client_address("172.18.0.5", ["198.51.100.9, 10.0.0.1"], PROXIES) == "198.51.100.9"
    # several headers count in arrival order
    # Mocked routable address; no external connection is made.
    assert client_address("172.18.0.5", ["6.6.6.6", "198.51.100.9"], PROXIES) == "198.51.100.9"


@pytest.mark.parametrize("forwarded", [[], [""], ["garbage"], ["10.0.0.1"], ["172.18.0.9"]])
def test_br_208_declared_proxy_without_usable_header_gives_the_peer(forwarded: list[str]) -> None:
    assert client_address("172.18.0.5", forwarded, PROXIES) == "172.18.0.5"


def test_br_208_forms_are_normalised() -> None:
    assert client_address("172.18.0.5", ["[2001:db8::1]"], PROXIES) == "2001:db8::1"
    assert client_address("172.18.0.5", ["198.51.100.9:4711"], PROXIES) == "198.51.100.9"
    assert client_address("::ffff:198.51.100.9", [], ()) == "198.51.100.9"
    # Mocked routable address; no external connection is made.
    assert client_address("testclient", ["1.2.3.4"], PROXIES) == "testclient"
