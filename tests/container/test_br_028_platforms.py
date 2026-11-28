"""BR-028 — a single image for amd64 and arm64 (images of `make image-multiarch`)."""

from __future__ import annotations

import io
import subprocess
import tarfile

import pytest

from .conftest import ROOT, docker, inspect

ELF_MACHINE = {"amd64": 62, "arm64": 183}  # EM_X86_64, EM_AARCH64
SITE = "/app/.venv/lib/python3.13/site-packages"


@pytest.mark.parametrize("arch", ["amd64", "arm64"])
def test_br_028_image_built_for_architecture(arch: str) -> None:
    image = inspect("image", f"hullgate:multiarch-{arch}")
    assert (image["Os"], image["Architecture"]) == ("linux", arch)


@pytest.mark.parametrize("arch", ["amd64", "arm64"])
def test_br_028_native_wheels_match_architecture(arch: str) -> None:
    """The native extensions of the environment are those of the target architecture."""
    cid = docker("create", f"hullgate:multiarch-{arch}").stdout.strip()
    try:
        data = subprocess.run(
            ["docker", "cp", f"{cid}:{SITE}", "-"], capture_output=True, check=True
        ).stdout
    finally:
        docker("rm", cid)
    machines = set()
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for member in tar.getmembers():
            if member.isfile() and member.name.endswith(".so"):
                handle = tar.extractfile(member)
                assert handle is not None
                head = handle.read(20)
                if head[:4] == b"\x7fELF":
                    machines.add(int.from_bytes(head[18:20], "little"))
    assert machines == {ELF_MACHINE[arch]}


def test_br_028_single_dockerfile_without_architecture_files() -> None:
    dockerfiles = sorted(p.name for p in ROOT.glob("Dockerfile*"))
    assert dockerfiles == ["Dockerfile"]
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "--platform=$BUILDPLATFORM" in text
    assert "$TARGETARCH" in text
