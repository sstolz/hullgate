"""Blocking tracked-file scan and exact, non-stale fingerprint allowlist (BR-326)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from base64 import b64encode
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

import secret_scanner as scanner

import qa

ALLOWLIST = "qa/secrets-allowlist.json"
PIN = "qa/secrets-scanner.json"
WORKSHOP = (".cairn/runs/", ".cairn/learnings/", ".cairn/workshop.git/")
FORBIDDEN = (".cairn/auth/", ".cairn/mcp/")


def git(root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        check=False,
        env=os.environ | {"GIT_NO_REPLACE_OBJECTS": "1"},
    )
    if result.returncode:
        # Git errors can contain file content, remote URLs or credentials.
        raise ValueError("Git operation failed (output withheld)")
    return result.stdout


def safe_path(path: str) -> bool:
    return bool(path) and not PurePosixPath(path).is_absolute() and ".." not in path.split("/")


def regular_bytes(target: Path) -> bytes:
    if any(p.is_symlink() for p in [target, *target.parents]):
        raise ValueError("regular input changed to a symlink")
    return target.read_bytes()


def validate_pin(root: Path) -> str:
    pin = json.loads(regular_bytes(root / PIN))
    digest = hashlib.sha256(regular_bytes(root / "scripts/secret_scanner.py")).hexdigest()
    if (
        pin["version"] != scanner.VERSION
        or pin["sha256"] != digest
        or re.fullmatch(r"python@sha256:[0-9a-f]{64}", pin["image"]) is None
    ):
        raise ValueError("scanner version/source/image pin mismatch")
    return str(pin["image"])


def allowlist(root: Path) -> dict[tuple[str, str, str], str]:
    rows = json.loads(regular_bytes(root / ALLOWLIST))
    if not isinstance(rows, list):
        raise ValueError("allowlist must be a list")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "detector", "fingerprint", "reason"}:
            raise ValueError("invalid allowlist schema")
        # Read by key, never depend on JSON key order.
        path, detector = row["path"], row["detector"]
        fingerprint, reason = row["fingerprint"], row["reason"]
        if (
            not all(isinstance(v, str) for v in row.values())
            or not safe_path(path)
            or detector not in scanner.DETECTORS
            or re.fullmatch(r"[0-9a-f]{24}", fingerprint) is None
            or not reason.strip()
        ):
            raise ValueError("invalid allowlist entry (value withheld)")
        key = (path, detector, fingerprint)
        if key in result:
            raise ValueError("duplicate allowlist entry")
        result[key] = reason
    return result


def tracked_bytes(root: Path) -> Iterator[tuple[str, bytes]]:
    for raw in git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if not raw:
            continue
        metadata, name = raw.split(b"\t", 1)
        mode, _, stage = metadata.split()
        path = os.fsdecode(name)
        if not safe_path(path) or stage != b"0":
            raise ValueError("unsafe path or unresolved index")
        if path in {".mcp.json"} or path.startswith(FORBIDDEN):
            raise ValueError("credential path tracked; refused without reading it")
        if path.startswith(WORKSHOP) or path == ".cairn/project-context.yaml":
            continue
        if mode not in {b"100644", b"100755", b"120000"}:
            raise ValueError("unsupported tracked entry (submodules are not scanned)")
        target = root / path
        # Do not follow a replaced file or parent directory into credentials.
        data = os.fsencode(os.readlink(target)) if mode == b"120000" else regular_bytes(target)
        yield path, data


def tracked_findings(root: Path, runtime: str = "native") -> tuple[int, list[scanner.Finding]]:
    files = list(tracked_bytes(root))
    if runtime == "native":
        return len(files), [f for path, data in files for f in scanner.scan(path, data)]
    if runtime != "image":
        raise ValueError("unknown scanner runtime")
    image = validate_pin(root)
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user=65534:65534",
            "--mount",
            f"type=bind,src={root / 'scripts/secret_scanner.py'},dst=/scanner.py,readonly",
            "--entrypoint=python",
            image,
            "-I",
            "/scanner.py",
        ],
        input=json.dumps([(path, b64encode(data).decode()) for path, data in files]),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise ValueError("offline scanner image unavailable or failed (output withheld)")
    rows = json.loads(result.stdout)
    if not isinstance(rows, list):
        raise ValueError("invalid scanner image response")
    findings = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"path", "line", "detector", "fingerprint"}
            or row["path"] not in dict(files)
            or type(row["line"]) is not int
            or row["line"] < 1
            or row["detector"] not in scanner.DETECTORS
            or not isinstance(row["fingerprint"], str)
            or re.fullmatch(r"[0-9a-f]{24}", row["fingerprint"]) is None
        ):
            raise ValueError("invalid scanner image finding (output withheld)")
        findings.append(scanner.Finding(**row))
    return len(files), findings


def evaluate(root: Path, runtime: str = "native") -> tuple[str, str, list[str]]:
    validate_pin(root)
    accepted = allowlist(root)
    count, findings = tracked_findings(root, runtime)
    found = {finding.key for finding in findings}
    diagnostics = [
        f"{json.dumps(f.path)}:{f.line} {f.detector} {f.fingerprint} {scanner.MASK}"
        for f in findings
        if f.key not in accepted
    ]
    diagnostics.extend(
        f"stale allowlist: {json.dumps(path)} {detector} {fingerprint}"
        for path, detector, fingerprint in sorted(accepted.keys() - found)
    )
    verdict = "FINDINGS" if diagnostics else "OK"
    return verdict, f"files={count} candidates={len(findings)} allowed={len(accepted)}", diagnostics


def main(argv: list[str]) -> int:
    role = argv[1] if len(argv) > 1 else "blocking"
    root = Path.cwd()
    print(f"== QA secrets : hullgate-secret-scanner {scanner.VERSION}")
    try:
        verdict, detail, diagnostics = evaluate(root, argv[2] if len(argv) > 2 else "native")
    except (OSError, ValueError, KeyError, TypeError):
        return qa.report("secrets", role, "ERROR", "scan/configuration failed (details withheld)")
    for line in diagnostics:
        print(line)
    return qa.report("secrets", role, verdict, detail)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
