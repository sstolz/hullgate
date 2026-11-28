"""BR-060 / HULL-31 — origin of the `redirect_uri` admitted in `form-action` on the barrier."""

from __future__ import annotations

import pytest

from hullgate.oauth import PAGE_HEADERS, consent_page_headers, redirect_origin_source


@pytest.mark.parametrize(
    ("uri", "source"),
    [
        ("https://claude.ai/api/mcp/auth_callback", "https://claude.ai"),
        ("https://Claude.AI/cb?x=1", "https://claude.ai"),
        ("https://user@claude.ai/cb", "https://claude.ai"),
        ("https://claude.ai:8443/cb", "https://claude.ai:8443"),
        ("http://localhost:4567/callback", "http://localhost:4567"),
        ("http://127.0.0.1:9/cb", "http://127.0.0.1:9"),
    ],
)
def test_br_060_redirect_origin_is_exact(uri: str, source: str) -> None:
    assert redirect_origin_source(uri) == source


@pytest.mark.parametrize(
    "uri",
    [
        "http://[::1]:4567/cb",  # IPv6 literal: outside the host-source grammar of CSP 3
        "https://*.example.test/cb",
        "https://exa;mple.test/cb",
        "https://exa'mple.test/cb",
        "https://exämple.test/cb",
        "https://example.test:99999/cb",
        "custom://example.test/cb",
        "https:///cb",
    ],
)
def test_br_060_inexpressible_origin_widens_nothing(uri: str) -> None:
    assert redirect_origin_source(uri) is None
    assert consent_page_headers(uri) == PAGE_HEADERS


def test_br_060_consent_headers_only_change_form_action() -> None:
    headers = consent_page_headers("https://claude.ai/api/mcp/auth_callback")
    assert headers["Content-Security-Policy"] == (
        "frame-ancestors 'none'; default-src 'none'; style-src 'unsafe-inline'; "
        "form-action 'self' https://claude.ai"
    )
    assert {k: v for k, v in headers.items() if k != "Content-Security-Policy"} == {
        k: v for k, v in PAGE_HEADERS.items() if k != "Content-Security-Policy"
    }
    assert PAGE_HEADERS["Content-Security-Policy"].endswith("form-action 'self'")
