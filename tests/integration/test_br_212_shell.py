"""BR-212 (AC-11, ASGI part) — the console is a self-contained bundle served by the admin
listener under a strict CSP. The image side (no Node, bundle present) is
`tests/container/test_br_212_bundle.py`; the browser side (no external request, no CSP violation)
is `tests/browser/test_br_212_csp.py`."""

from __future__ import annotations

from pathlib import Path

import pytest

from hullgate.admin.app import CSP
from tests.admin_support import Console, bundle
from tests.support import Gateway

EXPECTED_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)


def test_br_212_csp_is_the_rule_text() -> None:
    assert CSP == EXPECTED_CSP


@pytest.mark.parametrize("path", ["/admin", "/admin/", "/admin/enrol/abc", "/admin/tree/x"])
def test_br_212_shell_needs_no_session_and_holds_no_data(
    gw: Gateway, tmp_path: Path, path: str
) -> None:
    root = bundle(tmp_path)
    console = Console(gw, static_dir=root)
    with console.client() as client:
        response = client.get(path)
        assert response.status_code == 200
        assert response.content == (root / "index.html").read_bytes()  # same for everyone
        assert response.headers["content-security-policy"] == EXPECTED_CSP
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        console.admin(client)
        assert client.get(path).content == (root / "index.html").read_bytes()


def test_br_212_assets_are_served_and_contained(gw: Gateway, tmp_path: Path) -> None:
    root = bundle(tmp_path)
    (tmp_path / "secret.txt").write_text("outside", encoding="utf-8")
    console = Console(gw, static_dir=root)
    with console.client() as client:
        asset = client.get("/admin/assets/app.js")
        assert asset.status_code == 200
        assert asset.headers["content-security-policy"] == EXPECTED_CSP
        for path in ("/admin/assets/../../secret.txt", "/admin/assets/%2e%2e/%2e%2e/secret.txt",
                     "/admin/assets/missing.js", "/admin/assets/"):  # fmt: skip
            assert client.get(path).status_code == 404, path


def test_br_212_without_a_bundle_admin_is_404_and_the_api_works(gw: Gateway) -> None:
    console = Console(gw)
    with console.client() as client:
        assert client.get("/admin").status_code == 404
        assert client.get("/admin/enrol/x").status_code == 404
        console.admin(client)
        assert console.call(client, "GET", "meta").status_code == 200


def test_br_212_every_admin_response_carries_the_headers(gw: Gateway) -> None:
    console = Console(gw)
    with console.client() as client:
        for response in (client.get("/admin"), client.get("/admin/api/v1/session"),
                         client.post("/admin/api/v1/auth/options", json={})):  # fmt: skip
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["referrer-policy"] == "no-referrer"
