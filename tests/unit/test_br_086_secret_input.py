"""BR-086 — a secret comes in through a file or standard input, never an argument (AC-15)."""

from __future__ import annotations

import json
import secrets
from pathlib import Path

import pytest

from tests.support import REGISTRY, Gateway, standard_tree


@pytest.fixture
def leaked() -> str:
    value = "argv-secret-" + secrets.token_urlsafe(18)
    REGISTRY.secrets.append(value)
    return value


@pytest.mark.parametrize(
    "extra",
    [
        ["--secret", "{v}"],
        ["--token", "{v}"],
        ["{v}"],
        ["--value={v}"],
    ],
)
def test_br_086_value_in_argument_refused_and_not_echoed(
    gw: Gateway, leaked: str, extra: list[str]
) -> None:
    standard_tree(gw)
    gw.service()
    args = [a.replace("{v}", leaked) for a in extra]
    result = gw.cli(
        "connection", "add", "mpx/gitea-dev", "--service", "gitea-dev", "--provided",
        "--prefix", "gd", *args,
    )  # fmt: skip
    assert result.code == 2
    assert leaked not in result.out + result.err
    assert json.loads(gw.ok("connection", "list", "--json")) == []
    assert all(leaked not in json.dumps(e) for e in gw.audit())


def test_br_086_value_given_as_file_path_is_not_echoed(gw: Gateway, leaked: str) -> None:
    standard_tree(gw)
    gw.service()
    result = gw.cli(
        "connection", "add", "mpx/gitea-dev", "--service", "gitea-dev", "--provided",
        "--file", leaked, "--prefix", "gd",
    )  # fmt: skip
    assert result.code == 2
    assert leaked not in result.out + result.err


def test_br_086_application_secret_in_argument_refused(gw: Gateway, leaked: str) -> None:
    gw.service()
    result = gw.cli(
        "application", "declare", "app", "--service", "gitea-dev", "--mode", "declared",
        "--client-id", "c", "--secret", leaked,
    )  # fmt: skip
    assert result.code == 2
    assert leaked not in result.out + result.err


def test_br_086_file_and_stdin_trailing_newline_and_empty(gw: Gateway, tmp_path: Path) -> None:
    standard_tree(gw)
    gw.service()
    value = "file-secret-" + secrets.token_urlsafe(18)
    REGISTRY.secrets.append(value)
    secret_file = tmp_path / "token.txt"
    secret_file.write_text(value + "\n", encoding="utf-8")
    gw.ok(
        "connection", "add", "mpx/a", "--service", "gitea-dev", "--provided", "--file",
        str(secret_file), "--prefix", "ga",
    )  # fmt: skip
    for empty in ("", "\n", "  \n"):
        result = gw.cli(
            "connection", "add", "mpx/b", "--service", "gitea-dev", "--provided", "--file",
            "-", "--prefix", "gb", stdin=empty,
        )  # fmt: skip
        assert result.code == 2
        assert "empty value" in result.err
    no_file = gw.cli(
        "connection", "add", "mpx/b", "--service", "gitea-dev", "--provided", "--prefix", "gb"
    )
    assert no_file.code == 2
    conn = gw.connect()
    try:
        assert conn.execute("SELECT count(*) FROM connection").fetchone() == (1,)
    finally:
        conn.close()


@pytest.mark.parametrize(
    "url",
    [
        "https://user:{v}@gitea.example.test",
        "https://{v}@gitea.example.test",
        "http://user:{v}@gitea.example.test",
        "https://gitea.example.test/?token={v}",
        "https://gitea.example.test/#{v}",
    ],
)
def test_br_086_service_url_with_credentials_not_echoed(gw: Gateway, leaked: str, url: str) -> None:
    """Review 417 m1: the refusal of a service URL never repeats the URL."""
    result = gw.cli("service", "declare", "s", "--server", url.replace("{v}", leaked))
    assert result.code == 2
    assert "URL not repeated" in result.err
    assert leaked not in result.out + result.err
    assert all(leaked not in json.dumps(e) for e in gw.audit())
