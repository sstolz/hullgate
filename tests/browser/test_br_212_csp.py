"""BR-212 (AC-11, browser part) — the console requests nothing from another origin and raises no
CSP violation, over every page the browser run opened (this file runs after the sign-in flow);
the shell holds no data."""

from __future__ import annotations

from typing import Any

from browser_support import CONSOLE, EMAIL, Watched


def test_br_212_no_external_request_and_no_csp_violation(watches: list[Watched]) -> None:
    assert len(watches) >= 4
    for w in watches:
        assert w.requests, "a watched page made no request"
        assert w.external() == []
        assert w.csp_violations() == []


def test_br_212_shell_holds_no_data(browser: Any) -> None:
    context = browser.new_context()
    try:
        response = context.request.get(f"{CONSOLE}/admin")
        assert response.status == 200
        body = response.text()
        headers = response.headers
    finally:
        context.close()
    assert EMAIL not in body
    assert "adm_" not in body
    assert headers["content-security-policy"].startswith("default-src 'self'")
    assert "<script>" not in body  # no inline script: `script-src 'self'`
