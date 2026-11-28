"""Version guard of the `mcp` SDK (BR-036, BR-037; ADR-001 conditions 1 and 2).

"pin" step: `pyproject.toml` declares `mcp==X.Y.Z` and `uv.lock` resolves `mcp` to `X.Y.Z`.
"bench" step: the `mcp` and `mcp-types` versions of the root `uv.lock` are those of a conclusive
replay of the isolation bench's `v1` target (`poc/isolation-bench/evidence/results-v1.json`):
full replay, 16 tests and every control passed, the N3 harness and nominal `/revoke` included.
The guard reads verdicts and versions, nothing else of the evidence or of the repository.

Usage: `python scripts/sdk_guard.py [--root <repository>]`. Exit 0 if everything holds, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

SDK_PACKAGES = ("mcp", "mcp-types")
EVIDENCE = Path("poc/isolation-bench/evidence/results-v1.json")
TARGET = "v1"
ALL_TESTS = tuple(f"T{n:02d}" for n in range(1, 17))
# The two harnesses, identified by what follows the ticket prefix: the identifier changes with the
# prefix of the run that wrote the evidence, the harness does not (HULL-58).
HARNESSES = (
    re.compile(r"^H-10/[A-Z]+-5-review-2-n3$"),
    re.compile(r"^H-10/[A-Z]+-5-review-3-revoke-nominal$"),
)
PASSED = "réussi"
PIN = re.compile(r"^mcp\s*==\s*(\d+\.\d+\.\d+)$")
NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


class GuardError(Exception):
    def __init__(self, step: str, condition: str) -> None:
        super().__init__(f"{step}: {condition}")
        self.step = step


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def lock_versions(lock_path: Path) -> dict[str, str]:
    data = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    return {_norm(p["name"]): str(p["version"]) for p in data.get("package", []) if "version" in p}


def check_pin(root: Path) -> str:
    """Return the pinned version of `mcp`; raise `GuardError` otherwise."""
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    deps: list[str] = list(project.get("project", {}).get("dependencies", []))
    mcp_deps = [d for d in deps if (m := NAME.match(d)) and _norm(m.group(1)) == "mcp"]
    if len(mcp_deps) != 1:
        raise GuardError("pin", f"pyproject.toml must declare `mcp` once (found {mcp_deps})")
    match = PIN.match(mcp_deps[0].strip())
    if not match:
        raise GuardError(
            "pin", f"`{mcp_deps[0]}`: exact equality `mcp==X.Y.Z` expected, without range or extra"
        )
    pinned = match.group(1)
    locked = lock_versions(root / "uv.lock").get("mcp")
    if locked != pinned:
        raise GuardError("pin", f"uv.lock resolves mcp to {locked}, pyproject.toml pins {pinned}")
    return pinned


def _results(evidence: dict[str, Any], key: str) -> list[tuple[str, str]]:
    """(identifier, verdict) pairs of the evidence's `key` list; empty if it is absent.

    An entry that is not an object is refused, never skipped.
    """
    entries = evidence.get(key)
    if not isinstance(entries, list):
        return []
    pairs: list[tuple[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise GuardError("bench", f"entry of `{key}` that is not an object: {entry!r:.60}")
        pairs.append((str(entry.get("id")), str(entry.get("result"))))
    return pairs


def check_bench(root: Path) -> dict[str, str]:
    """Return the SDK versions attested by the bench; raise `GuardError` otherwise."""
    evidence_path = root / EVIDENCE
    if not evidence_path.is_file():
        raise GuardError("bench", f"evidence absent: {EVIDENCE}")
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise GuardError("bench", f"evidence unreadable (invalid JSON): {exc}") from exc
    if not isinstance(evidence, dict) or not isinstance(evidence.get("meta"), dict):
        raise GuardError("bench", "evidence malformed: `meta` absent")
    meta = evidence["meta"]
    if meta.get("target") != TARGET:
        raise GuardError("bench", f"target `{meta.get('target')}` instead of `{TARGET}`")
    if meta.get("dev_mode") is not False:
        raise GuardError(
            "bench",
            f"meta.dev_mode must be false, read {meta.get('dev_mode')!r} (inconclusive replay)",
        )
    if meta.get("partial_run") is not False:
        raise GuardError(
            "bench",
            f"meta.partial_run must be false, read {meta.get('partial_run')!r} (partial replay)",
        )
    locked = lock_versions(root / "uv.lock")
    modules = meta.get("module_versions")
    bench_versions = (
        {_norm(k): str(v) for k, v in modules.items()} if isinstance(modules, dict) else {}
    )
    for package in SDK_PACKAGES:
        attested = bench_versions.get(package)
        if attested is None:
            raise GuardError("bench", f"module_versions.{package} absent from the evidence")
        if locked.get(package) != attested:
            raise GuardError(
                "bench", f"{package}: v1 uv.lock {locked.get(package)}, bench {attested}"
            )
    tests = _results(evidence, "tests")
    present = {name for name, _ in tests}
    missing = [t for t in ALL_TESTS if t not in present]
    if missing:
        raise GuardError("bench", f"tests absent from the evidence: {', '.join(missing)}")
    # Every recorded test must have passed, including an identifier outside T01..T16 (BR-037:
    # "each test it records"); a passed duplicate does not make up for a failure.
    failed = list(dict.fromkeys(n for n, v in tests if v != PASSED))
    if failed:
        raise GuardError("bench", f"tests not passed: {', '.join(failed)}")
    controls = _results(evidence, "controls")
    if not controls:
        raise GuardError("bench", "controls absent or empty in the evidence")
    unmet = [name for name, verdict in controls if verdict != PASSED]
    if unmet:
        raise GuardError("bench", f"controls not passed: {', '.join(unmet)}")
    names = {name for name, _ in controls}
    absent = [h.pattern for h in HARNESSES if not any(h.match(name) for name in names)]
    if absent:
        raise GuardError("bench", f"harnesses absent from the controls: {', '.join(absent)}")
    return {p: bench_versions[p] for p in SDK_PACKAGES}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        pinned = check_pin(args.root)
        print(f"SDK-GUARD pin OK mcp=={pinned}")
        attested = check_bench(args.root)
        listed = ", ".join(f"{k} {v}" for k, v in attested.items())
        print(f"SDK-GUARD bench OK {listed} (results-v1.json)")
    except GuardError as exc:
        print(f"SDK-GUARD {exc.step} KO {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError) as exc:
        print(f"SDK-GUARD KO reading: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
