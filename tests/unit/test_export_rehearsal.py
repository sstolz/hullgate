"""F-022: archive boundary, public context equality and scratch cleanup on failure."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import export_rehearsal as exporter
import pytest
import yaml

from tests.unit.test_br_327_history import snapshot

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key in list(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    source = tmp_path / "source"
    source.mkdir()
    exporter.local_git(source, "init", "-q")
    for path in exporter.EXPORTED_CAIRN:
        (source / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / path, source / path)
    (source / ".gitignore").write_text("")
    for path in (
        ".cairn/runs/private/fixture",
        ".cairn/learnings/fixture",
        ".cairn/auth/fixture",
        ".mcp.json",
    ):
        (source / path).parent.mkdir(parents=True, exist_ok=True)
        (source / path).write_text("synthetic private fixture")
    (source / exporter.CONTEXT).write_text(
        yaml.safe_dump(
            {
                "project": {"visibility": "private"},
                "cairn": {"version": "0.57.0"},
                "language": "en",
                "infra": {"hosting": "https://internal.example.test"},
            }
        )
    )
    commit = snapshot(source)
    exporter.local_git(source, "update-ref", "HEAD", commit)
    return source


def test_export_archive_has_one_clean_commit_and_only_two_cairn_files(source: Path) -> None:
    original = (source / exporter.CONTEXT).read_bytes()
    output = source.parent / "export"
    exporter.export(source, output, "HEAD")
    exporter.assert_boundary(output)
    assert exporter.local_git(output, "rev-list", "--count", "HEAD").strip() == b"1"
    assert not (output / ".mcp.json").exists()
    assert not (output / ".cairn/runs").exists()
    assert not (output / ".cairn/auth").exists()
    assert (source / exporter.CONTEXT).read_bytes() == original
    scratch = exporter.scratch_context(source, output)
    assert scratch["project"]["visibility"] == "public"
    assert scratch["cairn"]["version"] == "0.60.0"
    assert scratch["conversation-language"] == "en"
    assert scratch["infra"]["hosting"] == "https://internal.example.test"
    with pytest.raises(ValueError, match="must not exist"):
        exporter.export(source, output, "HEAD")


def test_export_with_tag_holds_the_tag_tree_beside_head(source: Path) -> None:
    # F-025: the rehearsal of a commit after the switch carries tag v1.0.0, as the public
    # repository does, so that BR-350's guard can read the tag's tree.
    exporter.local_git(source, "tag", "v1.0.0")
    (source / "later.txt").write_text("after the tag")
    exporter.local_git(source, "update-ref", "HEAD", snapshot(source))
    output = source.parent / "export"
    exporter.export(source, output, "HEAD")
    tree = exporter.add_tag(source, output, "v1.0.0")
    assert exporter.local_git(output, "rev-parse", "v1.0.0^{tree}").decode().strip() == tree
    assert exporter.local_git(output, "rev-list", "--count", "HEAD").strip() == b"1"
    tagged = exporter.local_git(output, "ls-tree", "-r", "--name-only", "v1.0.0").decode()
    assert "later.txt" not in tagged.split()
    assert ".cairn/runs/private/fixture" not in tagged.split()
    exporter.assert_boundary(output)


def test_export_scratch_deleted_when_local_check_raises(
    source: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = source.parent / "export"
    exporter.export(source, output, "HEAD")
    calls = []

    def fake_check(root: Path, binary: str, required: set[str]) -> bool:
        calls.append(required)
        if "INV-069" in required:
            assert (root / exporter.CONTEXT).exists()
            raise ValueError("simulated local check failure")
        return True

    class Result:
        returncode = 0

    def fake_run(*args: Any, **kwargs: Any) -> Result:
        return Result()

    monkeypatch.setattr(exporter, "check_cairn", fake_check)
    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ValueError, match="simulated"):
        exporter.checks(source, output, "non-docker", "cairn")
    assert len(calls) == 2
    assert not (output / exporter.CONTEXT).exists()


def test_example_has_contract_and_no_local_keys() -> None:
    example = yaml.safe_load((ROOT / exporter.EXAMPLE).read_text())
    assert not exporter.LOCAL_TOP.intersection(example)
    assert set(example["cairn"]) == {"version"}
    assert all(set(driver) == {"type"} for driver in example["drivers"].values())
    assert example["tracker"]["project-id"] == "sstolz/hullgate"
    assert example["project"]["repository"] == "https://github.com/sstolz/hullgate"
    assert set(example["access"]) == {"forge-reserves-merge"}
    assert {
        "br-lifecycle",
        "feature-identifiers",
        "design-assets",
        "capability-map",
        "adr-dir",
        "design-dir",
        "learnings-budget",
    } <= example["references"].keys()
