"""BR-330: reserved fixture hosts and explicit public-service pins."""

from pathlib import Path

import fixture_names as guard
import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_br_330_fixture_names() -> None:
    assert guard.findings(ROOT) == []


@pytest.mark.parametrize("host", ["invented.dev", "person.mailhost.io", "example.com.evil.dev"])
def test_br_330_unreserved_host_fails(tmp_path: Path, host: str) -> None:
    path = "tests/fixture.txt"
    (tmp_path / "tests").mkdir()
    (tmp_path / path).write_text(f"https://{host}/path\nuser@{host}\n")
    assert len(guard.findings(tmp_path, [path])) == 2


@pytest.mark.parametrize(
    "host", ["example.test", "a.invalid", "a.example", "example.net", "localhost", "claude.ai"]
)
def test_br_330_reserved_and_named_service(tmp_path: Path, host: str) -> None:
    path = "tests/fixture.txt"
    (tmp_path / "tests").mkdir()
    (tmp_path / path).write_text(f"https://{host}:443/path\nuser@{host}\n")
    assert guard.findings(tmp_path, [path]) == []
