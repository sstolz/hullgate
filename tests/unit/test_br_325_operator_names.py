"""BR-325: private ratchet, byte scan and checkout/worktree boundary."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import operator_names as guard
import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_br_325_operator_names() -> None:
    # Slice 2 is strict: even an uncommitted restoration of the old allowance fails.
    assert not (ROOT / guard.BASELINE).exists(), "BR-325: baseline retired after slice 2"
    try:
        status, lines = guard.check(ROOT)
    except ValueError as exc:
        pytest.fail(str(exc), pytrace=False)
    for line in lines:
        print(line)
    if status == "skip":
        pytest.skip(lines[0])


def command(root: Path, *args: str, data: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        input=data,
        capture_output=True,
        check=True,
        env={
            "PATH": "/usr/bin:/bin",
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "a@example.test",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "a@example.test",
        },
    ).stdout


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Scratch repositories must not inherit a caller's alternate index/object store.
    for key in ("GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        monkeypatch.delenv(key, raising=False)
    command(tmp_path, "init", "-q")
    path = tmp_path / guard.DENYLIST
    path.parent.mkdir(parents=True)
    rows = [
        dict(
            index=f"E{i}",
            pattern=f"fixture{i}",
            case="insensitive",
            bounds="both",
            sample=f"FIXTURE{i}",
            exclusions=[],
        )
        for i in range(1, 9)
    ]
    path.write_text(json.dumps(rows))
    (tmp_path / guard.CONTEXT).write_text("private fixture\n")
    (tmp_path / "readme").write_text("clean\n")
    command(tmp_path, "add", ".")
    return tmp_path


def record(root: Path) -> None:
    """Create a local fixture snapshot for worktree tests; never commit the user's checkout."""
    command(root, "add", ".")
    tree = command(root, "write-tree").strip().decode()
    head = guard.git(root, "rev-parse", "--verify", "HEAD")
    parents = ["-p", head.stdout.decode().strip()] if head.returncode == 0 else []
    commit = command(root, "commit-tree", tree, *parents, data=b"fixture\n").strip().decode()
    command(root, "update-ref", "HEAD", commit)


def write_baseline(root: Path, count: int) -> None:
    (root / guard.BASELINE).write_text(json.dumps([dict(file="readme", entry="E1", count=count)]))
    command(root, "add", guard.BASELINE)


@pytest.mark.parametrize("index", range(1, 9))
def test_br_325_each_entry_rejects_a_new_match(repo: Path, index: int) -> None:
    (repo / "readme").write_text(f"fixture{index}\n")
    with pytest.raises(ValueError, match=rf"readme:1 E{index}") as exc:
        guard.check(repo)
    assert f"fixture{index}" not in str(exc.value)


def test_br_325_paths_bytes_and_population(repo: Path) -> None:
    (repo / "fixture1.txt").write_bytes(b"\xff\0fixture2\n")
    command(repo, "add", ".")
    result = guard.scan(repo, guard.tracked(repo), guard.entries(repo))
    assert result.counts == {("fixture1.txt", "E1"): 1, ("fixture1.txt", "E2"): 1}
    assert result.diagnostics == ["fixture1.txt:0 E1", "fixture1.txt:1 E2"]
    assert result.binary == ["fixture1.txt"]
    assert result.files == 2


def test_br_325_public_skip_private_failure(repo: Path) -> None:
    (repo / guard.DENYLIST).unlink()
    with pytest.raises(ValueError, match="private context present"):
        guard.check(repo)
    (repo / guard.CONTEXT).unlink()
    status, lines = guard.check(repo)
    assert status == "skip"
    assert "public/export" in lines[0]


def test_br_325_ratchet_shrinks_but_never_grows(repo: Path) -> None:
    (repo / "readme").write_text("fixture1 fixture1\n")
    write_baseline(repo, 2)
    record(repo)
    assert guard.check(repo)[0] == "pass"
    (repo / "readme").write_text("fixture1\n")
    with pytest.raises(ValueError, match="baseline stale"):
        guard.check(repo)
    write_baseline(repo, 1)
    assert guard.check(repo)[0] == "pass"
    write_baseline(repo, 3)
    with pytest.raises(ValueError, match="baseline grew"):
        guard.check(repo)
    (repo / "readme").write_text("clean\n")
    (repo / guard.BASELINE).unlink()
    command(repo, "add", "-u")
    assert guard.check(repo)[0] == "pass"


def test_br_325_worktree_has_private_inputs_and_fails_if_missing(
    repo: Path, tmp_path: Path
) -> None:
    record(repo)
    linked = tmp_path / "linked"
    command(repo, "worktree", "add", "--detach", str(linked), "HEAD")
    assert (linked / ".git").is_file()
    assert guard.check(linked)[0] == "pass"
    (linked / guard.DENYLIST).unlink()
    with pytest.raises(ValueError, match="private context present"):
        guard.check(linked)


def test_br_325_samples_exclusions_and_bounds(repo: Path) -> None:
    path = repo / guard.DENYLIST
    rows = json.loads(path.read_text())
    rows[0]["exclusions"] = [dict(path="readme", literal="fixture1 colour", reason="colour")]
    path.write_text(json.dumps(rows))
    (repo / "readme").write_text("fixture1 colour\n")
    assert guard.check(repo)[0] == "pass"
    (repo / "readme").write_text("fixture1\n")
    with pytest.raises(ValueError, match="stale exclusion"):
        guard.check(repo)
    rows[0]["sample"] = "misspelled"
    path.write_text(json.dumps(rows))
    with pytest.raises(ValueError, match="sample"):
        guard.check(repo)


def test_br_325_strict_cannot_regain_baseline(repo: Path) -> None:
    (repo / "scripts").mkdir()
    (repo / guard.GUARD).write_text("fixture guard\n")
    record(repo)
    (repo / "readme").write_text("fixture1\n")
    write_baseline(repo, 1)
    with pytest.raises(ValueError, match="cannot regain"):
        guard.check(repo)


def test_br_325_case_and_token_bounds(repo: Path) -> None:
    path = repo / guard.DENYLIST
    rows = json.loads(path.read_text())
    rows[0].update(pattern="sample", sample="SAMPLE", bounds="both")
    rows[1].update(pattern=r"example\\?-private", sample="example-private", bounds="none")
    rows[2].update(pattern="/sample/path", sample="/sample/path", case="sensitive", bounds="none")
    rows[3].update(pattern="givenname", sample="givenname", bounds="none")
    rows[4].update(pattern="34567", sample="34567", bounds="both")
    rows[5].update(pattern="operator@", sample="operator@server", bounds="login", case="sensitive")
    path.write_text(json.dumps(rows))
    patterns = [entry.pattern for entry in guard.entries(repo)]
    positives = [
        b"SAMPLE",
        b"example\\-private",
        b"/sample/path/file",
        b"GIVENNAME",
        b"-34567 ",
        b"operator@server",
    ]
    negatives = [
        b"asamplez",
        b"exampleXprivate",
        b"/Sample/path",
        "givénname".encode(),
        b"abc34567def",
        b"xoperator@server",
    ]
    for pattern, positive, negative in zip(patterns, positives, negatives, strict=False):
        assert pattern.search(positive)
        assert pattern.search(negative) is None
    assert patterns[5].search(b"operator@") is None
    assert patterns[5].search(b"Operator@server") is None


def test_br_325_missing_population_and_untracked_input_fail(repo: Path) -> None:
    command(repo, "rm", "--cached", guard.DENYLIST)
    with pytest.raises(ValueError, match="denylist must be tracked"):
        guard.check(repo)
    with pytest.raises(ValueError, match="git ls-files failed"):
        guard.tracked(repo / "missing")


def test_br_325_symlinks_scan_link_text_without_following_targets(repo: Path) -> None:
    (repo / "link").symlink_to("fixture1-missing-target")
    command(repo, "add", "link")
    result = guard.scan(repo, guard.tracked(repo), guard.entries(repo))
    assert result.counts == {("link", "E1"): 1}
    assert result.files == 2


@pytest.mark.parametrize("state", ["working", "staged", "committed"])
def test_br_325_growth_against_private_baseline(repo: Path, state: str) -> None:
    (repo / "readme").write_text("fixture1\n")
    write_baseline(repo, 1)
    record(repo)
    (repo / "readme").write_text("fixture1 fixture1\n")
    if state == "staged":
        command(repo, "add", "readme")
    elif state == "committed":
        record(repo)
        assert command(repo, "status", "--porcelain") == b""
    with pytest.raises(ValueError, match="matches differ from baseline"):
        guard.check(repo)


@pytest.mark.parametrize("state", ["staged", "committed"])
def test_br_325_baseline_growth_cannot_hide_new_matches(repo: Path, state: str) -> None:
    (repo / "readme").write_text("fixture1\n")
    write_baseline(repo, 1)
    record(repo)
    (repo / "readme").write_text("fixture1 fixture1\n")
    write_baseline(repo, 2)
    if state == "committed":
        record(repo)
        assert command(repo, "status", "--porcelain") == b""
    with pytest.raises(ValueError, match="baseline grew"):
        guard.check(repo)


def test_br_325_clean_tree_accepts_only_baselined_matches(repo: Path) -> None:
    (repo / "readme").write_text("fixture1\n")
    write_baseline(repo, 1)
    record(repo)
    assert command(repo, "status", "--porcelain") == b""
    assert guard.check(repo)[0] == "pass"
    (repo / "new-file").write_text("fixture2\n")
    record(repo)
    assert command(repo, "status", "--porcelain") == b""
    with pytest.raises(ValueError, match="new-file:1 E2"):
        guard.check(repo)
