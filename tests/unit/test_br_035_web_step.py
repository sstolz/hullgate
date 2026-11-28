"""BR-035 v2 (AC-33) — the gate's `web` step: console lockfile in sync, typecheck, lint, unit tests,
with a local Node of the pinned version or the pinned image through Docker, else `GATE: KO web`.

The behavioural test plays the real `make gate` recipe on a copy of the repository's Makefile,
`scripts/web.sh` and `web/`, with a stub `uv` so that the Python steps pass at once, and a console
unit test made to fail: the gate must end on `GATE: KO web`.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _node_available() -> bool:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    version = re.search(r"^NODE_VERSION := (\S+)$", makefile, re.MULTILINE)
    assert version is not None
    probe = subprocess.run(
        ["sh", "scripts/web.sh", version.group(1), _image(makefile), "version"],
        cwd=ROOT, capture_output=True, text=True, check=False, timeout=600,
    )  # fmt: skip
    return probe.returncode == 0


def _image(makefile: str) -> str:
    image = re.search(r"^NODE_IMAGE := (node@sha256:[0-9a-f]{64})$", makefile, re.MULTILINE)
    assert image is not None
    return image.group(1)


def test_br_035_web_step_names_the_pinned_node() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^NODE_VERSION := \d+\.\d+\.\d+$", makefile, re.MULTILINE)
    assert _image(makefile)
    assert "WEB := sh scripts/web.sh $(NODE_VERSION) $(NODE_IMAGE)" in makefile
    script = (ROOT / "scripts" / "web.sh").read_text(encoding="utf-8")
    assert "npm ci" in script  # the lockfile in sync, or the step fails
    for step in ("npm run --silent check", "npm run --silent lint", "npm run --silent test"):
        assert step in script
    package = (ROOT / "web" / "package.json").read_text(encoding="utf-8")
    for name in ('"check"', '"lint"', '"test"', '"build"'):
        assert name in package


def test_br_035_web_step_without_any_node_fails(tmp_path: Path) -> None:
    """No local Node of the version and no Docker: the step exits non-zero, saying why."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("docker", "node"):
        stub = bin_dir / tool
        stub.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    env = {**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin"}
    result = subprocess.run(
        ["sh", "scripts/web.sh", "26.13.0", "node@sha256:" + "0" * 64, "gate"],
        cwd=ROOT, env=env, capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert result.returncode != 0
    assert "no node v26.13.0" in result.stderr


@pytest.mark.skipif(shutil.which("make") is None, reason="make absent from this session")
def test_br_035_failing_console_test_gives_gate_ko_web() -> None:
    if not _node_available():
        pytest.skip("neither the pinned Node nor Docker with its image in this session")
    # Under the repository root, a path the session's Docker daemon also sees (bind mount of web/).
    tmp_path = ROOT / f".web-step-{os.getpid()}"
    shutil.rmtree(tmp_path, ignore_errors=True)
    tmp_path.mkdir()
    try:
        _play(tmp_path)
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def _play(tmp_path: Path) -> None:
    shutil.copy(ROOT / "Makefile", tmp_path / "Makefile")
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "web.sh", tmp_path / "scripts" / "web.sh")
    (tmp_path / "scripts" / "qa_tree.py").write_text("", encoding="utf-8")
    shutil.copytree(
        ROOT / "web", tmp_path / "web", ignore=shutil.ignore_patterns("node_modules", "dist")
    )
    (tmp_path / "web" / "src" / "lib" / "broken.test.ts").write_text(
        "import { expect, it } from 'vitest';\n"
        "it('fails on purpose', () => { expect(1).toBe(2); });\n",
        encoding="utf-8",
    )
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\necho 'uv stub'\nexit 0\n", encoding="utf-8")
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
    result = subprocess.run(
        ["make", "-s", "gate", f"UV={uv}"],
        cwd=tmp_path, capture_output=True, text=True, check=False, timeout=900,
    )  # fmt: skip
    lines = result.stdout.strip().splitlines()
    assert result.returncode != 0
    assert lines[-1] == "GATE: KO web", result.stdout[-2000:] + result.stderr[-2000:]
    assert any(line.startswith("== GATE web : node v") for line in lines)
    assert "fails on purpose" in result.stdout + result.stderr  # failed for the right reason
