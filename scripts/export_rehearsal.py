"""F-022 export boundary and rehearsal; local only, no clone/fetch/push.

Default: archive HEAD into a new directory, make its single local snapshot, run the
full gate and QA, then judge the example and an ephemeral local context. Use
--checks non-docker for the offline Python checks, or --checks prepare to only export.
A tree object may be supplied for pre-commit validation; it is labelled as such.
"""

from __future__ import annotations

import argparse
import copy
import io
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

import qa_secrets
import yaml

EXAMPLE = ".cairn/project-context.example.yaml"
CONTEXT = ".cairn/project-context.yaml"
EXPORTED_CAIRN = {EXAMPLE, ".cairn/docker/Dockerfile.project"}
IGNORE = [CONTEXT, ".cairn/runs/", ".cairn/learnings/", ".cairn/workshop.git/"]
LOCAL_TOP = {
    "conversation-language",
    "autonomy",
    "technical-autonomy",
    "runtime",
    "docker",
    "infra",
    "workshop",
}


class ContextDumper(yaml.SafeDumper):
    """Cairn's YAML reader requires indented block sequences."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        return super().increase_indent(flow, False)


def context_yaml(context: dict[str, Any]) -> str:
    return yaml.dump(
        context, Dumper=ContextDumper, sort_keys=False, allow_unicode=True, width=1000000
    )


def isolated_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def local_git(root: Path, *args: str, data: bytes | None = None) -> bytes:
    env = isolated_env() | {
        "GIT_AUTHOR_NAME": "Export rehearsal",
        "GIT_AUTHOR_EMAIL": "export@example.test",
        "GIT_COMMITTER_NAME": "Export rehearsal",
        "GIT_COMMITTER_EMAIL": "export@example.test",
    }
    result = subprocess.run(
        ["git", *args], cwd=root, env=env, input=data, capture_output=True, check=False
    )
    if result.returncode:
        raise ValueError("export Git operation failed (output withheld)")
    return result.stdout


def export(source: Path, destination: Path, ref: str) -> str:
    if destination.exists():
        raise ValueError("export destination must not exist")
    oid = qa_secrets.git(source, "rev-parse", "--verify", f"{ref}^{{tree}}").decode().strip()
    paths = [
        os.fsdecode(path)
        for path in qa_secrets.git(source, "ls-tree", "-r", "--name-only", "-z", oid).split(b"\0")
        if path
    ]
    exported = [
        path
        for path in paths
        if path != ".mcp.json" and (not path.startswith(".cairn/") or path in EXPORTED_CAIRN)
    ]
    if not exported or any(not qa_secrets.safe_path(path) for path in exported):
        raise ValueError("empty or unsafe export population")
    # Select before archiving: private/credential blobs never enter even the tar buffer.
    archive = qa_secrets.git(source, "archive", "--format=tar", oid, "--", *exported)
    destination.mkdir(parents=True, mode=0o700)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        members = []
        for member in tar.getmembers():
            path = member.name.rstrip("/")
            if not qa_secrets.safe_path(path):
                raise ValueError("unsafe archive path")
            # The only exported framework artifacts are this explicit pair. Never
            # extract the workshop, context, credentials, runtime files or MCP config.
            if path == ".mcp.json" or (path.startswith(".cairn/") and path not in EXPORTED_CAIRN):
                continue
            if member.issym() or member.islnk():
                # Refuse links rather than letting a future test read outside the copy.
                raise ValueError("export contains a link; explicit boundary review needed")
            members.append(member)
        tar.extractall(destination, members=members, filter="data")
    if not all((destination / p).is_file() for p in EXPORTED_CAIRN):
        raise ValueError("export lacks a required context/tooling artifact")
    with (destination / ".gitignore").open("a") as handle:
        handle.write("\n# Public export only (F-022 D1(c)); private source stays tracked.\n")
        handle.writelines(f"/{p}\n" for p in IGNORE)
    local_git(destination, "init", "-q", "-b", "main")
    local_git(destination, "add", ".")
    tree = local_git(destination, "write-tree").decode().strip()
    commit = local_git(
        destination, "commit-tree", tree, data=f"Public export of tree {oid}\n".encode()
    )
    local_git(destination, "update-ref", "HEAD", commit.decode().strip())
    assert_boundary(destination)
    return oid


def assert_boundary(root: Path) -> None:
    paths = set(local_git(root, "ls-files", ".cairn").decode().splitlines())
    if paths != EXPORTED_CAIRN:
        raise ValueError("export framework boundary differs from the two allowed files")
    ignored = local_git(root, "check-ignore", "--stdin", data="\n".join(IGNORE).encode())
    if set(ignored.decode().splitlines()) != set(IGNORE):
        raise ValueError("public ignore entries missing")


def scratch_context(source: Path, root: Path) -> dict[str, Any]:
    original = yaml.safe_load((source / CONTEXT).read_text())
    example: dict[str, Any] = yaml.safe_load((root / EXAMPLE).read_text())
    result = copy.deepcopy(example)
    for key in sorted(LOCAL_TOP):
        if key in original:
            result[key] = original[key]
    for key, value in original.get("cairn", {}).items():
        if key != "version":
            result["cairn"][key] = value
    for name, driver in original.get("drivers", {}).items():
        result["drivers"][name].update({k: v for k, v in driver.items() if k != "type"})
    for key in ("merge-by", "production"):
        if key in original.get("access", {}):
            result.setdefault("access", {})[key] = original["access"][key]
    # Migration 0.58.0 gesture 2: local only, absent from the public contract.
    result.setdefault("conversation-language", original.get("language", "en"))
    return result


def check_cairn(root: Path, binary: str, required: set[str]) -> bool:
    result = subprocess.run(
        [binary, "check", "--project", str(root)],
        env=isolated_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    # Cairn diagnostics may quote local infrastructure on failure. Print only status
    # headers/counts; do not echo detail values from either stream.
    lines = result.stdout.splitlines()
    for line in lines:
        match = re.search(r"\b(INV-\d{3})\s+(pass|fail|warn|skip|info)\b", line)
        if match:
            print(f"{match[1]} {match[2]}", flush=True)
    passed = {m[1] for line in lines if (m := re.search(r"\b(INV-\d{3})\s+pass\b", line))}
    if not required <= passed:
        raise ValueError("required public invariants did not pass (details withheld)")
    print(f"cairn exit={result.returncode}; required public invariants pass", flush=True)
    return result.returncode == 0


def checks(source: Path, root: Path, mode: str, cairn: str) -> bool:
    env = isolated_env() | {"UV_OFFLINE": "1", "PIP_NO_INDEX": "1"}
    commands = (
        [["make", "gate"], ["make", "qa"]]
        if mode == "full"
        else [
            ["ruff", "format", "--check", "."],
            ["ruff", "check", "."],
            ["mypy", "--strict"],
            [sys.executable, "scripts/operator_names.py"],
            [sys.executable, "-m", "pytest", "tests/unit", "tests/integration", "-q"],
            [sys.executable, "scripts/sdk_guard.py"],
            ["make", "qa-secrets"],
        ]
    )
    ok = True
    for command in commands:
        print("REHEARSAL " + " ".join(command), flush=True)
        result = subprocess.run(command, cwd=root, env=env, check=False)
        ok = result.returncode == 0 and ok
    print("cairn: judging the exported example (no local context)", flush=True)
    ok = check_cairn(root, cairn, {"INV-068"}) and ok
    target = root / CONTEXT
    created = False
    try:
        # exclusive creation; local values exist only inside this ignored scratch file
        with target.open("x") as handle:
            created = True
            target.chmod(0o600)
            handle.write(context_yaml(scratch_context(source, root)))
        print("cairn: judging scratch local context (never committed)", flush=True)
        ok = check_cairn(root, cairn, {"INV-068", "INV-069", "INV-070"}) and ok
    finally:
        if created:
            target.unlink(missing_ok=True)
    assert_boundary(root)
    print("git ls-files .cairn:\n" + "\n".join(sorted(EXPORTED_CAIRN)), flush=True)
    print("Scratch local context deleted", flush=True)
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--checks", choices=("full", "non-docker", "prepare"), default="full")
    parser.add_argument("--cairn", default="cairn")
    args = parser.parse_args()
    try:
        source, destination = Path.cwd(), args.destination.resolve()
        oid = export(source, destination, args.ref)
        print(f"Exported tree {oid}; ref={args.ref}; mode={args.checks}", flush=True)
        if args.checks == "prepare":
            print("Export prepared; no gate/QA/cairn acceptance claimed")
            return 0
        ok = checks(source, destination, args.checks, args.cairn)
        print("REHEARSAL: " + ("OK" if ok else "KO"))
        return 0 if ok else 1
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError):
        print("REHEARSAL: KO configuration/boundary/invariant error (details withheld)")
        return 1


if __name__ == "__main__":
    sys.exit(main())
