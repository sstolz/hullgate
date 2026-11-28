"""BR-300 (AC-01) — the public listener reads the client address of BR-208 only: a forged
`X-Forwarded-For` from an undeclared peer, and from a loopback peer, leaves the address at the
peer; a declared proxy's header gives the rightmost entry outside the list, across several header
lines; probes never read it."""

from __future__ import annotations

import ipaddress
import json
import logging

import pytest

from hullgate import app as app_mod
from tests.support import Gateway, ap, mcp, standard_tree

ROLE = "mpx/cairn/staging/dev"
TRAEFIK = "172.30.0.2"
PROXIES = (ipaddress.ip_network("172.30.0.0/24"),)


def _last_address(gw: Gateway) -> str:
    refused = [e for e in gw.audit() if e["action"] == "access.refused"]
    address = json.loads(refused[-1]["detail"])["address"]
    assert isinstance(address, str)
    return address


@pytest.mark.parametrize("peer", ["198.51.100.20", "127.0.0.1"], ids=["undeclared", "loopback"])
def test_br_300_forged_header_from_an_undeclared_peer_changes_nothing(
    gw: Gateway, peer: str
) -> None:
    standard_tree(gw)
    with gw.client(peer=peer, proxies=PROXIES) as client:
        mcp(client, ROLE, "hg_bad", headers={"X-Forwarded-For": "203.0.113.9"})
    assert _last_address(gw) == peer


def test_br_300_declared_proxy_gives_rightmost_untrusted_entry(gw: Gateway) -> None:
    standard_tree(gw)
    with gw.client(peer=TRAEFIK, proxies=PROXIES) as client:
        mcp(client, ROLE, "hg_bad", headers={"X-Forwarded-For": "203.0.113.9, 192.0.2.77"})
    assert _last_address(gw) == "192.0.2.77"


def test_br_300_several_header_lines_are_joined_in_order(gw: Gateway) -> None:
    standard_tree(gw)
    with gw.client(peer=TRAEFIK, proxies=PROXIES) as client:
        response = client.post(
            ap(ROLE),
            headers=[
                ("Authorization", "Bearer hg_bad"),
                ("Content-Type", "application/json"),
                ("X-Forwarded-For", "203.0.113.9"),  # forged first line
                ("X-Forwarded-For", "192.0.2.78, 172.30.0.9"),  # appended by the proxies
            ],
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
    assert response.status_code == 401
    assert _last_address(gw) == "192.0.2.78"


@pytest.mark.parametrize("forwarded", ["not-an-address", "172.30.0.5, 172.30.0.6"])
def test_br_300_no_usable_header_falls_back_to_the_peer(gw: Gateway, forwarded: str) -> None:
    standard_tree(gw)
    with gw.client(peer=TRAEFIK, proxies=PROXIES) as client:
        mcp(client, ROLE, "hg_bad", headers={"X-Forwarded-For": forwarded})
    assert _last_address(gw) == TRAEFIK


def test_br_300_other_forwarded_headers_are_ignored(gw: Gateway) -> None:
    standard_tree(gw)
    with gw.client(peer=TRAEFIK, proxies=PROXIES) as client:
        mcp(client, ROLE, "hg_bad",
            headers={"Forwarded": "for=192.0.2.1", "X-Real-IP": "192.0.2.2"})  # fmt: skip
    assert _last_address(gw) == TRAEFIK


def test_br_300_undeclared_forwarder_is_logged_once_per_hour(
    gw: Gateway, caplog: pytest.LogCaptureFixture
) -> None:
    standard_tree(gw)
    caplog.set_level(logging.WARNING, logger="hullgate.public")
    with gw.client(peer="198.51.100.21") as client:
        for _ in range(3):
            mcp(client, ROLE, "hg_bad", headers={"X-Forwarded-For": "203.0.113.9"})
    lines = [r for r in caplog.records if r.name == "hullgate.public"]
    assert len(lines) == 1
    assert "198.51.100.21" in lines[0].getMessage()
    assert app_mod.UNDECLARED_LOG_S == 3600.0
