"""BR-333: bind each tracked image to a complete, current review record."""

from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path

from operator_names import tracked

RECORD = "docs/captures/REVIEW.md"
IMAGE_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".avif",
    ".bmp",
    ".tif",
    ".tiff",
}
PNG_TEXT = {b"tEXt", b"iTXt", b"zTXt"}


def images(paths: list[str]) -> list[str]:
    return sorted(
        p for p in paths if not p.startswith(".cairn/") and Path(p).suffix.lower() in IMAGE_SUFFIXES
    )


def png_text_chunks(raw: bytes) -> list[str]:
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("invalid PNG signature")
    offset, chunks = 8, []
    while offset < len(raw):
        size = int.from_bytes(raw[offset : offset + 4], "big")
        kind = raw[offset + 4 : offset + 8]
        end = offset + 12 + size
        if end > len(raw) or len(kind) != 4:
            raise ValueError("truncated PNG chunk")
        if kind in PNG_TEXT:
            # Fingerprints identify kept text without copying possibly private metadata.
            digest = hashlib.sha256(raw[offset + 8 : offset + 8 + size]).hexdigest()
            chunks.append(f"{kind.decode()}:{digest}")
        offset = end
        if kind == b"IEND":
            if offset != len(raw):
                raise ValueError("trailing PNG data")
            return chunks
    raise ValueError("missing PNG end")


def check(root: Path, paths: list[str] | None = None) -> list[str]:
    population = images(tracked(root) if paths is None else paths)
    rows: dict[str, list[str]] = {}
    errors = []
    if not (root / RECORD).is_file():
        return [f"BR-333: missing {RECORD}"]
    for line in (root / RECORD).read_text().splitlines():
        if not line.startswith("| ") or line.startswith("| Path |"):
            continue
        fields = [f.strip().strip("`") for f in line.strip("|").split("|")]
        if len(fields) != 7:
            errors.append("BR-333: malformed review row")
            continue
        path = fields[0]
        if path in rows:
            errors.append(f"BR-333: duplicate record: {path}")
        rows[path] = fields
    for path in sorted(set(rows) - set(population)):
        errors.append(f"BR-333: record without tracked image: {path}")
    for path in population:
        row = rows.get(path)
        if row is None:
            errors.append(f"BR-333: missing record: {path}")
            continue
        _, sha, verdict, day, reader, notes, metadata = row
        raw = (root / path).read_bytes()
        try:
            date.fromisoformat(day)
            valid_date = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", day))
        except ValueError:
            valid_date = False
        complete = all(v and v.lower() not in {"pending", "—", "unknown"} for v in (reader, notes))
        valid_metadata = metadata == "stripped" or metadata.startswith("listed: ")
        if Path(path).suffix.lower() == ".png":
            try:
                chunks = png_text_chunks(raw)
                expected = "listed: " + (", ".join(chunks) if chunks else "none")
                valid_metadata = metadata == expected or (metadata == "stripped" and not chunks)
            except ValueError:
                valid_metadata = False
        if not (
            sha == hashlib.sha256(raw).hexdigest()
            and verdict == "clean"
            and valid_date
            and complete
            and valid_metadata
        ):
            errors.append(f"BR-333: no complete current clean record: {path}")
    return errors
