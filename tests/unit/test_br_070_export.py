"""BR-070 — the YAML export of the tree is deterministic and without secret."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from tests.support import REGISTRY, Gateway, export_of, standard_tree


def _populate(gw: Gateway, order: list[tuple[str, str, str]]) -> None:
    gw.build(*order)
    gw.ok("tree", "sensitive", "mpx/cairn/prod", "on")
    gw.ok("settings", "set", "mpx", "token_duration", "10d")
    gw.ok("tree", "lock", "acme")


TREE = [
    ("/", "client", "MPX"),
    ("/", "client", "Acme"),
    ("mpx", "project", "Cairn"),
    ("mpx/cairn", "environment", "Prod"),
    ("mpx/cairn", "environment", "Dev"),
    ("mpx/cairn/prod", "role", "Agent"),
]


def test_br_070_same_tree_same_bytes(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first, second = Gateway(tmp_path / "a"), Gateway(tmp_path / "b")
    try:
        _populate(first, TREE)
        shuffled = [*reversed(TREE[:2]), TREE[2], *reversed(TREE[3:5]), TREE[5]]
        _populate(second, shuffled)
        one = export_of(first)
        assert first.ok("export") == first.ok("export")  # the same database, byte for byte
        assert one == export_of(second)  # another database: its own identifier only differs
    finally:
        first.close()
        second.close()


def test_br_070_content_and_order(gw: Gateway) -> None:
    _populate(gw, TREE)
    out = gw.ok("export")
    assert out.startswith("# hullgate: tree export (BR-070)\nformat: 5\norganisation:\n  id: ")
    segments = re.findall(r'segment: "([a-z0-9-]+)"', out)
    assert segments == ["acme", "mpx", "cairn", "dev", "principal", "prod", "agent", "principal"]
    assert "sensitive: true" in out
    assert 'token_duration: "10d"' in out
    assert 'lock: "true"' in out  # lock of acme
    assert 'name: "MPX"' in out


def test_br_070_no_token_hash_client_audit_nor_identifier(gw: Gateway) -> None:
    standard_tree(gw)
    token_id, value = gw.issue("mpx/cairn")
    conn = gw.connect()
    conn.execute(
        "INSERT INTO client (client_id, kind, node_id, created_at) "
        "VALUES ('https://client.example/meta', 'cimd', NULL, 0)"
    )
    conn.commit()
    node_ids = [r[0] for r in conn.execute("SELECT id FROM node WHERE level != 'org'")]
    conn.close()
    out = gw.ok("export")
    for needle in (
        value,
        hashlib.sha256(value.encode()).hexdigest(),
        token_id,
        "client.example",
        "operator-cli",
        "tree.add",
        *node_ids,
    ):
        assert needle not in out, needle


def test_br_070_format_key_and_changed_organisation_values(gw: Gateway) -> None:
    """S23: `format` opens the document; a changed organisation value is exported."""
    gw.ok("settings", "set", "/", "token_duration", "60d")
    out = gw.ok("export")
    assert out.splitlines()[1] == "format: 5"
    assert out.splitlines()[3] == f'  id: "{gw.org_id}"'  # F-014: first key under `organisation:`
    assert 'token_duration: "60d"' in out


def _library(gw: Gateway, order: list[str]) -> None:
    standard_tree(gw)
    for sid in order:
        gw.service(sid, "gitea" if sid == "gitea-dev" else "generic")
    secret = "export-secret-" + "k" * 20
    REGISTRY.secrets.append(secret)
    gw.ok("application", "declare", "app", "--service", "gitea-dev", "--mode", "declared",
          "--client-id", "client-x", "--secret-file", "-", stdin=secret)  # fmt: skip
    names = [("mpx/zeta", "gz"), ("mpx/alpha", "ga")]
    for name, prefix in names if order[0] == "gitea-dev" else reversed(names):
        value = f"value-{prefix}-" + "w" * 20
        REGISTRY.secrets.append(value)
        gw.ok("connection", "add", name, "--service", "gitea-dev", "--provided", "--file",
              "-", "--prefix", prefix, stdin=value)  # fmt: skip
    gw.ok("connection", "add", "mpx/oauth", "--service", "gitea-dev", "--application", "app",
          "--prefix", "go")  # fmt: skip
    gw.ok("connection", "place", "mpx/alpha", "mpx/cairn/staging/principal")
    gw.ok("connection", "place", "mpx/alpha", "mpx/cairn/staging/dev")


def test_br_070_v2_library_exported_deterministically_without_secret(tmp_path: Path) -> None:
    """F-006 (BR-070 v2): services, applications and connections, format 2, two exports identical
    byte for byte, no token and no secret."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first, second = Gateway(tmp_path / "a"), Gateway(tmp_path / "b")
    try:
        _library(first, ["gitea-dev", "notion"])
        _library(second, ["notion", "gitea-dev"])
        one = export_of(first)
        assert first.ok("export") == first.ok("export")
        assert one == export_of(second)
    finally:
        first.close()
        second.close()
    assert "format: 5" in one
    library = one.split("\nservices:", 1)[1]
    assert library.index('id: "gitea-dev"') < library.index('id: "notion"')
    assert 'client_id: "client-x"' in library
    assert library.index('name: "mpx/alpha"') < library.index('name: "mpx/oauth"')
    alpha = library.split('name: "mpx/alpha"', 1)[1].split("\n  - name:", 1)[0]
    assert alpha.index('path: "mpx/cairn/staging/dev"') < alpha.index(
        'path: "mpx/cairn/staging/principal"'
    )  # placements as mappings, sorted (format 4, F-015)
    assert "application: null" in library
    assert 'state: "pending"' in library
    for needle in REGISTRY.secrets:
        assert needle not in one
    assert "secret" not in library
