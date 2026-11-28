"""F-025 publication check: the public repository's objects against the private source's exports.

Modes (F-025 §3.1):

- ``birth``    — a commit's tree against export(``v1.0.0``) plus the closed publication overlay,
  path by path; no build-context path differing from the tag; the commit's headers and message,
  and those of the annotated tag ``v1.0.0`` pointing at it (BR-334).
- ``catch-up`` — a commit's tree against export(switch commit), path by path; no ticket-shaped
  ``#N`` outside BR-350's exclusions in that export; the commit's headers and message (BR-335).
- ``pre-push`` — the push repository: refs exactly ``refs/heads/main`` (commit 2) and
  ``refs/tags/v1.0.0`` (the tag object); ``git rev-list --all --objects`` lists exactly commit 1,
  commit 2, the tag object and the trees and blobs of the two expected trees; the root commit has
  no parent; the birth and catch-up reads on its two commits (BR-334, BR-335, BR-345).
- ``clone``    — a fresh clone of the public repository: the reads of ``pre-push`` (its
  remote-tracking refs admitted), and no commit or tag object of the clone present in a fresh
  clone of the private remote, given as ``--private`` (``git cat-file -e``, BR-345).
- ``build``    — S3's gesture: a push repository born from ``git init`` in ``--repo``, never
  fetching the private source, holding commit 1, its tag and commit 2; then the ``pre-push``
  reads on it.

``--source`` is the private source checkout the expected exports are built from (the switch
commit ``--switch``, the overlay commit ``--overlay``, the tag ``v1.0.0``). Header screening, in
every mode: each author, committer and tagger address equals ``--email`` (the GitHub noreply
address ``<id>+<login>@users.noreply.github.com``) and each name ``--login``; headers and
messages pass the denylist of BR-325 read from ``--source``, with no header exception, and carry
no other e-mail address. Both values are supplied by the operator, never searched for, and
written in no tracked file. Each object's header set is closed, each header at most once:
commit 1 ``tree``, ``author``, ``committer``; commit 2 the same plus one ``parent``; the tag
``object``, ``type``, ``tag``, ``tagger`` — no signature, no ``encoding``.

Output: one line per difference, then ``PUBLICATION: OK`` or ``PUBLICATION: KO <n> paths``
(``n`` distinct paths or objects differing); ``PUBLICATION: KO configuration`` when the check
cannot run. Denylist findings name the entry index, never the matched text.
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import export_rehearsal
import operator_names

TAG = "v1.0.0"
MAIN = "refs/heads/main"
TAG_REF = f"refs/tags/{TAG}"
CLONE_REFS = ("refs/remotes/origin/main", "refs/remotes/origin/HEAD")
# The closed publication overlay (F-025 §3.1, Q2): adding a path is a change of the spec.
OVERLAY = ("LICENSE", "SECURITY.md", "CONTRIBUTING.md", ".github/workflows/gate.yml")
# The image's build inputs (`.dockerignore`, the package readme): commit 1 changes none of them.
BUILD_CONTEXT = ("pyproject.toml", "uv.lock", ".python-version", "README.md", "src/", "web/")
MESSAGES = {
    "commit 1": "chore(release): publish hullgate 1.0.0\n\n"
    "First public commit: the 1.0.0 tree, with LICENSE, SECURITY.md, CONTRIBUTING.md and the CI "
    "workflow. Earlier history is not public.\n",
    "commit 2": "chore(release): catch up with the working tree\n\n"
    "The tree of the working repository at the switch: the README for outside readers, the "
    "licence metadata and the changes since 1.0.0.\n",
    "tag": "hullgate 1.0.0\n",
}
NOREPLY = re.compile(r"\d+\+([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)@users\.noreply\.github\.com")
EMAIL = re.compile(rb"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
IDENT = re.compile(r"^(author|committer|tagger) (.*) <([^<>]*)> (\d+) ([+-]\d{4})$")
# The closed header set of each public object, each header at most once (F-025 §3.1, BR-334,
# BR-335): no `gpgsig`, no `encoding` — the public objects are unsigned for good.
HEADER_SETS = {
    "commit 1": ("tree", "author", "committer"),
    "commit 2": ("tree", "parent", "author", "committer"),
    "tag": ("object", "type", "tag", "tagger"),
}
# Header names a diagnostic may print; any other name is reported without its text, since a
# header outside the set is read before the denylist screen has said anything about it.
KNOWN_HEADERS = frozenset(
    {"tree", "parent", "author", "committer", "encoding", "gpgsig", "gpgsig-sha256", "mergetag"}
    | {"object", "type", "tag", "tagger"}
)
GUARD = Path(__file__).resolve().parents[1] / "tests/unit/test_br_350_private_refs.py"
Entries = list[operator_names.Entry]


class CheckError(Exception):
    """The check cannot run; the message names the cause, never a private value."""


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    subjects: set[str] = field(default_factory=set)

    def add(self, subject: str, difference: str) -> None:
        self.lines.append(f"{subject}: {difference}")
        self.subjects.add(subject)

    def verdict(self) -> int:
        for line in self.lines:
            print(line)
        if self.subjects:
            print(f"PUBLICATION: KO {len(self.subjects)} paths")
            return 1
        print("PUBLICATION: OK")
        return 0


@dataclass(frozen=True)
class Identity:
    login: str
    email: str


@dataclass(frozen=True)
class Tree:
    oid: str
    entries: dict[str, tuple[str, str]]  # path -> (mode, blob oid)
    repo: Path  # a scratch repository holding the tree's objects and files


@dataclass(frozen=True)
class Expected:
    birth: Tree
    catch_up: Tree


def git(
    root: Path, *args: str, data: bytes | None = None, env: dict[str, str] | None = None
) -> bytes:
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=root,
        input=data,
        capture_output=True,
        check=False,
        env=env if env is not None else export_rehearsal.isolated_env(),
    )
    if result.returncode:
        raise CheckError(f"git {args[0]} failed")
    return result.stdout


def succeeds(root: Path, *args: str) -> bool:
    env = export_rehearsal.isolated_env()
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, check=False, env=env)
    return result.returncode == 0


def guard() -> ModuleType:
    """BR-350's module: its ticket-shaped pattern, exclusions and pins."""
    spec = importlib.util.spec_from_file_location("br350_guard", GUARD)
    if spec is None or spec.loader is None:
        raise CheckError("BR-350 guard module absent")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ls_tree(root: Path, treeish: str) -> dict[str, tuple[str, str]]:
    entries = {}
    for item in git(root, "ls-tree", "-r", "-z", treeish).split(b"\0"):
        if item:
            meta, path = item.decode().split("\t", 1)
            mode, _kind, oid = meta.split()
            entries[path] = (mode, oid)
    return entries


# --- expected trees ---------------------------------------------------------------------------


def exported(source: Path, ref: str, scratch: Path, name: str) -> Tree:
    """export(`ref`) by F-022's export tool, in a scratch repository."""
    destination = scratch / name
    export_rehearsal.export(source, destination, ref)
    oid = git(destination, "rev-parse", "HEAD^{tree}").decode().strip()
    return Tree(oid, ls_tree(destination, oid), destination)


def birth_tree(source: Path, overlay_ref: str, scratch: Path) -> Tree:
    """export(`v1.0.0`) plus the overlay files of `overlay_ref`."""
    base = exported(source, TAG, scratch, "export-tag")
    overlay = ls_tree(source, f"{overlay_ref}^{{tree}}")
    for path in OVERLAY:
        if path not in overlay:
            raise CheckError(f"overlay path {path} absent from the overlay commit")
        if path in base.entries:
            raise CheckError(f"overlay path {path} already in the tag's export")
        mode, oid = overlay[path]
        if mode not in ("100644", "100755"):
            raise CheckError(f"overlay path {path} is not a regular file")
        file = base.repo / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(git(source, "cat-file", "blob", oid))
        file.chmod(0o755 if mode == "100755" else 0o644)
    git(base.repo, "add", "-f", "--", *OVERLAY)
    oid = git(base.repo, "write-tree").decode().strip()
    return Tree(oid, ls_tree(base.repo, oid), base.repo)


def tree_objects(tree: Tree) -> set[str]:
    objects = {tree.oid}
    for item in git(tree.repo, "ls-tree", "-r", "-t", "-z", tree.oid).split(b"\0"):
        if item:
            objects.add(item.decode().split("\t", 1)[0].split()[2])
    return objects


# --- reads ------------------------------------------------------------------------------------


def compare_tree(
    report: Report, label: str, actual: dict[str, tuple[str, str]], expected: Tree
) -> None:
    for path in sorted(set(actual) | set(expected.entries)):
        if path not in expected.entries:
            report.add(path, f"{label}: path not in the expected tree")
        elif path not in actual:
            report.add(path, f"{label}: expected path missing")
        elif actual[path] != expected.entries[path]:
            report.add(path, f"{label}: content or mode differs")


def build_context(path: str) -> bool:
    return any(path == p or (p.endswith("/") and path.startswith(p)) for p in BUILD_CONTEXT)


def compare_build_context(report: Report, actual: dict[str, tuple[str, str]], source: Path) -> None:
    tag = {p: v for p, v in ls_tree(source, f"{TAG_REF}^{{tree}}").items() if build_context(p)}
    mine = {p: v for p, v in actual.items() if build_context(p)}
    for path in sorted(set(tag) | set(mine)):
        if tag.get(path) != mine.get(path):
            report.add(path, f"build-context path differs from tag {TAG}")


def screen(report: Report, subject: str, raw: bytes, identity: Identity, entries: Entries) -> None:
    for entry in entries:
        if entry.pattern.search(raw):
            report.add(subject, f"denylist {entry.index}")
    if any(a.decode(errors="replace") != identity.email for a in EMAIL.findall(raw)):
        report.add(subject, "an e-mail address other than the supplied noreply address")


def headers(
    report: Report, root: Path, oid: str, kind: str, label: str, who: Identity, entries: Entries
) -> list[str]:
    """Read one commit or tag object's header set, identity lines and message; return its parents.

    The header set is closed (`HEADER_SETS`): a header outside it, a repeated one, or a value
    continued over several lines fails, whatever its value.
    """
    if not oid or git(root, "cat-file", "-t", oid).decode().strip() != kind:
        report.add(label, f"not a {kind} object")
        return []
    raw = git(root, "cat-file", kind, oid)
    head, _, message = raw.partition(b"\n\n")
    roles = {"commit": {"author", "committer"}, "tag": {"tagger"}}[kind]
    allowed = HEADER_SETS[label]
    counts: dict[str, int] = {}
    seen, parents, previous = set(), [], ""
    for line in head.decode(errors="replace").split("\n"):
        if line.startswith(" "):
            if previous in allowed:
                report.add(label, f"header {previous} continued over several lines")
            continue
        key, _, value = line.partition(" ")
        previous = key
        counts[key] = counts.get(key, 0) + 1
        if key not in allowed:
            name = key if key in KNOWN_HEADERS else "unknown"
            report.add(label, f"header outside F-025 §3.1's closed set: {name}")
        elif counts[key] == 2:
            report.add(label, f"header {key} repeated")
        if key == "parent":
            parents.append(value)
        match = IDENT.match(line)
        if match:
            seen.add(match[1])
            if match[2] != who.login:
                report.add(label, f"{match[1]} name differs from the supplied login")
            if match[3] != who.email:
                report.add(label, f"{match[1]} address differs from the supplied noreply address")
    if seen != roles:
        report.add(label, "identity header missing or malformed")
    if message.decode(errors="replace") != MESSAGES[label]:
        report.add(label, "message differs from F-025 §3.1")
    screen(report, label, raw, who, entries)
    return parents


def tag_target(report: Report, root: Path, tag_oid: str) -> str:
    if not tag_oid or git(root, "cat-file", "-t", tag_oid).decode().strip() != "tag":
        report.add("tag", f"{TAG} is not an annotated tag")
        return ""
    head = git(root, "cat-file", "tag", tag_oid).decode(errors="replace").partition("\n\n")[0]
    fields = dict(line.split(" ", 1) for line in head.split("\n") if " " in line)
    if fields.get("type") != "commit" or fields.get("tag") != TAG:
        report.add("tag", f"not an annotated tag {TAG} of a commit")
    return fields.get("object", "")


def check_birth(
    report: Report,
    root: Path,
    commit: str,
    expected: Tree,
    source: Path,
    who: Identity,
    entries: Entries,
) -> None:
    if headers(report, root, commit, "commit", "commit 1", who, entries):
        report.add("commit 1", "has a parent")
    actual = ls_tree(root, f"{commit}^{{tree}}")
    compare_tree(report, "birth", actual, expected)
    compare_build_context(report, actual, source)
    if not succeeds(root, "rev-parse", "--verify", "--quiet", TAG_REF):
        report.add("tag", f"{TAG_REF} absent")
        return
    tag_oid = git(root, "rev-parse", TAG_REF).decode().strip()
    if tag_target(report, root, tag_oid) != commit:
        report.add("tag", "does not point at commit 1")
    headers(report, root, tag_oid, "tag", "tag", who, entries)


def ticket_refs(report: Report, tree: Tree) -> None:
    module = guard()
    for path in sorted(tree.entries):
        if not module.outside_workshop(path):
            continue
        try:
            text = (tree.repo / path).read_bytes().decode("utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, _line in module.ticket_lines(path, text):
            report.add(path, f"line {number}: ticket-shaped private reference (BR-350)")


def check_catch_up(
    report: Report,
    root: Path,
    commit: str,
    expected: Tree,
    who: Identity,
    entries: Entries,
    parent: str | None,
) -> None:
    parents = headers(report, root, commit, "commit", "commit 2", who, entries)
    if parent is not None and parents != [parent]:
        report.add("commit 2", "its parent is not commit 1")
    compare_tree(report, "catch-up", ls_tree(root, f"{commit}^{{tree}}"), expected)
    ticket_refs(report, expected)


def check_repository(
    report: Report,
    root: Path,
    expected: Expected,
    source: Path,
    who: Identity,
    entries: Entries,
    clone: bool,
) -> tuple[str, str, str]:
    """`pre-push` reads (and `clone`'s refs); return commit 2, commit 1 and the tag object."""
    refs = {}
    listing = git(root, "for-each-ref", "--format=%(refname) %(objectname)").decode()
    for line in listing.splitlines():
        name, oid = line.split(" ", 1)
        refs[name] = oid
    allowed = {MAIN, TAG_REF} | (set(CLONE_REFS) if clone else set())
    for name in sorted(set(refs) - allowed):
        report.add(name, "ref not expected")
    for name in sorted({MAIN, TAG_REF} - set(refs)):
        report.add(name, "expected ref missing")
    c2, tag_oid = refs.get(MAIN, ""), refs.get(TAG_REF, "")
    if clone and any(refs.get(name, c2) != c2 for name in CLONE_REFS):
        report.add("refs/remotes/origin", "remote-tracking refs differ from main")
    if not c2 or not tag_oid:
        return c2, "", tag_oid
    c1 = tag_target(report, root, tag_oid)
    if c1:
        check_birth(report, root, c1, expected.birth, source, who, entries)
    check_catch_up(report, root, c2, expected.catch_up, who, entries, c1 or None)
    wanted = {c1, c2, tag_oid} | tree_objects(expected.birth) | tree_objects(expected.catch_up)
    listed = {
        line.split(" ", 1)[0]
        for line in git(root, "rev-list", "--all", "--objects").decode().splitlines()
        if line
    }
    listed |= set(git(root, "for-each-ref", "--format=%(objectname)", "refs/tags").decode().split())
    for oid in sorted(listed - wanted):
        report.add(oid, "object outside the two public trees and their three objects")
    for oid in sorted(wanted - listed):
        report.add(oid, "expected object missing")
    return c2, c1, tag_oid


def check_private_absence(report: Report, root: Path, private: Path) -> None:
    """No commit or tag object of `root`'s store, reachable or not, exists in `private`."""
    if not succeeds(private, "rev-parse", "--verify", "--quiet", "HEAD"):
        raise CheckError("private clone unreadable")
    listing = git(
        root, "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)"
    ).decode()
    for line in listing.splitlines():
        oid, kind = line.split()
        if kind in ("commit", "tag") and succeeds(private, "cat-file", "-e", oid):
            report.add(oid, "commit or tag object present in the private source")


# --- build (S3) -------------------------------------------------------------------------------


def build(out: Path, expected: Expected, who: Identity) -> None:
    """Commit 1, its tag and commit 2 in a repository born from `git init`; no fetch."""
    if out.exists():
        raise CheckError("push repository path exists")
    git(Path.cwd(), "init", "-q", "-b", "main", str(out))
    env = export_rehearsal.isolated_env() | {
        "GIT_AUTHOR_NAME": who.login,
        "GIT_AUTHOR_EMAIL": who.email,
        "GIT_COMMITTER_NAME": who.login,
        "GIT_COMMITTER_EMAIL": who.email,
    }
    commits: list[str] = []
    for tree, label in ((expected.birth, "commit 1"), (expected.catch_up, "commit 2")):
        index = out / ".git" / "publication.index"
        work = env | {
            "GIT_DIR": str(out / ".git"),
            "GIT_WORK_TREE": str(tree.repo),
            "GIT_INDEX_FILE": str(index),
        }
        files = git(tree.repo, "ls-files", "-z")
        git(tree.repo, "update-index", "--add", "-z", "--stdin", data=files, env=work)
        oid = git(tree.repo, "write-tree", env=work).decode().strip()
        index.unlink()
        if oid != tree.oid:
            raise CheckError(f"{label}: built tree differs from the expected export")
        parent = ["-p", commits[-1]] if commits else []
        message = MESSAGES[label].encode()
        commit = git(out, "commit-tree", oid, *parent, data=message, env=env).decode().strip()
        commits.append(commit)
        if label == "commit 1":
            tag = (
                f"object {commit}\ntype commit\ntag {TAG}\n"
                f"tagger {who.login} <{who.email}> {int(time.time())} +0000\n\n{MESSAGES['tag']}"
            )
            tag_oid = git(out, "mktag", data=tag.encode(), env=env).decode().strip()
            git(out, "update-ref", TAG_REF, tag_oid, "", env=env)
    git(out, "update-ref", MAIN, commits[-1], "", env=env)


# --- entry point ------------------------------------------------------------------------------


def identity(login: str, email: str) -> Identity:
    match = NOREPLY.fullmatch(email)
    if not login or match is None or match[1] != login:
        raise CheckError("the supplied address is not the GitHub noreply address of the login")
    return Identity(login, email)


def run(args: argparse.Namespace) -> int:
    who = identity(args.login, args.email)
    source = args.source.resolve()
    try:
        entries = operator_names.entries(source)
    except ValueError:
        raise CheckError("BR-325 denylist unreadable: headers cannot be screened") from None
    if args.repo is None:
        raise CheckError("--repo is required")
    root = args.repo.resolve()
    overlay = args.overlay or args.switch
    report = Report()
    with tempfile.TemporaryDirectory(prefix="publication-") as scratch:
        work = Path(scratch)
        if args.mode in ("birth", "catch-up"):
            if args.commit is None:
                raise CheckError("--commit is required")
            commit = git(root, "rev-parse", "--verify", f"{args.commit}^{{commit}}")
            oid = commit.decode().strip()
            if args.mode == "birth":
                tree = birth_tree(source, overlay, work)
                check_birth(report, root, oid, tree, source, who, entries)
            else:
                tree = exported(source, args.switch, work, "export-switch")
                check_catch_up(report, root, oid, tree, who, entries, None)
            return report.verdict()
        expected = Expected(
            birth_tree(source, overlay, work), exported(source, args.switch, work, "export-switch")
        )
        if args.mode == "build":
            build(root, expected, who)
        c2, c1, tag_oid = check_repository(
            report, root, expected, source, who, entries, args.mode == "clone"
        )
        if args.mode == "clone":
            if args.private is None:
                raise CheckError("--private is required in clone mode")
            check_private_absence(report, root, args.private.resolve())
        print(f"{MAIN} {c2}\n{TAG_REF} {tag_oid}\ncommit 1 (root) {c1}\ncommit 2 {c2}")
        return report.verdict()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("mode", choices=("birth", "catch-up", "pre-push", "clone", "build"))
    parser.add_argument("--repo", type=Path, help="repository read (or built, for build)")
    parser.add_argument("--commit", help="birth, catch-up: the commit of --repo read")
    parser.add_argument("--source", type=Path, default=Path.cwd(), help="private source checkout")
    parser.add_argument("--switch", default="HEAD", help="the switch commit of --source")
    parser.add_argument("--overlay", help="the commit of --source carrying the overlay")
    parser.add_argument("--private", type=Path, help="clone: a fresh clone of the private remote")
    parser.add_argument("--login", required=True, help="the operator's GitHub login")
    parser.add_argument("--email", required=True, help="the operator's GitHub noreply address")
    args = parser.parse_args(argv)
    try:
        return run(args)
    except CheckError as exc:
        print(f"publication check: {exc}")
    except (ValueError, OSError):
        print("publication check: configuration error (details withheld)")
    print("PUBLICATION: KO configuration")
    return 2


if __name__ == "__main__":
    sys.exit(main())
