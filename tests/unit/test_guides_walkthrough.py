"""Validate the real guide blocks without executing the Docker walkthrough."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def guide_block(guide: str, step: str) -> str:
    text = (ROOT / "docs/guides" / f"{guide}.md").read_text()
    body = re.split(rf"^## Step {step} — [^\n]*\n", text, flags=re.M)[1]
    body = re.split(r"^## Step ", body, flags=re.M)[0]
    match = re.search(r"^```sh\n(.*?)\n```", body, re.M | re.S)
    assert match is not None
    return match[1]


def test_walkthrough_extraction_and_selected_blocks_parse(tmp_path: Path) -> None:
    """Execute only the pure block renderer, then syntax-check its selected shell blocks."""
    script = (ROOT / "scripts/guides_walkthrough.sh").read_text()
    renderer = script.split("python3 - \"$WORK/blocks\" <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    env = dict(os.environ, ROOT=str(ROOT), CLONE=str(tmp_path / "target"), REF="a" * 40)
    subprocess.run(
        [sys.executable, "-", str(tmp_path)], input=renderer, text=True, env=env, check=True
    )
    selected = set(re.findall(r"\bblock ([\w-]+) ([A-Z]\d+) (\d+)", script))
    assert len(selected) >= 30
    for guide, step, index in selected:
        block = tmp_path / f"{guide}-{step}-{index}.sh"
        assert block.is_file(), block
        text = block.read_text()
        assert (
            re.search(r"<[A-Za-z][\w-]*>", text.replace("routers.<x>.", "routers.slot.")) is None
        ), block
        subprocess.run(["bash", "-n", str(block)], check=True)
    subprocess.run(["bash", "-n", str(ROOT / "scripts/guides_walkthrough.sh")], check=True)
    assert "block keys-and-upstreams S3 0" in script
    assert "block keys-and-upstreams C2 0" in script
    assert "${PREVIOUS_REF:?Run update M1 first}" in (tmp_path / "update-M4-0.sh").read_text()


@pytest.mark.parametrize(("step", "key"), [("S3", "signing_key"), ("C2", "connections_key")])
def test_key_creation_refuses_existing_file_before_chown(
    tmp_path: Path, step: str, key: str
) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "authorize_totp").write_text("fixture")
    (secrets / key).write_text("keep fixture")
    script = guide_block("keys-and-upstreams", step).replace("/opt/hullgate", str(tmp_path))
    # A sudo call would leave an observable marker; no real privileged command can run.
    script = "sudo() { touch chown-was-called; };\n" + script
    result = subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True)
    assert result.returncode != 0
    assert not (tmp_path / "chown-was-called").exists()
    assert (secrets / key).read_text() == "keep fixture"


@pytest.mark.parametrize(
    ("step", "key"), [("S4", "SIGNING_KEY_PATH"), ("C3", "CONNECTIONS_KEY_PATH")]
)
def test_path_declaration_rejects_duplicates_before_compose(
    tmp_path: Path, step: str, key: str
) -> None:
    (tmp_path / ".env").write_text(
        "SIGNING_KEY_PATH=/fixture/signing\nAUTHORIZE_TOTP_PATH=/fixture/totp\n"
        "CONNECTIONS_KEY_PATH=/fixture/connections\n" + f"{key}=/duplicate\n"
    )
    script = guide_block("keys-and-upstreams", step).replace("/opt/hullgate", str(tmp_path))
    script = "EDITOR=true; docker() { touch compose-was-called; };\n" + script
    result = subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True)
    assert result.returncode != 0
    assert not (tmp_path / "compose-was-called").exists()


def test_rollback_uses_saved_sha_not_current_deployed_ref(tmp_path: Path) -> None:
    (tmp_path / ".deployed-ref").write_text("new-but-unverified\nold-healthy\n")
    script = guide_block("update", "M4").replace("/opt/hullgate", str(tmp_path))
    script = script.replace("<previous-ref>", "old-healthy")
    script = "make() { printf '%s\\n' \"$*\" >> commands; };\n" + script
    subprocess.run(["bash", "-c", script], cwd=tmp_path, check=True)
    assert (tmp_path / "commands").read_text().splitlines() == ["deploy REF=old-healthy", "verify"]
