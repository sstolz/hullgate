"""BR-327: local mirror scope, masked report and verdict completeness."""

from __future__ import annotations

import hashlib
import os
import platform
from pathlib import Path

import export_rehearsal as exporter
import history_secrets as history
import pytest
import qa_secrets
import secret_scanner

from tests.integration.test_br_326_secrets import ROOT, accept, token


def snapshot(root: Path, parent: str | None = None) -> str:
    exporter.local_git(root, "add", ".")
    tree = exporter.local_git(root, "write-tree").decode().strip()
    parents = ["-p", parent] if parent else []
    return (
        exporter.local_git(root, "commit-tree", tree, *parents, data=b"fixture\n").decode().strip()
    )


@pytest.fixture
def mirror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key in list(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    source = tmp_path / "source"
    source.mkdir()
    exporter.local_git(source, "init", "-q")
    (source / "fixture.txt").write_text(token())
    first = snapshot(source)
    exporter.local_git(source, "update-ref", "refs/heads/main", first)
    (source / "fixture.txt").write_text("clean")
    clean = snapshot(source, first)
    exporter.local_git(source, "update-ref", "refs/heads/main", clean)
    # This root commit is reachable ONLY through a pull-request ref, not the branch.
    (source / "fixture.txt").write_text(token("B"))
    pull = snapshot(source)
    exporter.local_git(source, "update-ref", "refs/pull/17/head", pull)
    exporter.local_git(source, "update-ref", "refs/notes/fixture", first)
    mirror = tmp_path / "mirror.git"
    exporter.local_git(tmp_path, "clone", "--mirror", str(source), str(mirror))
    return mirror


def test_br_327_all_refs_and_removed_candidates(mirror: Path) -> None:
    refs, commits, rows = history.scan_mirror(mirror)
    assert refs == 3
    assert commits == 3
    fingerprints = {f.fingerprint for _, f in rows}
    assert fingerprints == {hashlib.sha256(token(c).encode()).hexdigest()[:24] for c in "AB"}
    assert all(len(commit) == 40 for commit, _ in rows)


def test_br_327_report_inherits_reason_and_requires_remaining_verdict(
    mirror: Path,
    tmp_path: Path,
) -> None:
    import shutil

    root = tmp_path / "config"
    (root / "qa").mkdir(parents=True)
    (root / "scripts").mkdir()
    for name in ("qa/secrets-scanner.json", "scripts/secret_scanner.py"):
        shutil.copyfile(ROOT / name, root / name)
    accept(root, token())
    report, verdicts = history.reports(root, mirror)
    assert "Refs: 3; commits: 3; candidates: 2; distinct fingerprints: 2" in report
    assert token() not in report + verdicts
    assert token("B") not in report + verdicts
    assert "not a credential" in verdicts
    assert not history.validate_verdicts(report, verdicts)
    complete = verdicts.replace("PENDING · YYYY-MM-DD", "not live · 2026-11-16")
    assert history.validate_verdicts(report, complete)
    assert not history.validate_verdicts(report, complete + complete)
    assert not history.validate_verdicts(report, complete.replace("2026-11-16", "2026-99-99"))


def test_br_327_refuses_missing_pr_refs(mirror: Path) -> None:
    exporter.local_git(mirror, "update-ref", "-d", "refs/pull/17/head")
    with pytest.raises(ValueError, match="pull-request refs"):
        history.scan_mirror(mirror)


def test_br_327_invalid_report_is_not_empty_success() -> None:
    assert not history.validate_verdicts("", "")


def test_br_327_report_identifies_native_runtime_without_scanning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_scan(mirror: Path) -> tuple[int, int, list[tuple[str, secret_scanner.Finding]]]:
        return 1, 1, []

    monkeypatch.setattr(history, "scan_mirror", no_scan)
    report, _ = history.reports(ROOT, Path("unused"))
    runtime = f"native {platform.python_implementation()} {platform.python_version()}"
    assert f"runtime: {runtime}\n" in report
    assert qa_secrets.validate_pin(ROOT) not in report
