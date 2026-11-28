"""BR-029 — reproducible image: bases pinned by digest, dependencies from the lock only."""

from __future__ import annotations

import re

from .conftest import IMAGE, ROOT, docker


def test_br_029_every_from_is_pinned_by_digest() -> None:
    lines = [
        line
        for line in (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()
        if line.startswith("FROM ")
    ]
    assert lines
    for line in lines:
        assert re.search(r"@sha256:[0-9a-f]{64}\b", line), line


def test_br_029_build_installs_from_lock_only() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "uv sync --locked --no-dev" in text
    assert "pip install" not in text


def test_br_029_uv_absent_from_runtime_image() -> None:
    result = docker("run", "--rm", "--entrypoint", "sh", IMAGE, "-c", "command -v uv", check=False)
    assert result.returncode != 0, result.stdout


def test_br_029_runtime_has_locked_sdk_version() -> None:
    code = "import importlib.metadata as m; print(m.version('mcp'), m.version('mcp-types'))"
    out = docker("run", "--rm", "--entrypoint", "python", IMAGE, "-c", code).stdout
    assert out.split() == ["2.3.0", "2.3.0"]


# -- BR-029 v2 (F-016 lot A): the console build stage --------------------------------------------


def test_br_029_v2_console_stage_installs_from_its_lockfile_only() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    stage = text[text.index(" AS console") : text.index(" AS build")]
    assert "npm ci" in stage
    assert "npm install" not in text
    assert (ROOT / "web" / "package-lock.json").is_file()


def test_br_029_v2_node_digest_is_the_makefile_one() -> None:
    """The gate's `web` step and the image build the console with the same pinned Node."""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    pinned = re.search(r"^NODE_IMAGE := (node@sha256:[0-9a-f]{64})$", makefile, re.MULTILINE)
    assert pinned is not None
    assert f"FROM --platform=$BUILDPLATFORM {pinned.group(1)} AS console" in dockerfile
