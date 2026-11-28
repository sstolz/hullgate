"""BR-161 v2 smoke forms, exact request bytes and verdicts; no Docker needed."""

from __future__ import annotations

import io
import json
import posixpath
import re
import socket
import ssl
import sys
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import unquote

import pytest

from deploy.smoke import probe

FAMILIES = {
    "access-point": ("POST", "", "/p/{role}/mcp"),
    "rfc9728": ("GET", "/.well-known/oauth-protected-resource", "/p/{role}/mcp"),
    "rfc8414": ("GET", "/.well-known/oauth-authorization-server", "/oauth/p/{role}"),
    "token": ("POST", "", "/oauth/p/{role}/token"),
}
REFUSALS = {
    "foreign-id",
    "pre-feature",
    "encoded-slash-prefix",
    "encoded-slash-path",
    "trailing-slash",
    "uppercase-id",
}


@pytest.fixture(autouse=True)
def mounted_secrets() -> None:
    """Override the container fixture: these unit checks need no keys or containers."""


@pytest.fixture
def tree() -> list[dict[str, Any]]:
    return [
        {"level": "org", "path": "/", "public_id": "k7mq2xv4tnpd"},
        {"level": "client", "path": "acme"},
        {"level": "role", "path": "acme/app/prod/ops"},
    ]


@pytest.fixture(autouse=True)
def clear_results() -> None:
    probe.results.clear()


@pytest.mark.parametrize(
    ("identifier", "role"),
    [("k7mq2xv4tnpd", "acme/app/prod/ops"), ("aaaaaaaaaaaa", "other/new-app/dev/reader")],
)
@pytest.mark.parametrize("family", FAMILIES)
def test_br_161_forms_follow_tree(identifier: str, role: str, family: str) -> None:
    forms = probe.edge_forms(
        [
            {"level": "role", "path": role},
            {"level": "org", "public_id": identifier},
        ]
    )
    assert len(forms) == 64
    assert {form.family for form in forms} == set(FAMILIES)
    selected = [form for form in forms if form.family == family]
    method, metadata, template = FAMILIES[family]
    tail = template.format(role=role)
    canonical = f"{metadata}/o/{identifier}{tail}"
    assert {form.method for form in selected} == {method}
    assert [
        (form.path, form.expected_status) for form in selected if form.label == "canonical"
    ] == [(canonical, 200 if family in ("rfc9728", "rfc8414") else 401)]
    readings = {form.label: form.path for form in selected if form.expected_status is None}
    assert set(readings) == {
        "dot-foreign-parent",
        "dot-parent",
        "dot-current",
        "dot-encoded-parent",
        "dot-encoded-current",
        "slash-prefix",
        "slash-leading",
        "unreserved-path",
        "unreserved-id",
    }
    # Each observational form can normalise to the canonical route. Leading // is
    # deliberately collapsed here (POSIX normpath alone preserves that spelling).
    for path in readings.values():
        assert path != canonical
        assert posixpath.normpath("/" + unquote(path).lstrip("/")) == canonical
    assert "/x/../" in readings["dot-parent"]
    assert "/./" in readings["dot-current"]
    assert "/x/%2e%2e/" in readings["dot-encoded-parent"]
    assert "/%2e/" in readings["dot-encoded-current"]
    assert readings["slash-leading"] == "/" + canonical
    assert f"/o/{identifier}//" in readings["slash-prefix"]
    assert f"/p/%{ord(role[0]):02X}{role[1:]}" in readings["unreserved-path"]
    assert f"/o/%{ord(identifier[0]):02X}{identifier[1:]}/" in readings["unreserved-id"]
    refusals = {form.label: form.path for form in selected if form.expected_status == 404}
    assert set(refusals) == REFUSALS
    foreign = refusals["foreign-id"][len(metadata) :].split("/")[2]
    assert re.fullmatch(r"[a-z2-7]{12}", foreign)
    assert foreign != identifier
    assert readings["dot-foreign-parent"] == f"{metadata}/o/{foreign}/../{identifier}{tail}"
    assert refusals == {
        "foreign-id": f"{metadata}/o/{foreign}{tail}",
        "pre-feature": metadata + tail,
        "encoded-slash-prefix": f"{metadata}/o%2F{identifier}{tail}",
        "encoded-slash-path": canonical.replace(role, role.replace("/", "%2F", 1)),
        "trailing-slash": canonical + "/",
        "uppercase-id": f"{metadata}/o/{identifier.upper()}{tail}",
    }


@pytest.mark.parametrize("fault", ["empty", "no-role", "two-orgs", "bad-id", "bad-role", "digits"])
def test_br_161_bad_tree_cannot_pass_vacuously(tree: list[dict[str, Any]], fault: str) -> None:
    if fault == "empty":
        tree.clear()
    elif fault == "no-role":
        tree.pop()
    elif fault == "two-orgs":
        tree.append(tree[0])
    elif fault == "bad-id":
        tree[0]["public_id"] = "wrong"
    elif fault == "bad-role":
        tree[-1]["path"] = "acme/app/prod/ops\r\nInjected: yes"
    else:
        tree[0]["public_id"] = "234567234567"
    with pytest.raises(ValueError, match=r"edge readings need|invalid identifier|no letters"):
        probe.edge_forms(tree)


@pytest.mark.parametrize(
    ("status", "headers", "expected"),
    [
        (404, [], True),
        (404, [("Content-Type", "application/json")], True),
        (200, [], False),
        (401, [], False),
        (301, [], False),
        (307, [], False),
        (500, [], False),
        (429, [], False),
        ("ERROR:TimeoutError", [], False),
        (404, [("Location", "/somewhere")], False),
        (404, [("lOcAtIoN", "")], False),
        (404, [("Content-Type", "application/json"), ("location", "")], False),
    ],
)
def test_br_161_404_requires_no_location(
    status: int | str, headers: list[tuple[str, str]], expected: bool
) -> None:
    assert probe.edge_404(status, headers, b'{"error":"not_found"}') is expected


@pytest.mark.parametrize("reading_status", [200, 301, 401, 404, 429, 500, "timeout"])
def test_br_161_readings_never_assert(
    tree: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    reading_status: int | str,
) -> None:
    forms = probe.edge_forms(tree)
    by_path = {form.path: form for form in forms}
    calls = []

    def respond(host: str, method: str, path: str) -> tuple[int, list[tuple[str, str]], bytes]:
        calls.append((method, path))
        form = by_path[path]
        if form.expected_status is None:
            if reading_status == "timeout":
                raise TimeoutError
            return int(reading_status), [("Location", "/anything")], b"not JSON"
        return form.expected_status, [], b'{"error":"not_found"}'

    monkeypatch.setattr(probe, "edge_request", respond)
    monkeypatch.setattr(
        sys, "argv", ["probe.py", "edge-readings", "smoke.invalid", json.dumps(tree)]
    )
    assert probe.main() == 0
    assert calls == [(form.method, form.path) for form in forms]
    assert len(calls) == 64
    assert probe.results == [True] * 28
    lines = capsys.readouterr().out.splitlines()
    expected_status = "ERROR:TimeoutError" if reading_status == "timeout" else str(reading_status)
    assert [line for line in lines if line.startswith("READING")] == [
        f"READING edge {form.family} {form.label} {expected_status}"
        for form in forms
        if form.expected_status is None
    ]
    assert len([line for line in lines if line.startswith("CHECK edge-404 OK")]) == 28


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("label", sorted(REFUSALS))
def test_br_161_each_required_refusal_affects_exit_code(
    tree: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    family: str,
    label: str,
) -> None:
    forms = probe.edge_forms(tree)
    statuses = {form.path: form.expected_status for form in forms}
    bad = next(form.path for form in forms if (form.family, form.label) == (family, label))

    def respond(host: str, method: str, path: str) -> tuple[int, list[tuple[str, str]], bytes]:
        return (200 if path == bad else statuses[path] or 200), [], b'{"error":"not_found"}'

    monkeypatch.setattr(probe, "edge_request", respond)
    monkeypatch.setattr(
        sys, "argv", ["probe.py", "edge-readings", "smoke.invalid", json.dumps(tree)]
    )
    assert probe.main() == 1
    assert probe.results.count(False) == 1


@pytest.mark.parametrize("body", [b"404 page not found\n", b'{"error":"not_found"}'])
def test_br_161_unrouted_gateway_fails_control(
    tree: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    body: bytes,
) -> None:
    monkeypatch.setattr(probe, "edge_request", lambda *args: (404, [], body))
    monkeypatch.setattr(
        sys, "argv", ["probe.py", "edge-readings", "smoke.invalid", json.dumps(tree)]
    )
    assert probe.main() == 1
    lines = capsys.readouterr().out.splitlines()
    for family in FAMILIES:
        assert any(line.startswith(f"CHECK edge-404 KO {family} canonical 404") for line in lines)
    assert probe.results.count(False) == (4 if body.startswith(b"{") else 28)


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("status", [404, 429, 500, 301, "timeout"])
def test_br_161_each_canonical_must_be_served(
    tree: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    family: str,
    status: int | str,
) -> None:
    forms = {form.path: form for form in probe.edge_forms(tree)}

    def respond(host: str, method: str, path: str) -> tuple[int, list[tuple[str, str]], bytes]:
        form = forms[path]
        if (form.family, form.label) == (family, "canonical"):
            if status == "timeout":
                raise TimeoutError
            return int(status), [], b""
        return form.expected_status or 200, [], b'{"error":"not_found"}'

    monkeypatch.setattr(probe, "edge_request", respond)
    monkeypatch.setattr(
        sys, "argv", ["probe.py", "edge-readings", "smoke.invalid", json.dumps(tree)]
    )
    assert probe.main() == 1
    assert probe.results.count(False) == 1


@pytest.mark.parametrize(
    ("status", "headers", "body"),
    [
        (404, [], b"404 page not found\n"),
        (404, [], b""),
        (404, [], b"\xff"),
        (404, [], b'{"error":'),
        (404, [], b"null"),
        (404, [], b"[]"),
        (404, [], b'"not_found"'),
        (404, [], b'{"error":"rate_limited"}'),
        (404, [("Location", "/elsewhere")], b'{"error":"not_found"}'),
        (429, [("Retry-After", "60")], b'{"error":"rate_limited"}'),
        ("timeout", [], b""),
    ],
)
def test_br_161_refusal_requires_gateway_json(
    tree: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    status: int | str,
    headers: list[tuple[str, str]],
    body: bytes,
) -> None:
    forms = {form.path: form for form in probe.edge_forms(tree)}

    def respond(host: str, method: str, path: str) -> tuple[int, list[tuple[str, str]], bytes]:
        form = forms[path]
        if form.expected_status == 404:
            if status == "timeout":
                raise TimeoutError
            return int(status), headers, body
        return form.expected_status or 200, [], b""

    monkeypatch.setattr(probe, "edge_request", respond)
    monkeypatch.setattr(
        sys, "argv", ["probe.py", "edge-readings", "smoke.invalid", json.dumps(tree)]
    )
    assert probe.main() == 1
    assert probe.results.count(False) == 24


@pytest.mark.parametrize(
    "path", ["//o/abc//p/x", "/o/abc/./x/../p/x", "/o/%61bc/%2e/%2e%2e/p/a%2Fb"]
)
def test_br_161_transport_sends_exact_request_target(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    raw, connection, context = MagicMock(), MagicMock(), MagicMock()
    raw.__enter__.return_value = raw
    connection.__enter__.return_value = connection
    connection.makefile.return_value = io.BytesIO(
        b'HTTP/1.1 404 Not Found\r\nContent-Length: 21\r\nlOcAtIoN: \r\n\r\n{"error":"not_found"}'
    )
    context.wrap_socket.return_value = connection
    connect = MagicMock(return_value=raw)
    monkeypatch.setattr(socket, "create_connection", connect)
    monkeypatch.setattr(ssl, "create_default_context", lambda: context)
    status, headers, body = probe.edge_request("smoke.invalid", "POST", path)
    connect.assert_called_once_with(("traefik", 443), timeout=5)
    context.wrap_socket.assert_called_once_with(raw, server_hostname="smoke.invalid")
    connection.sendall.assert_called_once_with(
        f"POST {path} HTTP/1.1\r\nHost: smoke.invalid\r\n"
        "Content-Length: 0\r\nConnection: close\r\n\r\n".encode("ascii")
    )
    assert status == 404
    assert body == b'{"error":"not_found"}'
    assert not probe.edge_404(status, headers, body)
