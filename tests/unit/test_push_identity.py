"""F-025 §3.5: the pre-push identity reading of the commits pushed to the public repository.

Scratch repositories only; identities are fixture values of the noreply shape, never the
operator's (BR-330 admits no literal public mail domain: the address is built from fragments).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from push_identity import main

LOGIN = "octo-fixture"
EMAIL = "1234567+octo-fixture" + "@" + "users.noreply." + "github.com"
OTHER = "someone" + "@" + "example.invalid"
WHO = ["--login", LOGIN, "--email", EMAIL]


def commit(root: Path, name: str, *, author: tuple[str, str], committer: tuple[str, str]) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")} | {
        "GIT_AUTHOR_NAME": author[0],
        "GIT_AUTHOR_EMAIL": author[1],
        "GIT_COMMITTER_NAME": committer[0],
        "GIT_COMMITTER_EMAIL": committer[1],
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    (root / name).write_text(name)
    for args in (["add", name], ["commit", "-q", "-m", name]):
        subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false", *args],
            cwd=root,
            check=True,
            env=env,
        )
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True)
    return out.stdout.decode().strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    commit(tmp_path, "base", author=(LOGIN, EMAIL), committer=(LOGIN, EMAIL))
    return tmp_path


def run(capsys: pytest.CaptureFixture[str], repo: Path, *argv: str) -> tuple[int, str]:
    code = main([*argv, "--repo", str(repo)])
    return code, capsys.readouterr().out


def test_push_identity_ok(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sha = commit(repo, "a", author=(LOGIN, EMAIL), committer=(LOGIN, EMAIL))
    code, out = run(capsys, repo, *WHO, "HEAD~1..HEAD")
    assert code == 0
    assert out.splitlines() == [
        f"push-identity: {sha} {LOGIN} {EMAIL} {LOGIN} {EMAIL}",
        "IDENTITY: OK",
    ]


def test_push_identity_label_for_ac_op_06(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run(capsys, repo, *WHO, "HEAD", "--label", "pr-commit-identity")
    assert code == 0
    assert out.startswith("pr-commit-identity: ")


@pytest.mark.parametrize(
    ("author", "committer", "expected"),
    [
        ((LOGIN, OTHER), (LOGIN, EMAIL), "author address differs"),
        (("someone", EMAIL), (LOGIN, EMAIL), "author name differs"),
        ((LOGIN, EMAIL), (LOGIN, OTHER), "committer address differs"),
        ((LOGIN, EMAIL), ("someone", EMAIL), "committer name differs"),
    ],
)
def test_push_identity_difference_fails(
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    author: tuple[str, str],
    committer: tuple[str, str],
    expected: str,
) -> None:
    commit(repo, "good", author=(LOGIN, EMAIL), committer=(LOGIN, EMAIL))
    bad = commit(repo, "bad", author=author, committer=committer)
    code, out = run(capsys, repo, *WHO, "HEAD~2..HEAD")
    assert code == 1
    assert f"{bad}: {expected}" in out
    assert out.splitlines()[-1] == "IDENTITY: KO 1 commits"


def test_push_identity_refuses_a_non_noreply_value(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = run(capsys, repo, "--login", LOGIN, "--email", OTHER, "HEAD")
    assert code == 2
    assert out.splitlines()[-1] == "IDENTITY: KO configuration"
    assert OTHER not in out


def test_push_identity_empty_range_fails(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run(capsys, repo, *WHO, "HEAD..HEAD")
    assert code == 2
    assert "no commit" in out


def test_push_identity_unknown_revision_fails(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = run(capsys, repo, *WHO, "no-such-ref")
    assert code == 2
    assert out.splitlines()[-1] == "IDENTITY: KO configuration"
