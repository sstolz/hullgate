"""BR-333: require current clean records; pending or changed images cannot reuse a verdict."""

from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path

import capture_review as guard
import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_br_333_capture_review() -> None:
    assert guard.check(ROOT) == []


def chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


@pytest.fixture
def reviewed(tmp_path: Path) -> tuple[Path, str]:
    path = "docs/captures/example.png"
    image = tmp_path / path
    image.parent.mkdir(parents=True)
    image.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\0\0\0\0"))
        + chunk(b"IEND", b"")
    )
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    (tmp_path / guard.RECORD).write_text(
        f"| {path} | {digest} | clean | 2026-11-16 | Fixture reader | blank | listed: none |\n"
    )
    return tmp_path, path


def test_br_333_clean_current_record(reviewed: tuple[Path, str]) -> None:
    root, path = reviewed
    assert guard.check(root, [path]) == []


@pytest.mark.parametrize(
    "change", ["bytes", "pending", "reader", "date", "metadata", "duplicate", "orphan", "missing"]
)
def test_br_333_rejects_incomplete_or_stale_record(reviewed: tuple[Path, str], change: str) -> None:
    root, path = reviewed
    record = root / guard.RECORD
    text = record.read_text()
    paths = [path]
    if change == "bytes":
        image = root / path
        image.write_bytes(image.read_bytes() + b"changed")
    elif change == "pending":
        record.write_text(text.replace("clean", "pending"))
    elif change == "reader":
        record.write_text(text.replace("Fixture reader", "pending"))
    elif change == "date":
        record.write_text(text.replace("2026-11-16", "2026-02-31"))
    elif change == "metadata":
        record.write_text(text.replace("listed: none", "pending"))
    elif change == "duplicate":
        record.write_text(text + text)
    elif change == "orphan":
        paths = []
    else:
        record.write_text("")
    assert guard.check(root, paths)


def test_br_333_kept_metadata_must_be_listed(reviewed: tuple[Path, str]) -> None:
    root, path = reviewed
    image = root / path
    raw = image.read_bytes()
    body = b"Comment\0fixture metadata"
    image.write_bytes(raw[:-12] + chunk(b"tEXt", body) + raw[-12:])
    record = root / guard.RECORD
    text = record.read_text().replace(
        hashlib.sha256(raw).hexdigest(), hashlib.sha256(image.read_bytes()).hexdigest()
    )
    record.write_text(text)
    assert guard.check(root, [path])
    record.write_text(
        text.replace("listed: none", "listed: tEXt:" + hashlib.sha256(body).hexdigest())
    )
    assert guard.check(root, [path]) == []
