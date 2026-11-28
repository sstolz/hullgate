"""BR-132, BR-066 v4, BR-039 (F-010 §3.3, §3.4; AC-09 to AC-13): migration 6 rewrites every stored
French value to its English spelling or stops.

A version-5 database is fabricated with every old value of F-010 §3.2 "Stored values" (this file
is one of the pinned exclusions of the sweeps: it must carry the old values). It is migrated the
way the gateway does it (`Database.open`), then compared field by field with the same rows mapped
by the table below, which is written out here as literals, never derived from the code.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from hullgate import backup, migrations
from hullgate.app import build_public_app
from hullgate.db import Database
from tests.support import shared_key

OLD_URI = "http://127.0.0.1/oauth/retour"
NEW_URI = "http://127.0.0.1/oauth/callback"

# F-010 §3.2 "Stored values", literal (column -> old -> new).
MAPPING: dict[str, dict[str | None, str | None]] = {
    "connection.kind": {"oauth": "oauth", "fourni": "provided"},
    "connection.state": {
        "en_attente": "pending",
        "active": "active",
        "reauth_requise": "reauth_required",
    },
    "connection.last_outcome": {
        None: None,
        "rendu": "renewed",
        "definitive": "permanent",
        "incertaine": "uncertain",
        "transitoire": "transient",
        "anomalie": "anomaly",
    },
    "connection.last_reason": {
        None: None,
        "invalid_grant": "invalid_grant",  # a provider code: kept
        "sans refresh_token": "no_refresh_token",
        "bail échu sans résultat": "lease_expired",
        "écriture impossible avant la fin du bail": "lease_write_failed",
        "révoquée": "revoked",
        "application réenregistrée": "application_reregistered",
        "refus signalé : 401 du serveur MCP": "mcp_refusal_401",
        "refus amont signalé": "upstream_refusal",
        "échéance trop courte": "expiry_too_short",
    },
    "service.model": {"gitea": "gitea", "generique": "generic"},
    "service.upstream_type": {None: None, "mcp-distant": "mcp-public", "mcp-local": "mcp-internal"},
    "application.mode": {"declaree": "declared", "dynamique": "dynamic"},
    "application.redirect_uri": {OLD_URI: NEW_URI},
    "setting.key": {"duree_jetons": "token_duration", "verrou": "lock"},
    "key_alert.reason": {
        "clé absente": "key_absent",
        "clé invalide": "key_invalid",
        "clé différente": "key_different",
    },
}
ENGLISH = {
    column: {v for v in values.values() if v is not None} for column, values in MAPPING.items()
}
ENGLISH["connection.last_reason"] -= {"invalid_grant"}
SEALED = {"connection": "tokens_enc", "application": "secret_enc", "pending_auth": "verifier_enc"}

# Every old value of §3.2 at least once; placement, pending_auth, key_alert, tool_name populated.
V5_DATA = """
INSERT INTO node (id, parent_id, level, slug, name, created_at) VALUES
    ('c1', 'org', 'client', 'mpx', 'MPX', 0),
    ('p1', 'c1', 'project', 'cairn', 'Cairn', 0),
    ('e1', 'p1', 'env', 'staging', 'Staging', 0),
    ('r1', 'e1', 'role', 'dev', 'Dev', 0);
INSERT INTO setting (node_id, key, value) VALUES ('c1', 'duree_jetons', '10d'),
    ('c1', 'verrou', 'true');
INSERT INTO service (id, model, base_url, created_at, upstream_type, mcp_url, mcp_internal) VALUES
    ('s-gitea', 'gitea', 'https://git.example.test', 1, 'mcp-distant',
        'https://m.example.test/mcp', 0),
    ('s-gen', 'generique', 'https://gen.example.test', 2, 'mcp-local', 'http://10.0.0.5/mcp', 1),
    ('s-none', 'generique', 'https://none.example.test', 3, NULL, NULL, 0);
INSERT INTO application (id, service_id, mode, client_id, redirect_uri, secret_enc, created_at,
    auth_method) VALUES
    ('a-decl', 's-gitea', 'declaree', 'cid-1', 'http://127.0.0.1/oauth/retour', randomblob(48), 4,
        'client_secret_basic'),
    ('a-dyn', 's-gen', 'dynamique', 'cid-2', 'http://127.0.0.1/oauth/retour', NULL, 5, 'none');
INSERT INTO connection (id, client_node_id, name, prefix, service_id, application_id, kind, account,
    state, last_outcome, last_reason, tokens_enc, removed, created_at) VALUES
    ('k01', 'c1', 'n01', 'p01', 's-gitea', NULL, 'fourni', 'acct', 'active', NULL, NULL,
        randomblob(64), 0, 10),
    ('k02', 'c1', 'n02', 'p02', 's-gitea', 'a-decl', 'oauth', NULL, 'en_attente', NULL, NULL,
        NULL, 0, 11),
    ('k03', 'c1', 'n03', 'p03', 's-gitea', 'a-decl', 'oauth', NULL, 'reauth_requise',
        'definitive', 'sans refresh_token', NULL, 0, 12),
    ('k04', 'c1', 'n04', 'p04', 's-gen', 'a-dyn', 'oauth', 'x', 'active', 'rendu', NULL,
        randomblob(64), 0, 13),
    ('k05', 'c1', 'n05', 'p05', 's-gen', 'a-dyn', 'oauth', 'x', 'active', 'incertaine',
        'bail échu sans résultat', randomblob(64), 0, 14),
    ('k06', 'c1', 'n06', 'p06', 's-gen', 'a-dyn', 'oauth', 'x', 'active', 'incertaine',
        'écriture impossible avant la fin du bail', randomblob(64), 0, 15),
    ('k07', 'c1', 'n07', 'p07', 's-gen', 'a-dyn', 'oauth', 'x', 'reauth_requise', NULL,
        'révoquée', NULL, 0, 16),
    ('k08', 'c1', 'n08', 'p08', 's-gen', 'a-dyn', 'oauth', 'x', 'reauth_requise', NULL,
        'application réenregistrée', NULL, 0, 17),
    ('k09', 'c1', 'n09', 'p09', 's-gitea', NULL, 'fourni', NULL, 'reauth_requise', 'definitive',
        'refus signalé : 401 du serveur MCP', randomblob(64), 0, 18),
    ('k10', 'c1', 'n10', 'p10', 's-gitea', NULL, 'fourni', NULL, 'reauth_requise', 'definitive',
        'refus amont signalé', randomblob(64), 0, 19),
    ('k11', 'c1', 'n11', 'p11', 's-gitea', 'a-decl', 'oauth', NULL, 'reauth_requise',
        'definitive', 'échéance trop courte', NULL, 0, 20),
    ('k12', 'c1', 'n12', 'p12', 's-gitea', 'a-decl', 'oauth', NULL, 'active', 'transitoire',
        'invalid_grant', randomblob(64), 0, 21),
    ('k13', 'c1', 'n13', 'p13', 's-gitea', 'a-decl', 'oauth', NULL, 'active', 'anomalie', NULL,
        randomblob(64), 0, 22),
    ('k14', 'c1', 'n01', 'p01', NULL, NULL, 'fourni', NULL, 'active', NULL, NULL, NULL, 1, 23);
INSERT INTO placement (connection_id, role_id, created_at) VALUES ('k01', 'r1', 0),
    ('k04', 'r1', 0);
INSERT INTO pending_auth (connection_id, state_hash, verifier_enc, expires_at)
    VALUES ('k02', 'h', randomblob(40), 99);
INSERT INTO key_alert (connection_id, reason, at) VALUES ('k01', 'clé absente', 1),
    ('k04', 'clé invalide', 2), ('k05', 'clé différente', 3);
INSERT INTO tool_name (connection_id, upstream_name, public_name, created_at)
    VALUES ('k01', 'read', 'p01__read', 0);
INSERT INTO vault_meta (id, envelope_version, key_check) VALUES (1, 1, randomblob(16));
"""


# F-016 lot A, migration 8: the console's tables, empty after a restore of an older backup.
ADMIN_TABLES = {
    "administrator": 0, "passkey": 0, "admin_session": 0, "enrolment": 0,
    "webauthn_challenge": 0, "admin_attempt": 0, "admin_allowlist": 0,
}  # fmt: skip


def _audit_rows(n: int) -> str:
    return "".join(
        "INSERT INTO audit (at, actor, action, result, reason, detail) VALUES"  # noqa: S608 - fixture text
        f" ('2026-10-0{1 + i % 9}T00:00:00Z', 'gateway', 'connection.renew', 'definitive',"
        f' \'refus amont signalé\', \'{{"issue": "erreur_outil", "n": {i}}}\');\n'
        for i in range(n)
    )


def _v5(path: Path, audit_rows: int = 20, extra: str = "") -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    migrations.migrate(conn, migrations.MIGRATIONS[:5])
    assert migrations.user_version(conn) == 5
    conn.executescript(V5_DATA + _audit_rows(audit_rows) + extra)
    conn.close()


def _start(path: Path) -> Database:
    """The gateway's own start: open, WAL, migrations (BR-069)."""
    db = Database(str(path))
    assert db.open()
    return db


def _to_v6(path: Path) -> None:
    """Migrations 1 to 6 only: what migration 6 does, before F-015's migration 7 adds to it."""
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        migrations.migrate(conn, migrations.MIGRATIONS[:6])
    finally:
        conn.close()


def _rows(conn: sqlite3.Connection, table: str) -> list[dict[str, object]]:
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(f"SELECT rowid AS _rowid, * FROM {table} ORDER BY rowid").fetchall()  # noqa: S608
        return [dict(r) for r in rows]
    finally:
        conn.row_factory = None


TABLES = [
    "node", "setting", "operator_token", "client", "lineage", "auth_code", "refresh",
    "access_jti", "consent_request", "consent_failure", "totp_used", "instance_flag",
    "service", "application", "vault_meta", "connection", "placement", "pending_auth",
    "key_alert", "tool_name",
]  # fmt: skip


def _snapshot(path: Path) -> dict[str, list[dict[str, object]]]:
    conn = sqlite3.connect(path)
    try:
        return {t: _rows(conn, t) for t in TABLES}
    finally:
        conn.close()


def _mapped(before: dict[str, list[dict[str, object]]]) -> dict[str, list[dict[str, object]]]:
    expected: dict[str, list[dict[str, object]]] = json.loads(json.dumps(before, default=bytes.hex))
    for column, values in MAPPING.items():
        table, field = column.split(".")
        for row in expected[table]:
            row[field] = values[row[field]]  # type: ignore[index]
    return expected


def _digest(value: object) -> str | None:
    return hashlib.sha256(value).hexdigest() if isinstance(value, bytes) else None


def _sha(conn: sqlite3.Connection, sql: str) -> str:
    return hashlib.sha256(repr(conn.execute(sql).fetchall()).encode()).hexdigest()


# -- AC-09: every stored value is mapped ---------------------------------------------------------


def test_br_132_maps_every_stored_value(tmp_path: Path) -> None:
    path = tmp_path / "hullgate.db"
    _v5(path)
    before = _snapshot(path)
    counts = {t: len(rows) for t, rows in before.items()}
    for column, values in MAPPING.items():  # the fixture holds every old value of §3.2
        table, field = column.split(".")
        assert {r[field] for r in before[table]} == set(values), column
    sealed_before = {t: [_digest(r[c]) for r in before[t]] for t, c in SEALED.items()}
    _to_v6(path)
    after = _snapshot(path)
    conn = sqlite3.connect(path)
    try:
        assert migrations.user_version(conn) == 6
        for column, english in ENGLISH.items():
            table, field = column.split(".")
            got = {r[0] for r in conn.execute(f"SELECT DISTINCT {field} FROM {table}")}  # noqa: S608
            assert got - {None, "invalid_grant"} == english, column
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    finally:
        conn.close()
    assert {t: len(rows) for t, rows in after.items()} == counts
    sealed_after = {t: [_digest(r[c]) for r in after[t]] for t, c in SEALED.items()}
    assert sealed_after == sealed_before
    # field by field: every row equals the v5 row with only the listed columns mapped
    assert json.loads(json.dumps(after, default=bytes.hex)) == _mapped(before)
    assert all(after[t] for t in ("placement", "pending_auth", "key_alert", "tool_name"))


def test_br_132_fresh_database_reaches_version_6_with_english_settings(tmp_path: Path) -> None:
    path = tmp_path / "hullgate.db"
    _to_v6(path)
    conn = sqlite3.connect(path)
    try:
        assert migrations.user_version(conn) == 6
        assert conn.execute("SELECT key, value FROM setting ORDER BY key").fetchall() == [
            ("lock", "false"),
            ("token_duration", "30d"),
        ]
        assert conn.execute("SELECT count(*) FROM audit").fetchone() == (0,)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        assert not {n for n in names if n.startswith("_vocab") or n.endswith("_v6")}
    finally:
        conn.close()


# -- AC-10: structure survives the rebuild --------------------------------------------------------

_LISTS = [
    ("'generique'", "'generic'"),
    ("'mcp-distant', 'mcp-local'", "'mcp-public', 'mcp-internal'"),
    ("'declaree', 'dynamique'", "'declared', 'dynamic'"),
    ("'oauth', 'fourni'", "'oauth', 'provided'"),
    ("'en_attente', 'active', 'reauth_requise'", "'pending', 'active', 'reauth_required'"),
    ("(kind = 'fourni')", "(kind = 'provided')"),
    # the one intended structural edit: a CHECK on `last_outcome` (F-010 §3.3)
    (
        "last_outcome TEXT,",
        "last_outcome TEXT CHECK (last_outcome IS NULL OR last_outcome IN ('renewed', 'permanent',"
        " 'uncertain', 'transient', 'anomaly')),",
    ),
]


def _norm(sql: str | None) -> str | None:
    if sql is None:
        return None
    text = re.sub(r"\s+", " ", sql.replace('"', ""))
    return re.sub(r"\s*([(),])\s*", r"\1", text).strip()


def _structure(conn: sqlite3.Connection) -> dict[tuple[str, str, str], str | None]:
    return {
        (t, n, tb): _norm(s)
        for t, n, tb, s in conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master")
    }


def _xinfo(conn: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    return {t: conn.execute(f"PRAGMA table_xinfo({t})").fetchall() for t in tables}


Structure = dict[tuple[str, str, str], str | None]


def _expected_structure(v5: Structure) -> Structure:
    out = {}
    for key, sql in v5.items():
        if sql is not None:
            for old, new in _LISTS:
                sql = sql.replace(_norm(old) or old, _norm(new) or new)
        out[key] = sql
    return out


def _structure_diff(v5_path: Path, v6_path: Path) -> list[str]:
    a, b = sqlite3.connect(v5_path), sqlite3.connect(v6_path)
    try:
        want, got = _expected_structure(_structure(a)), _structure(b)
        diffs = [
            f"sqlite_master {k}" for k in sorted(set(want) | set(got)) if want.get(k) != got.get(k)
        ]
        xa, xb = _xinfo(a), _xinfo(b)
        diffs += [f"table_xinfo {t}" for t in sorted(set(xa) | set(xb)) if xa.get(t) != xb.get(t)]
        return diffs
    finally:
        a.close()
        b.close()


def test_br_132_structure_survives_the_rebuild(tmp_path: Path) -> None:
    v5, v6 = tmp_path / "v5.db", tmp_path / "v6.db"
    _v5(v5)
    _v5(v6)
    _to_v6(v6)
    assert _structure_diff(v5, v6) == []
    conn = sqlite3.connect(v6)
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"connection_name", "connection_prefix", "application_service"} <= names
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):  # partial unique index on the live prefix
            conn.execute(
                "INSERT INTO connection (id, client_node_id, name, prefix, service_id, kind,"
                " state, created_at) VALUES ('k99', 'c1', 'n99', 'p01', 's-gitea', 'provided',"
                " 'active', 0)"
            )
        with pytest.raises(sqlite3.IntegrityError):  # the new CHECK on last_outcome
            conn.execute("UPDATE connection SET last_outcome = 'rendu' WHERE id = 'k01'")
        with pytest.raises(sqlite3.IntegrityError):  # foreign keys still reference the final names
            conn.execute("INSERT INTO placement VALUES ('nope', 'r1', 0)")
    finally:
        conn.close()


def test_br_132_structure_check_sees_a_lost_index(tmp_path: Path) -> None:
    """Negative control of the structure comparison: a dropped partial index is reported."""
    v5, v6 = tmp_path / "v5.db", tmp_path / "v6.db"
    _v5(v5)
    _v5(v6)
    _to_v6(v6)
    conn = sqlite3.connect(v6)
    conn.execute("DROP INDEX connection_prefix")
    conn.close()
    assert _structure_diff(v5, v6) == ["sqlite_master ('index', 'connection_prefix', 'connection')"]


# -- AC-11: fail closed -------------------------------------------------------------------------

UNKNOWN = [
    ("UPDATE connection SET last_outcome = 'bogus' WHERE id = 'k01'", "connection_last_outcome"),
    ("INSERT INTO setting (node_id, key, value) VALUES ('org', 'bogus', 'x')", "setting_key"),
    ("UPDATE application SET redirect_uri = 'http://127.0.0.1/cb' WHERE id = 'a-dyn'",
     "application_redirect_uri"),
    ("UPDATE key_alert SET reason = 'bogus' WHERE connection_id = 'k01'", "key_alert_reason"),
]  # fmt: skip


@pytest.mark.parametrize(("poison", "guard"), UNKNOWN, ids=[g for _, g in UNKNOWN])
def test_br_132_unknown_value_stops_at_version_5(tmp_path: Path, poison: str, guard: str) -> None:
    path = tmp_path / "hullgate.db"
    _v5(path, extra=poison + ";")
    raw = path.read_bytes()
    conn = sqlite3.connect(path, isolation_level=None)
    with pytest.raises(migrations.SchemaError) as caught:
        migrations.migrate(conn)
    assert caught.value.reason == "schema_error"
    assert f"vocab_guard_{guard}" in str(caught.value)
    assert migrations.user_version(conn) == 5
    conn.close()
    assert path.read_bytes() == raw  # the file is left unchanged
    db = _start(path)
    try:
        ready = TestClient(build_public_app(db, "https://gw.example.test", key=shared_key()))
        response = ready.get("/readyz")
        assert response.status_code == 503
        assert response.json()["reason"] == "schema_error"
    finally:
        db.close()
    conn = sqlite3.connect(path)
    assert migrations.user_version(conn) == 5
    conn.close()


def test_br_132_no_key_migrates_and_is_ready(tmp_path: Path) -> None:
    """BR-088 not engaged: no connections key is read; the gateway migrates and stays ready."""
    path = tmp_path / "hullgate.db"
    _v5(path)
    db = _start(path)
    try:
        client = TestClient(build_public_app(db, "https://gw.example.test", key=shared_key()))
        assert client.get("/readyz").status_code == 200
    finally:
        db.close()
    conn = sqlite3.connect(path)
    assert migrations.user_version(conn) == migrations.SCHEMA_VERSION
    conn.close()


# -- AC-12: audit history intact, one marker row --------------------------------------------------


def test_br_132_audit_untouched_and_one_marker(tmp_path: Path) -> None:
    path = tmp_path / "hullgate.db"
    _v5(path, audit_rows=20)
    conn = sqlite3.connect(path)
    old = _sha(conn, "SELECT * FROM audit WHERE seq <= 20 ORDER BY seq")
    assert conn.execute("SELECT max(seq) FROM audit").fetchone() == (20,)
    conn.close()
    _start(path).close()
    conn = sqlite3.connect(path)
    try:
        assert _sha(conn, "SELECT * FROM audit WHERE seq <= 20 ORDER BY seq") == old
        new = conn.execute(
            "SELECT seq, actor, action, result, node_id, path, reason, before, after, detail"
            " FROM audit WHERE seq > 20"
        ).fetchall()
        assert len(new) == 1
        seq, actor, action, result, node_id, at_path, reason, before, after, detail = new[0]
        assert (seq, actor, action, result) == (
            21,
            "migration",
            "instance.vocabulary_migrated",
            "ok",
        )
        assert (node_id, at_path, reason, before, after) == (None, None, None, None, None)
        data = json.loads(detail)
        assert (data["from"], data["to"]) == (5, 6)
        assert data["converted"] == {
            "application.mode": 2,
            "application.redirect_uri": 2,
            "connection.kind": 4,
            "connection.last_outcome": 9,
            "connection.last_reason": 8,
            "connection.state": 7,
            "key_alert.reason": 3,
            "service.model": 2,
            "service.upstream_type": 2,
            "setting.key": 4,
        }
        triggers = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        assert {"audit_append_only_update", "audit_append_only_delete"} <= triggers
    finally:
        conn.close()


def test_br_132_audit_empty_gets_no_marker(tmp_path: Path) -> None:
    path = tmp_path / "hullgate.db"
    _v5(path, audit_rows=0)
    _start(path).close()
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT count(*) FROM audit").fetchone() == (0,)
    conn.close()


# -- AC-13 (unit half): restore counts after migration, the started copy adds nothing ------------


def test_br_132_restore_of_a_v5_backup_counts_after_migration(tmp_path: Path) -> None:
    path = tmp_path / "hullgate.db"
    _v5(path, audit_rows=20)
    snap = tmp_path / "snap.db"
    v5_counts = backup.snapshot(str(path), str(snap))
    restored = tmp_path / "restored.db"
    expected = backup.restore(str(snap), str(restored))
    # the marker, once; F-015's migration 7 adds three settings and four empty tables
    added = {"tool_catalog": 0, "placement_tool": 0, "argument_rule": 0, "pinned_definition": 0}
    added |= ADMIN_TABLES  # F-016 lot A, migration 8
    assert expected == {
        **v5_counts,
        **added,
        "audit": v5_counts["audit"] + 1,
        "admin_setup": 1,
        "setting": v5_counts["setting"] + 3 + 2 + 1,  # F-020 migration 11: two settings
    }
    conn = sqlite3.connect(restored)
    assert migrations.user_version(conn) == migrations.SCHEMA_VERSION
    conn.close()
    _start(restored).close()  # the gateway starts on an already-migrated copy
    assert backup.counts(str(restored)) == expected  # no second marker
