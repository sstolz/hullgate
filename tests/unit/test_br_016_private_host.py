"""BR-016: scan both halves with private input, public half even without it."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    path = ROOT / "poc/isolation-bench/runner/secrets_guard.py"
    spec = importlib.util.spec_from_file_location("bench_secrets_guard", path)
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    bench = tmp_path / "poc/isolation-bench"
    monkeypatch.setattr(module, "BENCH", str(bench))
    monkeypatch.setattr(module, "DIGESTS", str(bench / ".digests/population.sha256"))
    monkeypatch.setattr(module, "SEEN", str(bench / ".run/seen_tokens"))
    monkeypatch.setattr(module, "RUN_ENV", str(bench / ".run/env"))
    monkeypatch.setattr(module, "PRIVATE_HOSTS", str(tmp_path / "hosts.txt"))
    return module


@pytest.mark.parametrize("present", [False, True])
def test_br_016_private_input_and_public_half(
    guard: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str], present: bool
) -> None:
    if present:
        Path(guard.PRIVATE_HOSTS).write_text("forge.example.test\n")
    fixture = tmp_path / "poc/fixture.txt"
    fixture.parent.mkdir()
    fixture.write_text("https://forge.example.test/repo\n")
    bad, _ = guard.scan([str(fixture)])
    assert bool(bad) is present
    fixture.write_text("https://gitea.com/repo\n")
    bad, _ = guard.scan([str(fixture)])
    assert bad
    fixture.write_text("https://GITEA.COM/repo\n")
    assert guard.scan([str(fixture)])[0]
    fixture.write_text("https://docker.gitea.com/image\n")
    assert guard.scan([str(fixture)])[0] == []
    output = capsys.readouterr().out
    assert ("BR-016 SKIP" in output) is not present
    if not present:
        assert ".cairn/runs/private/bench-forbidden-hosts.txt" in output


def test_br_016_invalid_input_fails_closed(guard: ModuleType) -> None:
    Path(guard.PRIVATE_HOSTS).write_text("\n")
    with pytest.raises(ValueError, match="invalid private forge-host input"):
        guard.forbidden_hosts()
