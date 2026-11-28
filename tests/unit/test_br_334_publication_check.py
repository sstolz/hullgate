"""BR-334, BR-335, BR-345: the publication check on scratch repositories.

A scratch private source carries a tag `v1.0.0`, a workshop file, a denylist and a later commit
with the publication overlay; `build` makes the push repository from `git init`, and each mode is
read on it, then on scratch repositories broken one property at a time. Identities are fixture
values of the noreply shape, never the operator's.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from publication_check import MESSAGES, OVERLAY, TAG, main

LOGIN = "octo-fixture"
# The noreply shape of F-025 §3.1, from fragments: BR-330 admits no literal public mail domain.
EMAIL = "1234567+octo-fixture" + "@" + "users.noreply." + "github.com"
WHO = ["--login", LOGIN, "--email", EMAIL]
HASH = "#"
OUTSIDE = "header outside F-025 §3.1's closed set:"


def _env(name: str = LOGIN, email: str = EMAIL) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return env | {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }


def git(
    root: Path, *args: str, data: bytes | None = None, env: dict[str, str] | None = None
) -> str:
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "tag.gpgSign=false", *args],
        cwd=root,
        input=data,
        capture_output=True,
        check=True,
        env=env or _env(),
    )
    return result.stdout.decode().strip()


def write(root: Path, files: dict[str, str]) -> None:
    for path, text in files.items():
        file = root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)


def denylist() -> str:
    entries = [
        {
            "index": f"E{i}",
            "case": "insensitive",
            "bounds": "both",
            "pattern": f"fixturehost{i}",
            "sample": f"at fixturehost{i}.",
        }
        for i in range(1, 9)
    ]
    return json.dumps(entries)


@pytest.fixture(scope="module")
def source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("private")
    git(root, "init", "-q", "-b", "main")
    write(
        root,
        {
            "README.md": "# app\n\nDeveloper notes, see " + HASH + "7.\n",
            "pyproject.toml": '[project]\nname = "app"\n',
            "src/app.py": "print('hello')\n",
            ".gitignore": "/build/\n",
            ".cairn/project-context.example.yaml": "project:\n  name: app\n",
            ".cairn/docker/Dockerfile.project": "FROM scratch\n",
            ".cairn/runs/acts/report.md": "workshop, not exported\n",
            ".cairn/runs/private/operator-denylist.json": denylist(),
        },
    )
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "first")
    git(root, "tag", "-a", TAG, "-m", "private tag")
    write(
        root,
        {
            "README.md": "# app\n\nFor outside readers, see HULL-7.\n",
            "LICENSE": "MIT License\n",
            "SECURITY.md": "# Security\n",
            "CONTRIBUTING.md": "# Contributing\n",
            ".github/workflows/gate.yml": "name: gate\n",
        },
    )
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "publication files")
    return root


def run(capsys: pytest.CaptureFixture[str], *argv: str | Path) -> tuple[int, str]:
    code = main([str(a) for a in argv])
    return code, capsys.readouterr().out


@pytest.fixture(scope="module")
def built(source: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("push") / "repo"
    assert main(["build", "--repo", str(out), "--source", str(source), *WHO]) == 0
    return out


def clone_of(repo: Path, target: Path) -> Path:
    subprocess.run(["git", "clone", "-q", "--no-local", str(repo), str(target)], check=True)
    return target


def drop_remote_refs(repo: Path) -> None:
    git(repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")


def retag(
    repo: Path,
    commit: str,
    message: str = MESSAGES["tag"],
    who: str = LOGIN,
    email: str = EMAIL,
    extra: str = "",
) -> None:
    tag = f"object {commit}\ntype commit\ntag {TAG}\ntagger {who} <{email}> 1 +0000\n{extra}"
    data = f"{tag}\n{message}".encode()
    oid = git(repo, "hash-object", "-t", "tag", "-w", "--stdin", "--literally", data=data)
    git(repo, "update-ref", f"refs/tags/{TAG}", oid)


def with_headers(repo: Path, commit: str, extra: str) -> str:
    """`commit` rewritten with `extra` header lines after its own: tree and message kept."""
    raw = git(repo, "cat-file", "commit", commit) + "\n"
    head, _, message = raw.partition("\n\n")
    data = f"{head}\n{extra}\n\n{message}".encode()
    return git(repo, "hash-object", "-t", "commit", "-w", "--stdin", "--literally", data=data)


def commit_with(repo: Path, base: str, extra: dict[str, str], message: str, **ident: str) -> str:
    index = repo / ".git" / "scratch.index"
    env = _env(**ident) | {"GIT_INDEX_FILE": str(index)}
    git(repo, "read-tree", f"{base}^{{tree}}", env=env)
    for path, text in extra.items():
        blob = git(repo, "hash-object", "-w", "--stdin", data=text.encode())
        git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env)
    tree = git(repo, "write-tree", env=env)
    index.unlink()
    return git(repo, "commit-tree", tree, data=message.encode(), env=_env(**ident))


def scratch_birth(
    built: Path, tmp: Path, extra: dict[str, str], message: str = MESSAGES["commit 1"], **ident: str
) -> tuple[Path, str]:
    repo = clone_of(built, tmp / "scratch")
    c1 = git(repo, "rev-parse", f"{TAG}^{{commit}}")
    commit = commit_with(repo, c1, extra, message, **ident)
    retag(repo, commit)
    return repo, commit


def test_BR_334_001_birth_ok_and_extra_build_context_path(
    source: Path, built: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = run(capsys, "birth", "--repo", built, "--commit", TAG, "--source", source, *WHO)
    assert (code, out.splitlines()[-1]) == (0, "PUBLICATION: OK"), out
    repo, commit = scratch_birth(built, tmp_path, {"src/extra.py": "x = 1\n"})
    code, out = run(capsys, "birth", "--repo", repo, "--commit", commit, "--source", source, *WHO)
    assert code == 1
    assert out.splitlines()[-1] == "PUBLICATION: KO 1 paths", out
    assert "src/extra.py: build-context path differs from tag v1.0.0" in out


def test_BR_334_001_birth_tree_is_the_tag_export_plus_overlay(built: Path, source: Path) -> None:
    paths = set(git(built, "ls-tree", "-r", "--name-only", TAG).splitlines())
    assert set(OVERLAY) <= paths
    assert not any(p.startswith(".cairn/runs/") for p in paths)
    assert git(built, "show", f"{TAG}:README.md") == git(source, "show", f"{TAG}:README.md")
    assert git(built, "rev-list", "--max-parents=0", "main") == git(
        built, "rev-parse", f"{TAG}^{{commit}}"
    )


def test_BR_334_002_header_screening(
    source: Path, built: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = {
        "foreign": ({"email": "someone@example.invalid"}, "commit 1: author address differs"),
        "name": ({"name": "someone-else"}, "commit 1: author name differs"),
    }
    for case, (ident, line) in cases.items():
        repo, commit = scratch_birth(built, tmp_path / case, {}, **ident)
        code, out = run(
            capsys, "birth", "--repo", repo, "--commit", commit, "--source", source, *WHO
        )
        assert code == 1, case
        assert line in out, case
    # Denylist on the identity headers alone: the supplied login and address are themselves a
    # denylist sample, so the name and address checks pass and only the screen can refuse —
    # on the commit and on the tagger of the tag.
    login = "fixturehost3"
    email = "1234567+" + login + "@" + "users.noreply." + "github.com"
    repo, commit = scratch_birth(built, tmp_path / "denylist", {}, name=login, email=email)
    retag(repo, commit, who=login, email=email)
    code, out = run(
        capsys,
        "birth",
        "--repo",
        repo,
        "--commit",
        commit,
        "--source",
        source,
        "--login",
        login,
        "--email",
        email,
    )
    assert code == 1, out
    assert out.splitlines()[:-1] == ["commit 1: denylist E3", "tag: denylist E3"], out
    repo, commit = scratch_birth(built, tmp_path / "message", {}, message="publish\n")
    code, out = run(capsys, "birth", "--repo", repo, "--commit", commit, "--source", source, *WHO)
    assert "commit 1: message differs from F-025 §3.1" in out
    repo = clone_of(built, tmp_path / "tag")
    retag(repo, git(repo, "rev-parse", f"{TAG}^{{commit}}"), message="hullgate 1.0.0\n\nRefs x\n")
    code, out = run(capsys, "birth", "--repo", repo, "--commit", TAG, "--source", source, *WHO)
    assert "tag: message differs from F-025 §3.1" in out
    code, out = run(
        capsys,
        "birth",
        "--repo",
        built,
        "--commit",
        TAG,
        "--source",
        source,
        "--login",
        LOGIN,
        "--email",
        "x@example.invalid",
    )
    assert (code, out.splitlines()[-1]) == (2, "PUBLICATION: KO configuration")


def test_BR_334_002_closed_header_set(
    source: Path, built: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Commit 1: tree, author, committer, each once; tag: object, type, tag, tagger, each once.
    commit_cases = {
        "extra": ("x-note fixture-value", f"commit 1: {OUTSIDE} unknown"),
        "encoding": ("encoding ISO-8859-1", f"commit 1: {OUTSIDE} encoding"),
        "signed": (
            "gpgsig -----BEGIN PGP SIGNATURE-----\n fixture\n -----END PGP SIGNATURE-----",
            f"commit 1: {OUTSIDE} gpgsig",
        ),
        "author": (f"author {LOGIN} <{EMAIL}> 1 +0000", "commit 1: header author repeated"),
    }
    for case, (extra, line) in commit_cases.items():
        repo = clone_of(built, tmp_path / f"commit-{case}")
        commit = with_headers(repo, f"{TAG}^{{commit}}", extra)
        retag(repo, commit)
        code, out = run(
            capsys, "birth", "--repo", repo, "--commit", commit, "--source", source, *WHO
        )
        assert code == 1, case
        assert line in out, out
        assert "fixture-value" not in out
        assert out.splitlines()[-1] == "PUBLICATION: KO 1 paths", out
    tag_cases = {
        "extra": ("x-note fixture-value\n", f"tag: {OUTSIDE} unknown"),
        "tagger": (f"tagger {LOGIN} <{EMAIL}> 2 +0000\n", "tag: header tagger repeated"),
    }
    for case, (extra, line) in tag_cases.items():
        repo = clone_of(built, tmp_path / f"tag-{case}")
        retag(repo, git(repo, "rev-parse", f"{TAG}^{{commit}}"), extra=extra)
        code, out = run(capsys, "birth", "--repo", repo, "--commit", TAG, "--source", source, *WHO)
        assert code == 1, case
        assert line in out, out
        assert "fixture-value" not in out
    # The unmodified birth objects carry exactly their sets.
    code, out = run(capsys, "birth", "--repo", built, "--commit", TAG, "--source", source, *WHO)
    assert (code, out.splitlines()[-1]) == (0, "PUBLICATION: OK"), out


def test_BR_335_001_closed_header_set_and_parent(
    source: Path, built: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Commit 2: tree, parent, author, committer, each once; at most one parent in `catch-up`,
    # and that parent commit 1 in `pre-push`.
    c1 = git(built, "rev-parse", f"{TAG}^{{commit}}")
    committer = f"committer {LOGIN} <{EMAIL}> 1 +0000"
    cases = {
        "extra": ("x-note fixture-value", f"commit 2: {OUTSIDE} unknown"),
        "committer": (committer, "commit 2: header committer repeated"),
        "second parent": (f"parent {c1}", "commit 2: header parent repeated"),
    }
    for case, (extra, line) in cases.items():
        repo = clone_of(built, tmp_path / case.replace(" ", "-"))
        commit = with_headers(repo, "main", extra)
        code, out = run(
            capsys, "catch-up", "--repo", repo, "--commit", commit, "--source", source, *WHO
        )
        assert code == 1, case
        assert line in out, out
        assert "fixture-value" not in out
    # A parent other than commit 1, read by `pre-push`.
    repo = clone_of(built, tmp_path / "orphan")
    drop_remote_refs(repo)
    tree = git(repo, "rev-parse", "main^{tree}")
    orphan = git(repo, "commit-tree", tree, data=MESSAGES["commit 2"].encode())
    git(repo, "update-ref", "refs/heads/main", orphan)
    code, out = run(capsys, "pre-push", "--repo", repo, "--source", source, *WHO)
    assert code == 1
    assert "commit 2: its parent is not commit 1" in out


def test_BR_335_001_catch_up(
    source: Path, built: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = run(
        capsys, "catch-up", "--repo", built, "--commit", "main", "--source", source, *WHO
    )
    assert (code, out.splitlines()[-1]) == (0, "PUBLICATION: OK"), out
    repo = clone_of(built, tmp_path / "extra")
    commit = commit_with(repo, "main", {"docs/extra.md": "x\n"}, MESSAGES["commit 2"])
    code, out = run(
        capsys, "catch-up", "--repo", repo, "--commit", commit, "--source", source, *WHO
    )
    assert out.splitlines()[-1] == "PUBLICATION: KO 1 paths", out
    # A ticket-shaped private reference in the switch commit's export.
    code, out = run(
        capsys,
        "catch-up",
        "--repo",
        built,
        "--commit",
        "main",
        "--source",
        source,
        "--switch",
        TAG,
        *WHO,
    )
    assert code == 1
    assert "README.md: line 3: ticket-shaped private reference (BR-350)" in out


def test_BR_345_001_pre_push(
    source: Path, built: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = run(capsys, "pre-push", "--repo", built, "--source", source, *WHO)
    assert (code, out.splitlines()[-1]) == (0, "PUBLICATION: OK"), out
    assert f"refs/heads/main {git(built, 'rev-parse', 'main')}" in out
    extra_ref = clone_of(built, tmp_path / "ref")
    drop_remote_refs(extra_ref)
    code, out = run(capsys, "pre-push", "--repo", extra_ref, "--source", source, *WHO)
    assert (code, out.splitlines()[-1]) == (0, "PUBLICATION: OK"), out
    git(extra_ref, "update-ref", "refs/heads/extra", "main")
    code, out = run(capsys, "pre-push", "--repo", extra_ref, "--source", source, *WHO)
    assert code == 1
    assert "refs/heads/extra: ref not expected" in out
    extra_commit = clone_of(built, tmp_path / "commit")
    drop_remote_refs(extra_commit)
    tree = git(extra_commit, "rev-parse", "main^{tree}")
    c3 = git(extra_commit, "commit-tree", tree, "-p", "main", data=MESSAGES["commit 2"].encode())
    git(extra_commit, "update-ref", "refs/heads/main", c3)
    code, out = run(capsys, "pre-push", "--repo", extra_commit, "--source", source, *WHO)
    assert code == 1
    assert "object outside the two public trees" in out
    note = clone_of(built, tmp_path / "note")
    drop_remote_refs(note)
    git(note, "notes", "add", "-m", "a note", "main")
    code, out = run(capsys, "pre-push", "--repo", note, "--source", source, *WHO)
    assert code == 1
    assert "refs/notes/commits: ref not expected" in out


def test_BR_345_002_clone(
    source: Path, built: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    private = clone_of(source, tmp_path / "private")
    clone = clone_of(built, tmp_path / "clone")
    code, out = run(
        capsys, "clone", "--repo", clone, "--source", source, "--private", private, *WHO
    )
    assert (code, out.splitlines()[-1]) == (0, "PUBLICATION: OK"), out
    raw = subprocess.run(
        ["git", "cat-file", "tag", f"refs/tags/{TAG}"], cwd=source, capture_output=True, check=True
    ).stdout
    # An unreferenced private tag object in the clone's object store.
    oid = git(clone, "hash-object", "-t", "tag", "-w", "--stdin", "--literally", data=raw)
    code, out = run(
        capsys, "clone", "--repo", clone, "--source", source, "--private", private, *WHO
    )
    assert code == 1
    assert f"{oid}: commit or tag object present in the private source" in out
