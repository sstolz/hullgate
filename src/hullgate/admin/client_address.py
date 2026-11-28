"""The client address of a request (BR-208).

The socket peer, unless that peer lies in `TRUSTED_PROXIES`: then the rightmost `X-Forwarded-For`
entry outside `TRUSTED_PROXIES`. With `TRUSTED_PROXIES` empty, forwarded headers are ignored.
uvicorn's own `proxy_headers` would rewrite the peer before this function runs, so both listeners
start with `proxy_headers=False` (`hullgate.__main__`). HULL-70 reuses this function: since F-020
(BR-300) `scope_address` is the one reader of the peer and of `X-Forwarded-For` on both listeners.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Sequence
from typing import Any

from hullgate.config import Network

type Address = ipaddress.IPv4Address | ipaddress.IPv6Address


def parse(value: str) -> Address | None:
    """An IP address from a peer or a forwarded entry (`[v6]`, `v4:port` tolerated), or None."""
    text = value.strip()
    if text.startswith("["):
        text = text[1:].split("]", 1)[0]
    elif text.count(":") == 1:
        text = text.split(":", 1)[0]
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _trusted(address: Address | None, proxies: Sequence[Network]) -> bool:
    return address is not None and any(address in network for network in proxies)


def client_address(peer: str, forwarded: Iterable[str], proxies: Sequence[Network]) -> str:
    """The address a request is attributed to, as text.

    `forwarded` holds the values of every `X-Forwarded-For` header in arrival order; each may list
    several comma-separated entries, the rightmost being the one the nearest proxy appended. A peer
    that is not an IP address (a test client) is returned as is and is never trusted.
    """
    peer_address = parse(peer)
    if not proxies or not _trusted(peer_address, proxies):
        return str(peer_address) if peer_address is not None else peer
    entries = [item for value in forwarded for item in value.split(",") if item.strip()]
    for entry in reversed(entries):
        address = parse(entry)
        if address is None:
            break  # an unusable entry ends the chain the proxies vouch for
        if not _trusted(address, proxies):
            return str(address)
    return str(peer_address)


def scope_address(scope: Any, proxies: Sequence[Network]) -> tuple[str, str | None]:
    """The client address of an ASGI request (BR-208, BR-300), and the socket peer when that peer
    lies outside `proxies` yet carries `X-Forwarded-For` (BR-302's log line), else None.

    Several `X-Forwarded-For` lines are joined in arrival order before the rightmost reading;
    `Forwarded`, `X-Real-IP` and the other forwarded headers are never read."""
    peer = str((scope.get("client") or ("", 0))[0])
    forwarded = [
        value.decode("latin-1")
        for name, value in scope.get("headers", [])
        if name.lower() == b"x-forwarded-for"
    ]
    undeclared = peer if forwarded and not _trusted(parse(peer), proxies) else None
    return client_address(peer, forwarded, proxies), undeclared


def in_networks(address: str, networks: Iterable[Network]) -> bool:
    """True when `address` parses and lies in one of `networks`."""
    parsed = parse(address)
    return parsed is not None and any(parsed in network for network in networks)
