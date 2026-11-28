"""F-025 §3.5: the identity of every commit pushed to the public repository, read before the push.

Before each push to the public repository, from any clone, worktree or container, the session
reads the author and committer, name and address, of every commit it pushes against the login
and the GitHub noreply address of F-025 §3.1, and does not push on a difference. AC-OP-06 reads
the same headers on the commits of the first public pull request (``pr-commit-identity``).

  python scripts/push_identity.py --login <login> --email <noreply> <revision>...

The revisions are given as to ``git rev-list``: ``origin/main..HEAD`` for a branch about to be
pushed, ``<sha>`` or a range for a pull request's commits fetched locally. Both values are supplied
by the operator, never searched for, never read from the clone's own git configuration (the
reading would then confirm the configuration it checks), and written in no tracked file.

Output: one ``<label>: <sha> <author name> <author email> <committer name> <committer email>``
line per commit (``--label``, ``push-identity`` by default, ``pr-commit-identity`` for AC-OP-06),
one line per difference, then ``IDENTITY: OK`` or ``IDENTITY: KO <n> commits`` (exit 1);
``IDENTITY: KO configuration`` (exit 2) when the reading cannot run, or reads no commit.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from publication_check import NOREPLY

# One record per commit: sha, author name, author email, committer name, committer email.
FORMAT = "%H%x00%an%x00%ae%x00%cn%x00%ce%x1e"


class ReadingError(Exception):
    """The reading cannot run; the message names the cause, never a private value."""


def commits(repo: Path, revisions: list[str]) -> list[tuple[str, str, str, str, str]]:
    result = subprocess.run(
        ["git", "log", f"--format={FORMAT}", *revisions, "--"],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ReadingError("git log failed on the given revisions")
    records = []
    for record in result.stdout.decode(errors="replace").split("\x1e"):
        fields = record.strip("\n").split("\x00")
        if len(fields) == 5:
            sha, an, ae, cn, ce = fields
            records.append((sha, an, ae, cn, ce))
    return records


def differences(record: tuple[str, str, str, str, str], login: str, email: str) -> list[str]:
    sha, an, ae, cn, ce = record
    found = []
    for role, name, address in (("author", an, ae), ("committer", cn, ce)):
        if name != login:
            found.append(f"{sha}: {role} name differs from the supplied login")
        if address != email:
            found.append(f"{sha}: {role} address differs from the supplied noreply address")
    return found


def read(repo: Path, revisions: list[str], login: str, email: str, label: str) -> int:
    match = NOREPLY.fullmatch(email)
    if not login or match is None or match[1] != login:
        raise ReadingError("the supplied address is not the GitHub noreply address of the login")
    records = commits(repo, revisions)
    if not records:
        raise ReadingError("no commit in the given revisions")
    failing = set()
    for record in records:
        print(f"{label}: {' '.join(record)}")
    for record in records:
        for line in differences(record, login, email):
            print(line)
            failing.add(record[0])
    if failing:
        print(f"IDENTITY: KO {len(failing)} commits")
        return 1
    print("IDENTITY: OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("revisions", nargs="+", help="as to git rev-list: origin/main..HEAD")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="the clone read")
    parser.add_argument("--login", required=True, help="the operator's GitHub login")
    parser.add_argument("--email", required=True, help="the operator's GitHub noreply address")
    parser.add_argument("--label", default="push-identity", help="pr-commit-identity for AC-OP-06")
    args = parser.parse_args(argv)
    try:
        return read(args.repo, args.revisions, args.login, args.email, args.label)
    except ReadingError as exc:
        print(f"push identity: {exc}")
    except OSError:
        print("push identity: configuration error (details withheld)")
    print("IDENTITY: KO configuration")
    return 2


if __name__ == "__main__":
    sys.exit(main())
