"""Schema migrations, forward only, run at startup (BR-069).

Each migration is a sequence of statements run in its own transaction; the schema version is
`PRAGMA user_version`. A published migration is never modified: a schema change is one more
migration.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

ORG_ID = "org"  # identifier of the root, written in clear in migration 1


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    statements: tuple[str, ...]
    # Table rebuild (SQLite procedure): foreign keys suspended during the migration, then
    # checked (`PRAGMA foreign_key_check`) before the COMMIT.
    rebuild: bool = False


# Migration 1 (F-005): tree, settings, operator tokens, OAuth clients and lineages, audit.
_M1 = (
    """CREATE TABLE node (
        id TEXT PRIMARY KEY,
        parent_id TEXT REFERENCES node(id),
        level TEXT NOT NULL CHECK (level IN ('org', 'client', 'project', 'env', 'role')),
        slug TEXT NOT NULL,
        name TEXT NOT NULL,
        sensitive INTEGER NOT NULL DEFAULT 0 CHECK (sensitive IN (0, 1)),
        deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1)),
        created_at INTEGER NOT NULL
    )""",
    # Segment uniqueness among live siblings; a deleted node keeps its row (BR-045).
    "CREATE UNIQUE INDEX node_sibling_slug ON node(parent_id, slug) WHERE deleted = 0",
    "CREATE INDEX node_parent ON node(parent_id)",
    """CREATE TABLE setting (
        node_id TEXT NOT NULL REFERENCES node(id),
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        PRIMARY KEY (node_id, key)
    )""",
    """CREATE TABLE operator_token (
        id TEXT PRIMARY KEY,
        node_id TEXT NOT NULL REFERENCES node(id),
        hash TEXT NOT NULL UNIQUE,
        label TEXT NOT NULL,
        issued_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        revoked INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1)),
        revoked_at INTEGER
    )""",
    "CREATE INDEX operator_token_node ON operator_token(node_id)",
    """CREATE TABLE client (
        client_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('preregistered', 'cimd', 'dcr')),
        node_id TEXT NOT NULL REFERENCES node(id),
        metadata TEXT NOT NULL DEFAULT '{}',
        created_at INTEGER NOT NULL
    )""",
    """CREATE TABLE lineage (
        id TEXT PRIMARY KEY,
        role_id TEXT NOT NULL REFERENCES node(id),
        client_id TEXT NOT NULL REFERENCES client(client_id),
        resource TEXT NOT NULL,
        scope TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        revoked INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1)),
        revoked_at INTEGER
    )""",
    "CREATE INDEX lineage_role ON lineage(role_id)",
    """CREATE TABLE auth_code (
        hash TEXT PRIMARY KEY,
        lineage_id TEXT NOT NULL REFERENCES lineage(id),
        expires_at INTEGER NOT NULL,
        used INTEGER NOT NULL DEFAULT 0 CHECK (used IN (0, 1))
    )""",
    """CREATE TABLE refresh (
        hash TEXT PRIMARY KEY,
        lineage_id TEXT NOT NULL REFERENCES lineage(id),
        created_at INTEGER NOT NULL,
        used INTEGER NOT NULL DEFAULT 0 CHECK (used IN (0, 1))
    )""",
    """CREATE TABLE access_jti (
        jti TEXT PRIMARY KEY,
        lineage_id TEXT NOT NULL REFERENCES lineage(id),
        expires_at INTEGER NOT NULL,
        revoked INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1))
    )""",
    """CREATE TABLE audit (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        actor TEXT NOT NULL,
        node_id TEXT,
        path TEXT,
        action TEXT NOT NULL,
        result TEXT NOT NULL,
        reason TEXT,
        before TEXT,
        after TEXT,
        detail TEXT
    )""",
    # Append-only table (BR-066): no command modifies or deletes an entry.
    """CREATE TRIGGER audit_append_only_update BEFORE UPDATE ON audit
        BEGIN SELECT RAISE(ABORT, 'audit en ajout seul'); END""",
    """CREATE TRIGGER audit_append_only_delete BEFORE DELETE ON audit
        BEGIN SELECT RAISE(ABORT, 'audit en ajout seul'); END""",
    # Root organisation (one per instance, BR-044) and default setting values (BR-048).
    """INSERT INTO node (id, parent_id, level, slug, name, created_at)
        VALUES ('org', NULL, 'org', '', 'Organisation', CAST(strftime('%s', 'now') AS INTEGER))""",
    "INSERT INTO setting (node_id, key, value) VALUES ('org', 'duree_jetons', '30d')",
    "INSERT INTO setting (node_id, key, value) VALUES ('org', 'verrou', 'false')",
)

# Migration 2 (F-005, lot B): authorization server per access point, authorization barrier, DCR.
_M2 = (
    # What a code must bind besides its lineage (BR-057, BR-059): redirect and PKCE challenge.
    "ALTER TABLE auth_code ADD COLUMN redirect_uri TEXT",
    "ALTER TABLE auth_code ADD COLUMN code_challenge TEXT",
    "ALTER TABLE auth_code ADD COLUMN created_at INTEGER",
    # Clients (BR-061, S1, S9): public only (no secret); key (client_id, node_id), node_id
    # null for a CIMD client, admitted by its document and bound to the access point by its
    # lineage; a removed client is no longer admitted. The lineage no longer references
    # `client(client_id)`, which is no longer unique: tables rebuilt (SQLite procedure, foreign
    # keys checked at COMMIT).
    """CREATE TABLE client_v2 (
        client_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('preregistered', 'cimd', 'dcr')),
        node_id TEXT REFERENCES node(id),
        metadata TEXT NOT NULL DEFAULT '{}',
        created_at INTEGER NOT NULL,
        removed_at INTEGER,
        CHECK ((kind = 'cimd') = (node_id IS NULL))
    )""",
    """INSERT INTO client_v2 (client_id, kind, node_id, metadata, created_at)
        SELECT client_id, kind, CASE kind WHEN 'cimd' THEN NULL ELSE node_id END, metadata,
               created_at FROM client""",
    """CREATE TABLE lineage_v2 (
        id TEXT PRIMARY KEY,
        role_id TEXT NOT NULL REFERENCES node(id),
        client_id TEXT NOT NULL,
        resource TEXT NOT NULL,
        scope TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        revoked INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1)),
        revoked_at INTEGER
    )""",
    "INSERT INTO lineage_v2 SELECT * FROM lineage",
    "DROP TABLE lineage",
    "ALTER TABLE lineage_v2 RENAME TO lineage",
    "CREATE INDEX lineage_role ON lineage(role_id)",
    "DROP TABLE client",
    "ALTER TABLE client_v2 RENAME TO client",
    "CREATE UNIQUE INDEX client_on_node ON client(client_id, node_id) WHERE node_id IS NOT NULL",
    "CREATE UNIQUE INDEX client_cimd ON client(client_id) WHERE node_id IS NULL",
    # Authorization request awaiting the authorization barrier (BR-060): identified by the
    # fingerprint of a 192-bit value, 10 min, consumed by a success only.
    """CREATE TABLE consent_request (
        hash TEXT PRIMARY KEY,
        role_id TEXT NOT NULL REFERENCES node(id),
        client_id TEXT NOT NULL,
        client_kind TEXT NOT NULL,
        client_metadata TEXT NOT NULL DEFAULT '{}',
        redirect_uri TEXT NOT NULL,
        code_challenge TEXT NOT NULL,
        resource TEXT NOT NULL,
        scope TEXT NOT NULL,
        state TEXT,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL
    )""",
    # Authorization barrier failures (BR-073, global sliding window); the counter survives restarts.
    "CREATE TABLE consent_failure (seq INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER NOT NULL)",
    "CREATE INDEX consent_failure_at ON consent_failure(at)",
    # Accepted TOTP steps (BR-060, S17): a code is refused if its step is lower than or equal to
    # the last accepted step; kept in the database, it survives a restart.
    "CREATE TABLE totp_used (counter INTEGER PRIMARY KEY, used_at INTEGER NOT NULL)",
    # Instance settings outside the tree: dynamic registration (BR-061), off by default.
    "CREATE TABLE instance_flag (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "INSERT INTO instance_flag (key, value) VALUES ('dcr', 'off')",
    "CREATE INDEX lineage_client ON lineage(client_id)",
    "CREATE INDEX refresh_lineage ON refresh(lineage_id)",
    "CREATE INDEX access_jti_lineage ON access_jti(lineage_id)",
    "CREATE INDEX auth_code_lineage ON auth_code(lineage_id)",
)

# Migration 3 (F-006, lot A): connection library (§6.5). Upstream values are encrypted envelopes
# (BR-087); the key stays outside the database, only its key check is in it (BR-088). A removed
# connection keeps its row (id reserved, BR-075), with no token, no placement, no reference to the
# service or the application; its name and its prefix are freed.
_M3 = (
    """CREATE TABLE service (
        id TEXT PRIMARY KEY,
        model TEXT NOT NULL CHECK (model IN ('gitea', 'generique')),
        base_url TEXT NOT NULL,
        scopes TEXT NOT NULL DEFAULT '',
        internal_host INTEGER NOT NULL DEFAULT 0 CHECK (internal_host IN (0, 1)),
        endpoints TEXT NOT NULL DEFAULT '{}',
        resource TEXT,
        created_at INTEGER NOT NULL
    )""",
    """CREATE TABLE application (
        id TEXT PRIMARY KEY,
        service_id TEXT NOT NULL REFERENCES service(id),
        mode TEXT NOT NULL CHECK (mode IN ('declaree', 'dynamique')),
        client_id TEXT NOT NULL,
        redirect_uri TEXT NOT NULL,
        secret_enc BLOB,
        secret_expires_at INTEGER,
        created_at INTEGER NOT NULL
    )""",
    "CREATE INDEX application_service ON application(service_id)",
    """CREATE TABLE vault_meta (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        envelope_version INTEGER NOT NULL,
        key_check BLOB NOT NULL
    )""",
    """CREATE TABLE connection (
        id TEXT PRIMARY KEY,
        client_node_id TEXT NOT NULL REFERENCES node(id),
        name TEXT NOT NULL,
        prefix TEXT NOT NULL,
        service_id TEXT REFERENCES service(id),
        application_id TEXT REFERENCES application(id),
        kind TEXT NOT NULL CHECK (kind IN ('oauth', 'fourni')),
        account TEXT,
        state TEXT NOT NULL CHECK (state IN ('en_attente', 'active', 'reauth_requise')),
        granted_scopes TEXT NOT NULL DEFAULT '',
        expires_at INTEGER,
        expiry_estimated INTEGER NOT NULL DEFAULT 0 CHECK (expiry_estimated IN (0, 1)),
        not_before INTEGER,
        fail_count INTEGER NOT NULL DEFAULT 0,
        renewing_until INTEGER,
        renewal_owner TEXT,
        generation INTEGER NOT NULL DEFAULT 0,
        last_outcome TEXT,
        tokens_enc BLOB,
        removed INTEGER NOT NULL DEFAULT 0 CHECK (removed IN (0, 1)),
        created_at INTEGER NOT NULL,
        removed_at INTEGER,
        CHECK (removed = 1 OR service_id IS NOT NULL),
        CHECK (removed = 1 OR (kind = 'fourni') = (application_id IS NULL))
    )""",
    "CREATE UNIQUE INDEX connection_name ON connection(client_node_id, name) WHERE removed = 0",
    "CREATE UNIQUE INDEX connection_prefix ON connection(prefix) WHERE removed = 0",
    "CREATE INDEX connection_service ON connection(service_id)",
    "CREATE INDEX connection_application ON connection(application_id)",
    """CREATE TABLE placement (
        connection_id TEXT NOT NULL REFERENCES connection(id),
        role_id TEXT NOT NULL REFERENCES node(id),
        created_at INTEGER NOT NULL,
        PRIMARY KEY (connection_id, role_id)
    )""",
    "CREATE INDEX placement_role ON placement(role_id)",
    # Open outbound authorization (BR-082, lot B): at most one per connection.
    """CREATE TABLE pending_auth (
        connection_id TEXT PRIMARY KEY REFERENCES connection(id),
        state_hash TEXT NOT NULL,
        verifier_enc BLOB NOT NULL,
        expires_at INTEGER NOT NULL,
        used INTEGER NOT NULL DEFAULT 0 CHECK (used IN (0, 1))
    )""",
)

# F-006 lot B: authentication method of an application at the token endpoint (BR-083), reason of
# a connection's last outcome (BR-092: `etat` names the application for `invalid_client`), and a
# marker per connection of the last audited key-unavailable reason (deduplication, review 417 m2):
# outside the `connection` row, which BR-088 forbids modifying without a key.
_M4 = (
    "ALTER TABLE application ADD COLUMN auth_method TEXT NOT NULL DEFAULT 'none'"
    " CHECK (auth_method IN ('none', 'client_secret_basic', 'client_secret_post'))",
    "UPDATE application SET auth_method = 'client_secret_basic' WHERE secret_enc IS NOT NULL",
    "ALTER TABLE connection ADD COLUMN last_reason TEXT",
    """CREATE TABLE key_alert (
        connection_id TEXT PRIMARY KEY REFERENCES connection(id),
        reason TEXT NOT NULL,
        at INTEGER NOT NULL
    )""",
)

# Migration 5 (F-008 lot A; the spec announced "4", a number already taken by F-006 lot B): MCP
# upstream of a service (BR-106), NULL for a service declared before F-008, and the table of the
# public tool names (BR-110), unique per (connection, upstream name) and per (connection, public
# name), without organisation (HULL-42). No secret.
_M5 = (
    "ALTER TABLE service ADD COLUMN upstream_type TEXT"
    " CHECK (upstream_type IS NULL OR upstream_type IN ('mcp-distant', 'mcp-local'))",
    "ALTER TABLE service ADD COLUMN mcp_url TEXT",
    "ALTER TABLE service ADD COLUMN mcp_internal INTEGER NOT NULL DEFAULT 0"
    " CHECK (mcp_internal IN (0, 1))",
    """CREATE TABLE tool_name (
        connection_id TEXT NOT NULL REFERENCES connection(id),
        upstream_name TEXT NOT NULL,
        public_name TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        PRIMARY KEY (connection_id, upstream_name)
    )""",
    "CREATE UNIQUE INDEX tool_name_public ON tool_name(connection_id, public_name)",
)

# Migration 6 (F-010, BR-132): vocabulary of the stored values moved to English. `service`,
# `application` and `connection` are rebuilt (SQLite does not modify a CHECK), with their columns
# in the same order (those of migrations 4 and 5 included), their constraints and their indexes;
# the child tables reference the final names. A value outside the mapping table stops the
# migration: a guard table, created and dropped in the same transaction, receives a row whose
# every column carries a named CHECK (`vocab_guard_<table>_<column>`); the violation raises
# `IntegrityError`, the engine rolls back and returns `schema_error` naming the guard, and the
# database stays at version 5. Static statements only. Guard tables of the main schema (not TEMP:
# the container's file system is read-only, BR-031, and a temporary table could need a file
# outside /data). No encrypted column, no identifier and no row count is touched; the audit rows
# stay as they are (BR-066 v4) and a marker row is added when the audit is not empty
# (F-010 §3.4).
_M6 = (
    # Input guards: each column carries only values the mapping knows.
    """CREATE TABLE _vocab_guard (
        connection_kind INTEGER CONSTRAINT vocab_guard_connection_kind
            CHECK (connection_kind = 1),
        connection_state INTEGER CONSTRAINT vocab_guard_connection_state
            CHECK (connection_state = 1),
        connection_last_outcome INTEGER CONSTRAINT vocab_guard_connection_last_outcome
            CHECK (connection_last_outcome = 1),
        service_model INTEGER CONSTRAINT vocab_guard_service_model CHECK (service_model = 1),
        service_upstream_type INTEGER CONSTRAINT vocab_guard_service_upstream_type
            CHECK (service_upstream_type = 1),
        application_mode INTEGER CONSTRAINT vocab_guard_application_mode
            CHECK (application_mode = 1),
        application_redirect_uri INTEGER CONSTRAINT vocab_guard_application_redirect_uri
            CHECK (application_redirect_uri = 1),
        setting_key INTEGER CONSTRAINT vocab_guard_setting_key CHECK (setting_key = 1),
        key_alert_reason INTEGER CONSTRAINT vocab_guard_key_alert_reason
            CHECK (key_alert_reason = 1)
    )""",
    """INSERT INTO _vocab_guard SELECT
        NOT EXISTS (SELECT 1 FROM connection WHERE kind NOT IN ('oauth', 'fourni')),
        NOT EXISTS (SELECT 1 FROM connection
            WHERE state NOT IN ('en_attente', 'active', 'reauth_requise')),
        NOT EXISTS (SELECT 1 FROM connection WHERE last_outcome IS NOT NULL AND last_outcome
            NOT IN ('rendu', 'definitive', 'incertaine', 'transitoire', 'anomalie')),
        NOT EXISTS (SELECT 1 FROM service WHERE model NOT IN ('gitea', 'generique')),
        NOT EXISTS (SELECT 1 FROM service WHERE upstream_type IS NOT NULL
            AND upstream_type NOT IN ('mcp-distant', 'mcp-local')),
        NOT EXISTS (SELECT 1 FROM application WHERE mode NOT IN ('declaree', 'dynamique')),
        NOT EXISTS (SELECT 1 FROM application
            WHERE redirect_uri <> 'http://127.0.0.1/oauth/retour'),
        NOT EXISTS (SELECT 1 FROM setting WHERE key NOT IN ('duree_jetons', 'verrou')),
        NOT EXISTS (SELECT 1 FROM key_alert
            WHERE reason NOT IN ('clé absente', 'clé invalide', 'clé différente'))""",
    # Cutover marker (F-010 §3.4, BR-066 v4), counted before conversion, only if the audit is not
    # empty; the existing rows are never modified.
    """INSERT INTO audit (at, actor, action, result, detail)
        SELECT strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), 'migration',
            'instance.vocabulary_migrated', 'ok',
            json_object(
                'converted', json_object(
                    'application.mode', (SELECT count(*) FROM application
                        WHERE mode IN ('declaree', 'dynamique')),
                    'application.redirect_uri', (SELECT count(*) FROM application
                        WHERE redirect_uri = 'http://127.0.0.1/oauth/retour'),
                    'connection.kind', (SELECT count(*) FROM connection WHERE kind = 'fourni'),
                    'connection.last_outcome', (SELECT count(*) FROM connection
                        WHERE last_outcome IS NOT NULL),
                    'connection.last_reason', (SELECT count(*) FROM connection
                        WHERE last_reason IN (
            'sans refresh_token',
            'bail échu sans résultat',
            'écriture impossible avant la fin du bail',
            'révoquée',
            'application réenregistrée',
            'refus signalé : 401 du serveur MCP',
            'refus amont signalé',
            'échéance trop courte'
        )),
                    'connection.state', (SELECT count(*) FROM connection
                        WHERE state IN ('en_attente', 'reauth_requise')),
                    'key_alert.reason', (SELECT count(*) FROM key_alert),
                    'service.model', (SELECT count(*) FROM service WHERE model = 'generique'),
                    'service.upstream_type', (SELECT count(*) FROM service
                        WHERE upstream_type IS NOT NULL),
                    'setting.key', (SELECT count(*) FROM setting
                        WHERE key IN ('duree_jetons', 'verrou'))
                ),
                'from', 5,
                'to', 6
            )
        WHERE EXISTS (SELECT 1 FROM audit)""",
    """CREATE TABLE service_v6 (
        id TEXT PRIMARY KEY,
        model TEXT NOT NULL CHECK (model IN ('gitea', 'generic')),
        base_url TEXT NOT NULL,
        scopes TEXT NOT NULL DEFAULT '',
        internal_host INTEGER NOT NULL DEFAULT 0 CHECK (internal_host IN (0, 1)),
        endpoints TEXT NOT NULL DEFAULT '{}',
        resource TEXT,
        created_at INTEGER NOT NULL
    , upstream_type TEXT CHECK (upstream_type IS NULL OR upstream_type IN ('mcp-public',"""
    """ 'mcp-internal')), mcp_url TEXT, mcp_internal INTEGER NOT NULL DEFAULT 0"""
    """ CHECK (mcp_internal IN (0, 1)))""",
    """INSERT INTO service_v6 (rowid, id, model, base_url, scopes, internal_host, endpoints,
            resource, created_at, upstream_type, mcp_url, mcp_internal)
        SELECT rowid, id, CASE model WHEN 'generique' THEN 'generic' ELSE model END, base_url,
            scopes, internal_host, endpoints, resource, created_at,
            CASE upstream_type WHEN 'mcp-distant' THEN 'mcp-public'
                WHEN 'mcp-local' THEN 'mcp-internal' ELSE upstream_type END,
            mcp_url, mcp_internal
        FROM service""",
    "DROP TABLE service",
    "ALTER TABLE service_v6 RENAME TO service",
    """CREATE TABLE application_v6 (
        id TEXT PRIMARY KEY,
        service_id TEXT NOT NULL REFERENCES service(id),
        mode TEXT NOT NULL CHECK (mode IN ('declared', 'dynamic')),
        client_id TEXT NOT NULL,
        redirect_uri TEXT NOT NULL,
        secret_enc BLOB,
        secret_expires_at INTEGER,
        created_at INTEGER NOT NULL
    , auth_method TEXT NOT NULL DEFAULT 'none' CHECK (auth_method IN ('none',"""
    """ 'client_secret_basic', 'client_secret_post')))""",
    """INSERT INTO application_v6 (rowid, id, service_id, mode, client_id, redirect_uri,
            secret_enc, secret_expires_at, created_at, auth_method)
        SELECT rowid, id, service_id,
            CASE mode WHEN 'declaree' THEN 'declared' WHEN 'dynamique' THEN 'dynamic'
                ELSE mode END,
            client_id,
            CASE redirect_uri WHEN 'http://127.0.0.1/oauth/retour'
                THEN 'http://127.0.0.1/oauth/callback' ELSE redirect_uri END,
            secret_enc, secret_expires_at, created_at, auth_method
        FROM application""",
    "DROP TABLE application",
    "ALTER TABLE application_v6 RENAME TO application",
    "CREATE INDEX application_service ON application(service_id)",
    """CREATE TABLE connection_v6 (
        id TEXT PRIMARY KEY,
        client_node_id TEXT NOT NULL REFERENCES node(id),
        name TEXT NOT NULL,
        prefix TEXT NOT NULL,
        service_id TEXT REFERENCES service(id),
        application_id TEXT REFERENCES application(id),
        kind TEXT NOT NULL CHECK (kind IN ('oauth', 'provided')),
        account TEXT,
        state TEXT NOT NULL CHECK (state IN ('pending', 'active', 'reauth_required')),
        granted_scopes TEXT NOT NULL DEFAULT '',
        expires_at INTEGER,
        expiry_estimated INTEGER NOT NULL DEFAULT 0 CHECK (expiry_estimated IN (0, 1)),
        not_before INTEGER,
        fail_count INTEGER NOT NULL DEFAULT 0,
        renewing_until INTEGER,
        renewal_owner TEXT,
        generation INTEGER NOT NULL DEFAULT 0,
        last_outcome TEXT CHECK (last_outcome IS NULL OR last_outcome IN ('renewed', 'permanent',
            'uncertain', 'transient', 'anomaly')),
        tokens_enc BLOB,
        removed INTEGER NOT NULL DEFAULT 0 CHECK (removed IN (0, 1)),
        created_at INTEGER NOT NULL,
        removed_at INTEGER, last_reason TEXT,
        CHECK (removed = 1 OR service_id IS NOT NULL),
        CHECK (removed = 1 OR (kind = 'provided') = (application_id IS NULL))
    )""",
    """INSERT INTO connection_v6 (rowid, id, client_node_id, name, prefix, service_id,
            application_id, kind, account, state, granted_scopes, expires_at, expiry_estimated,
            not_before, fail_count, renewing_until, renewal_owner, generation, last_outcome,
            tokens_enc, removed, created_at, removed_at, last_reason)
        SELECT rowid, id, client_node_id, name, prefix, service_id, application_id,
            CASE kind WHEN 'fourni' THEN 'provided' ELSE kind END,
            account,
            CASE state WHEN 'en_attente' THEN 'pending' WHEN 'reauth_requise'
                THEN 'reauth_required' ELSE state END,
            granted_scopes, expires_at, expiry_estimated, not_before, fail_count,
            renewing_until, renewal_owner, generation,
            CASE last_outcome WHEN 'rendu' THEN 'renewed' WHEN 'definitive' THEN 'permanent'
                WHEN 'incertaine' THEN 'uncertain' WHEN 'transitoire' THEN 'transient'
                WHEN 'anomalie' THEN 'anomaly' ELSE last_outcome END,
            tokens_enc, removed, created_at, removed_at,
            CASE last_reason
                WHEN 'sans refresh_token' THEN 'no_refresh_token'
                WHEN 'bail échu sans résultat' THEN 'lease_expired'
                WHEN 'écriture impossible avant la fin du bail' THEN 'lease_write_failed'
                WHEN 'révoquée' THEN 'revoked'
                WHEN 'application réenregistrée' THEN 'application_reregistered'
                WHEN 'refus signalé : 401 du serveur MCP' THEN 'mcp_refusal_401'
                WHEN 'refus amont signalé' THEN 'upstream_refusal'
                WHEN 'échéance trop courte' THEN 'expiry_too_short'
                ELSE last_reason END
        FROM connection""",
    "DROP TABLE connection",
    "ALTER TABLE connection_v6 RENAME TO connection",
    "CREATE UNIQUE INDEX connection_name ON connection(client_node_id, name) WHERE removed = 0",
    "CREATE UNIQUE INDEX connection_prefix ON connection(prefix) WHERE removed = 0",
    "CREATE INDEX connection_service ON connection(service_id)",
    "CREATE INDEX connection_application ON connection(application_id)",
    """UPDATE setting SET key = CASE key WHEN 'duree_jetons' THEN 'token_duration'
        WHEN 'verrou' THEN 'lock' ELSE key END
        WHERE key IN ('duree_jetons', 'verrou')""",
    """UPDATE key_alert SET reason = CASE reason WHEN 'clé absente' THEN 'key_absent'
        WHEN 'clé invalide' THEN 'key_invalid' WHEN 'clé différente' THEN 'key_different'
        ELSE reason END""",
    # Output guards (F-010 §3.3): only English values remain in the columns without a permanent
    # CHECK; `last_reason` is guarded on the eight sentinels only.
    """CREATE TABLE _vocab_check (
        application_redirect_uri INTEGER CONSTRAINT vocab_guard_application_redirect_uri_out
            CHECK (application_redirect_uri = 1),
        setting_key INTEGER CONSTRAINT vocab_guard_setting_key_out CHECK (setting_key = 1),
        key_alert_reason INTEGER CONSTRAINT vocab_guard_key_alert_reason_out
            CHECK (key_alert_reason = 1),
        connection_last_reason INTEGER CONSTRAINT vocab_guard_connection_last_reason_out
            CHECK (connection_last_reason = 1)
    )""",
    """INSERT INTO _vocab_check SELECT
        NOT EXISTS (SELECT 1 FROM application
            WHERE redirect_uri <> 'http://127.0.0.1/oauth/callback'),
        NOT EXISTS (SELECT 1 FROM setting WHERE key NOT IN ('token_duration', 'lock')),
        NOT EXISTS (SELECT 1 FROM key_alert
            WHERE reason NOT IN ('key_absent', 'key_invalid', 'key_different')),
        NOT EXISTS (SELECT 1 FROM connection WHERE last_reason IN (
            'sans refresh_token',
            'bail échu sans résultat',
            'écriture impossible avant la fin du bail',
            'révoquée',
            'application réenregistrée',
            'refus signalé : 401 du serveur MCP',
            'refus amont signalé',
            'échéance trop courte'
        ))""",
    "DROP TABLE _vocab_check",
    "DROP TABLE _vocab_guard",
)

# Migration 7 (F-015 lot A; the spec announced "8" behind F-014's 7, which is not on the base
# branch: next free number, workflow §Rules): action policy. Additive. `placement` gains the
# mode override and the pin of a placement, NULL for "inherit". The tool catalogue is per
# (service, upstream name): manual class, class the last listing's annotations gave (NULL: no
# usable annotation), queue state, `removed` when the last listing no longer announced it. The
# lists, argument rules and pinned definitions hang off a placement and go with it. The three
# settings get their organisation defaults (BR-048). No secret, no argument value.
_M7 = (
    "ALTER TABLE placement ADD COLUMN mode TEXT"
    " CHECK (mode IS NULL OR mode IN ('read', 'write', 'destructive'))",
    "ALTER TABLE placement ADD COLUMN pin TEXT CHECK (pin IS NULL OR pin IN ('on', 'off'))",
    """CREATE TABLE tool_catalog (
        service_id TEXT NOT NULL REFERENCES service(id),
        upstream_name TEXT NOT NULL,
        manual_class TEXT CHECK (manual_class IS NULL
            OR manual_class IN ('read', 'write', 'destructive')),
        annotation_class TEXT CHECK (annotation_class IS NULL
            OR annotation_class IN ('read', 'write', 'destructive')),
        queued INTEGER NOT NULL DEFAULT 1 CHECK (queued IN (0, 1)),
        removed INTEGER NOT NULL DEFAULT 0 CHECK (removed IN (0, 1)),
        first_seen INTEGER NOT NULL,
        PRIMARY KEY (service_id, upstream_name)
    )""",
    """CREATE TABLE placement_tool (
        connection_id TEXT NOT NULL,
        role_id TEXT NOT NULL,
        list TEXT NOT NULL CHECK (list IN ('open', 'deny', 'allowlist')),
        upstream_name TEXT NOT NULL,
        PRIMARY KEY (connection_id, role_id, list, upstream_name),
        FOREIGN KEY (connection_id, role_id) REFERENCES placement(connection_id, role_id)
    )""",
    """CREATE TABLE argument_rule (
        connection_id TEXT NOT NULL,
        role_id TEXT NOT NULL,
        upstream_name TEXT NOT NULL,
        argument TEXT NOT NULL,
        allowed_values TEXT NOT NULL,
        PRIMARY KEY (connection_id, role_id, upstream_name),
        FOREIGN KEY (connection_id, role_id) REFERENCES placement(connection_id, role_id)
    )""",
    """CREATE TABLE pinned_definition (
        connection_id TEXT NOT NULL,
        role_id TEXT NOT NULL,
        upstream_name TEXT NOT NULL,
        tool_pin INTEGER NOT NULL DEFAULT 0 CHECK (tool_pin IN (0, 1)),
        sha256 TEXT,
        definition TEXT,
        drift_sha256 TEXT,
        drift_definition TEXT,
        suspended_at INTEGER,
        PRIMARY KEY (connection_id, role_id, upstream_name),
        FOREIGN KEY (connection_id, role_id) REFERENCES placement(connection_id, role_id)
    )""",
    "INSERT INTO setting (node_id, key, value) VALUES ('org', 'mode', 'write')",
    "INSERT INTO setting (node_id, key, value) VALUES ('org', 'pinning', 'auto')",
    "INSERT INTO setting (node_id, key, value) VALUES ('org', 'response_cap', '1MiB')",
)

# Migration 8 (F-016 lot A, §6.5): the instance's administrators, their passkeys, sessions and
# enrolment links, WebAuthn challenges, sign-in attempts and the console's IP allow-list. Only
# digests of session values and links are stored (BR-203, BR-204); public keys only for passkeys.
_M8 = (
    """CREATE TABLE administrator (
        id TEXT PRIMARY KEY,
        email TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('pending', 'active')),
        created_at INTEGER NOT NULL
    )""",
    """CREATE TABLE passkey (
        credential_id TEXT PRIMARY KEY,
        administrator_id TEXT NOT NULL REFERENCES administrator(id),
        public_key BLOB NOT NULL,
        sign_count INTEGER NOT NULL DEFAULT 0,
        label TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        last_used_at INTEGER
    )""",
    "CREATE INDEX passkey_administrator ON passkey(administrator_id)",
    """CREATE TABLE admin_session (
        digest TEXT PRIMARY KEY,
        administrator_id TEXT NOT NULL REFERENCES administrator(id),
        csrf_digest TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        last_activity_at INTEGER NOT NULL,
        fresh_until INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX admin_session_administrator ON admin_session(administrator_id)",
    """CREATE TABLE enrolment (
        digest TEXT PRIMARY KEY,
        administrator_id TEXT NOT NULL REFERENCES administrator(id),
        kind TEXT NOT NULL CHECK (kind IN ('bootstrap', 'recovery')),
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        used_at INTEGER
    )""",
    "CREATE INDEX enrolment_administrator ON enrolment(administrator_id)",
    """CREATE TABLE webauthn_challenge (
        challenge TEXT PRIMARY KEY,
        purpose TEXT NOT NULL CHECK (purpose IN ('sign_in', 'enrol', 'fresh', 'passkey')),
        administrator_id TEXT REFERENCES administrator(id),
        enrolment_digest TEXT,
        address TEXT NOT NULL,
        expires_at INTEGER NOT NULL
    )""",
    "CREATE INDEX webauthn_challenge_address ON webauthn_challenge(address)",
    """CREATE TABLE admin_attempt (
        id INTEGER PRIMARY KEY,
        address TEXT NOT NULL,
        at INTEGER NOT NULL
    )""",
    "CREATE INDEX admin_attempt_address ON admin_attempt(address, at)",
    "CREATE TABLE admin_allowlist (cidr TEXT PRIMARY KEY)",
)

# Migration 9 (F-016 slice 2, §6.5): an application's "web return registered" flag (BR-216), the
# `redirect_uri` chosen when an authorisation opens, sent again at the exchange (BR-082 v3), and
# the share confirmation kept on the placement, by whom and when (BR-078 v3).
_M9 = (
    "ALTER TABLE application ADD COLUMN web_return INTEGER NOT NULL DEFAULT 0"
    " CHECK (web_return IN (0, 1))",
    "ALTER TABLE pending_auth ADD COLUMN redirect_uri TEXT",
    "ALTER TABLE placement ADD COLUMN share_confirmed_by TEXT",
    "ALTER TABLE placement ADD COLUMN share_confirmed_at INTEGER",
)

# Migration 10 (F-014; the spec announced "7", taken by F-015, then 8 and 9 by F-016): the
# organisation's public identifier (BR-160), 12 characters of the lowercase RFC 4648 base32
# alphabet, drawn once here on any database, new or existing. Each character takes 5 uniform bits
# of its own SQLite `random()` (`& 31`; no `abs()`, which overflows on the minimum value). The
# store holds the rule: format and organisation-only placement by `CHECK`, uniqueness by a partial
# index, permanence by a trigger refusing any change of a value once set, while the row's other
# columns stay editable.
PUBLIC_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
PUBLIC_ID_LENGTH = 12
_DRAW = " || ".join([f"substr('{PUBLIC_ID_ALPHABET}', (random() & 31) + 1, 1)"] * PUBLIC_ID_LENGTH)
_M10 = (
    "ALTER TABLE node ADD COLUMN public_id TEXT CHECK (public_id IS NULL OR (level = 'org'"
    f" AND length(public_id) = {PUBLIC_ID_LENGTH} AND public_id NOT GLOB '*[^a-z2-7]*'))",
    "CREATE UNIQUE INDEX node_public_id ON node(public_id) WHERE public_id IS NOT NULL",
    """CREATE TRIGGER node_public_id_permanent BEFORE UPDATE OF public_id ON node
        WHEN OLD.public_id IS NOT NULL AND NEW.public_id IS NOT OLD.public_id
        BEGIN SELECT RAISE(ABORT, 'public_id is permanent'); END""",
    f"UPDATE node SET public_id = {_DRAW} WHERE level = 'org' AND public_id IS NULL",  # noqa: S608
)

# Migration 11 (F-020, §9 Data Model Impact): a pending request approved from the host (BR-310)
# and the two tool-call limits seeded at the organisation (BR-303, BR-304, BR-048).
_M11 = (
    "ALTER TABLE consent_request ADD COLUMN approved_at INTEGER",
    "INSERT INTO setting (node_id, key, value) VALUES ('org', 'token_rate_limit', '120/min')",
    "INSERT INTO setting (node_id, key, value) VALUES ('org', 'connection_rate_limit', '300/min')",
)

# Slice 4: global expiry warning, setup dismissal, and barrier-only challenges.
_M12 = (
    "ALTER TABLE connection ADD COLUMN refresh_expires_at INTEGER",
    "INSERT INTO setting (node_id, key, value) VALUES ('org', 'connection_expiry_days', '7')",
    "CREATE TABLE admin_setup (id INTEGER PRIMARY KEY CHECK (id = 1), dismissed INTEGER NOT NULL)",
    "INSERT INTO admin_setup VALUES (1, 0)",
    "ALTER TABLE webauthn_challenge RENAME TO old_webauthn_challenge",
    "CREATE TABLE webauthn_challenge (challenge TEXT PRIMARY KEY, purpose TEXT NOT NULL"
    " CHECK (purpose IN ('sign_in', 'enrol', 'fresh', 'passkey', 'barrier')),"
    " administrator_id TEXT REFERENCES administrator(id), enrolment_digest TEXT,"
    " address TEXT NOT NULL, expires_at INTEGER NOT NULL)",
    "INSERT INTO webauthn_challenge SELECT * FROM old_webauthn_challenge",
    "DROP TABLE old_webauthn_challenge",
    "CREATE INDEX webauthn_challenge_address ON webauthn_challenge(address)",
)

MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, _M1),
    Migration(2, _M2, rebuild=True),
    Migration(3, _M3),
    Migration(4, _M4),
    Migration(5, _M5),
    Migration(6, _M6, rebuild=True),
    Migration(7, _M7),
    Migration(8, _M8),
    Migration(9, _M9),
    Migration(10, _M10),
    Migration(11, _M11),
    Migration(12, _M12),
)
SCHEMA_VERSION = MIGRATIONS[-1].version


class SchemaError(Exception):
    """Failed migration (`schema_error`) or database newer than the code (`schema_too_new`)."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason


def user_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def migrate(conn: sqlite3.Connection, migrations: Sequence[Migration] = MIGRATIONS) -> int:
    """Applies, in order, the migrations beyond the database's version; returns the version.

    `conn` is in autocommit mode (`isolation_level=None`). A failed migration is rolled back
    whole and leaves the database at the last successful version (`SchemaError("schema_error")`);
    a database newer than the code is not touched (`SchemaError("schema_too_new")`).
    """
    target = migrations[-1].version if migrations else 0
    current = user_version(conn)
    if current > target:
        raise SchemaError(
            "schema_too_new", f"database at version {current}, code at version {target}"
        )
    for migration in migrations:
        if migration.version <= current:
            continue
        if migration.rebuild:
            conn.execute(
                "PRAGMA foreign_keys = OFF"
            )  # outside the transaction: no effect inside one
        try:
            conn.execute("BEGIN IMMEDIATE")
            # Read again under the lock: a concurrent open may have migrated in the meantime.
            current = user_version(conn)
            if current > target:
                conn.execute("ROLLBACK")
                raise SchemaError(
                    "schema_too_new", f"database at version {current}, code at version {target}"
                )
            if migration.version <= current:
                conn.execute("ROLLBACK")
                continue
            for statement in migration.statements:
                conn.execute(statement)
            if migration.rebuild and conn.execute("PRAGMA foreign_key_check").fetchone():
                raise sqlite3.IntegrityError("orphan foreign key after rebuild")
            conn.execute(f"PRAGMA user_version = {int(migration.version)}")
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise SchemaError(
                "schema_error", f"migration {migration.version} failed ({exc})"
            ) from exc
        finally:
            if migration.rebuild:
                conn.execute("PRAGMA foreign_keys = ON")
        current = migration.version
    return current
