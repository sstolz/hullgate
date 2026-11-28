"""BR-131 (F-010 §3.2, §3.5; AC-03, AC-06 to AC-08, AC-14 to AC-17, AC-23, AC-24): the gateway's
closed vocabularies and its product name are English.

Each family is read by its extractor and compared with a table pinned here as literals (F-010 §3.2,
the operator's naming table), with one mutant per key compared: the CLI by a walk of
`cli._parser()`, the JSON and YAML shapes by the real output of the commands, the CHECK lists by
`sqlite_master` of a fresh migrated database, the runbooks and the bench by parsing their CLI
calls, the Makefile targets by `make -pn`, the compose files and `env.example` by their keys. The
sweeps of §3.5 (A, B1, B2, C, D, E, F) and sweep H of F-011 §3.4 (the names bound to data) return
no line outside the pinned exclusions, and each has a positive fixture, so a mistyped pattern cannot
pass by matching nothing. Sweep G (the former product name) left for the former-name guard of
HULL-58 (`test_former_names.py`), with its kept tokens.
"""

from __future__ import annotations

import argparse
import ast
import copy
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from hullgate import cli, config, export, migrations, tree
from tests.support import Gateway, standard_tree
from tests.unit.cli_output_tour import run_tour

ROOT = Path(__file__).resolve().parents[2]

# -- the CLI surface (F-010 §3.2), pinned ---------------------------------------------------------

J: dict[str, None] = {"--json": None}
PINNED_CLI: dict[str, dict[str, dict[str, list[str] | None]]] = {
    "tree": {
        "add": {"parent": None, "level": None, "name": None, "--slug": None},
        "rename": {"path": None, "name": None, "--slug": None},
        "move": {"path": None, "new_parent": None, "--confirm": None},
        "delete": {"path": None, "--confirm": None},
        "show": {"path": None, **J},
        "sensitive": {"path": None, "state": ["off", "on"]},
        "lock": {"path": None},
        "unlock": {"path": None},
    },
    "settings": {
        "get": {"path": None, **J},
        "set": {"path": None, "key": None, "value": None},
        "unset": {"path": None, "key": None},
    },
    "token": {
        "create": {"--path": None, "--label": None, "--duration": None, "--confirm": None, **J},
        "list": {"path": None, "--client": None, **J},
        "revoke": {"id": None},
    },
    "audit": {"tail": {"-n": None, **J}},
    "client": {
        "add": {"client_id": None, "--path": None, "--redirect-uri": None, "--name": None},
        "list": {**J},
        "remove": {"client_id": None},
        "dcr": {"state": ["off", "on"]},
    },
    "export": {},
    "import": {},
    "undo": {},
    "service": {
        "declare": {
            "id": None, "--server": None, "--model": ["generic", "gitea"], "--scopes": None,
            "--internal-host": None,
        },
        "list": {**J},
        "remove": {"id": None},
        "rediscover": {"id": None, "--confirm": None},
        "upstream": {
            "id": None, "--type": ["mcp-internal", "mcp-public"], "--mcp-url": None,
            "--mcp-internal-host": None, "--remove": None, "--confirm": None,
        },
    },
    "application": {
        "declare": {
            "id": None, "--service": None, "--mode": ["declared", "dynamic"], "--client-id": None,
            "--secret-file": None,
        },
        "list": {**J},
        "remove": {"id": None},
        "reregister": {"id": None, "--confirm": None},
    },
    "connection": {
        "add": {
            "name": None, "--service": None, "--application": None, "--provided": None,
            "--file": None, "--prefix": None, "--account": None, "--expires-at": None,
        },
        "authorize": {"name": None, "--callback": None, "--confirm-account": None},
        "reauthorize": {"name": None, "--file": None, "--account": None, "--expires-at": None},
        "renew": {"name": None},
        "revoke": {"name": None},
        "prefix": {"name": None, "prefix": None},
        "place": {"name": None, "role": None, "--confirm-share": None},
        "unplace": {"name": None, "role": None},
        "status": {"name": None, **J},
        "list": {**J},
        "remove": {"name": None, "--confirm": None},
        "tools": {
            "name": None, "--role": None, "--purge-names": None, "--confirm": None, **J,
        },
    },
    # F-015 (SCR-01, SCR-02): lot A
    "policy": {
        "show": {"name": None, "--role": None, **J},
        "mode": {"name": None, "--role": None, "mode": ["destructive", "inherit", "read", "write"]},
        "add": {
            "name": None, "--role": None, "list": ["allowlist", "deny", "open"], "tools": None,
            "--class": ["read"],
        },
        "remove": {
            "name": None, "--role": None, "list": ["allowlist", "deny", "open"], "tools": None,
        },
        "arg": {"name": None, "--role": None, "tool": None, "argument": None, "values": None},
        "arg-remove": {"name": None, "--role": None, "tool": None},
        "pin": {"name": None, "--role": None, "pin": ["inherit", "off", "on"]},  # lot B
        "pin-tool": {"name": None, "--role": None, "tool": None},
        "unpin-tool": {"name": None, "--role": None, "tool": None},
        "preview": {"role": None, **J},
    },
    "admin": {  # F-016 lot A, host only (BR-211's exclusions)
        "bootstrap": {"email": None, "--name": None},
        "recover": {"email": None},
        "list": {**J},
        "attempts": {"action": ["clear"]},
        "allowlist": {"action": ["clear"]},
    },
    "consent": {"list": {**J}, "approve": {"reference": None}},  # F-020, host only (BR-310)
    "tool": {
        "queue": {"--service": None, **J},
        "classify": {
            "service": None, "tool": None, "cls": ["clear", "destructive", "read", "write"],
        },
        "accept": {"service": None, "tool": None},
        "diff": {"path": None, **J},  # lot B
        "validate": {"path": None, "name": None, "--tool": None},
    },
}  # fmt: skip
PINNED_LEVELS = {"client", "project", "environment", "env", "role"}
PINNED_SETTINGS = {
    "connection_expiry_days",
    "token_duration", "lock", "mode", "pinning", "response_cap",
    "token_rate_limit", "connection_rate_limit",  # F-020
}  # fmt: skip

# F-010 §3.2, French column: none of these may appear in the parser.
OLD_GROUPS = ["jeton", "connexion"]
OLD_VERBS = [  # (new group, old verb, valid remaining arguments)
    ("token", "creer", []), ("token", "lister", []), ("token", "revoquer", ["x"]),
    ("service", "declarer", ["s"]), ("service", "lister", []), ("service", "retirer", ["s"]),
    ("service", "redecouvrir", ["s"]), ("service", "amont", ["s"]),
    ("application", "declarer", ["a"]), ("application", "lister", []),
    ("application", "retirer", ["a"]), ("application", "reenregistrer", ["a"]),
    ("connection", "ajouter", ["c/n"]), ("connection", "autoriser", ["c/n"]),
    ("connection", "reautoriser", ["c/n"]), ("connection", "renouveler", ["c/n"]),
    ("connection", "revoquer", ["c/n"]), ("connection", "prefixe", ["c/n", "px"]),
    ("connection", "placer", ["c/n", "a/b/c/d"]), ("connection", "retirer-placement", ["c/n", "r"]),
    ("connection", "etat", []), ("connection", "lister", []), ("connection", "retirer", ["c/n"]),
    ("connection", "outils", ["c/n"]),
]  # fmt: skip
OLD_FLAGS = [  # (a verb that had it, valid arguments, old flag and its value if it takes one)
    (["token", "create", "--path", "x", "--label", "l"], ["--chemin", "x"]),
    (["token", "create", "--path", "x", "--label", "l"], ["--duree", "1d"]),
    (["service", "declare", "s", "--server", "https://s.test"], ["--serveur", "https://s.test"]),
    (["service", "declare", "s", "--server", "https://s.test"], ["--modele", "gitea"]),
    (["service", "declare", "s", "--server", "https://s.test"], ["--portees", "x"]),
    (["service", "declare", "s", "--server", "https://s.test"], ["--hote-interne"]),
    (["service", "upstream", "s"], ["--hote-interne-mcp"]),
    (["service", "upstream", "s"], ["--url-mcp", "https://m.test/mcp"]),
    (["service", "upstream", "s"], ["--retirer"]),
    (["application", "declare", "a", "--service", "s", "--mode", "declared"],
     ["--secret-fichier", "-"]),
    (["connection", "add", "c/n", "--service", "s", "--prefix", "px"], ["--fourni"]),
    (["connection", "add", "c/n", "--service", "s", "--prefix", "px"], ["--fichier", "-"]),
    (["connection", "add", "c/n", "--service", "s", "--prefix", "px"], ["--prefixe", "px"]),
    (["connection", "add", "c/n", "--service", "s", "--prefix", "px"], ["--compte", "a"]),
    (["connection", "authorize", "c/n", "--callback", "-"], ["--retour", "-"]),
    (["connection", "authorize", "c/n", "--callback", "-"], ["--confirme-compte"]),
    (["connection", "place", "c/n", "a/b/c/d"], ["--confirme-partage"]),
    (["connection", "tools", "c/n", "--role", "a/b/c/d"], ["--purger-noms"]),
]  # fmt: skip


def _walk(parser: argparse.ArgumentParser) -> dict[str, dict[str, dict[str, list[str] | None]]]:
    """Groups, verbs, options and positionals with their accepted values, read from the parser."""

    def subparsers(p: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
        for action in p._actions:
            if isinstance(action, argparse._SubParsersAction):
                return dict(action.choices)
        return {}

    def args(p: argparse.ArgumentParser) -> dict[str, list[str] | None]:
        out: dict[str, list[str] | None] = {}
        for action in p._actions:
            if isinstance(action, argparse._HelpAction | argparse._SubParsersAction):
                continue
            names = action.option_strings or [action.dest]
            for name in names:
                out[name] = sorted(action.choices) if action.choices else None
        return out

    return {
        group: {verb: args(vp) for verb, vp in subparsers(gp).items()}
        for group, gp in subparsers(parser).items()
    }


def test_br_131_parser_surface_is_the_pinned_table() -> None:
    surface = _walk(cli._parser())
    assert surface == PINNED_CLI
    assert len(surface) == 15  # F-015: policy, tool; F-016: admin; F-020: consent
    assert (
        sum(len(verbs) for verbs in surface.values()) == 62
    )  # F-015 lot B: pin, …; F-016: 4; F-020: 2
    flags = {f for verbs in surface.values() for a in verbs.values() for f in a if f[0] == "-"}
    assert len(flags) == 35  # F-016 slice 4: --expires-at
    assert set(tree.LEVEL_ALIASES) == PINNED_LEVELS
    assert set(tree.SETTINGS) == PINNED_SETTINGS


def _mutants() -> list[tuple[str, Any]]:
    """One mutant per key compared: every group, verb, option and accepted value renamed once."""
    out: list[tuple[str, Any]] = []
    for g, verbs in PINNED_CLI.items():
        out.append((f"group:{g}", ("group", g)))
        for v, opts in verbs.items():
            out.append((f"verb:{g}.{v}", ("verb", g, v)))
            for o, choices in opts.items():
                out.append((f"opt:{g}.{v}.{o}", ("opt", g, v, o)))
                for c in choices or []:
                    out.append((f"choice:{g}.{v}.{o}={c}", ("choice", g, v, o, c)))
    return out


MUTANTS = _mutants()


@pytest.mark.parametrize("mutant", [m for _, m in MUTANTS], ids=[i for i, _ in MUTANTS])
def test_br_131_parser_mutant_is_seen(mutant: tuple[str, ...]) -> None:
    surface = copy.deepcopy(_walk(cli._parser()))
    kind = mutant[0]
    if kind == "group":
        surface[mutant[1] + "x"] = surface.pop(mutant[1])
    elif kind == "verb":
        g, v = mutant[1], mutant[2]
        surface[g][v + "x"] = surface[g].pop(v)
    elif kind == "opt":
        g, v, o = mutant[1], mutant[2], mutant[3]
        surface[g][v][o + "x"] = surface[g][v].pop(o)
    else:
        g, v, o, c = mutant[1], mutant[2], mutant[3], mutant[4]
        choices = surface[g][v][o]
        assert choices is not None
        surface[g][v][o] = sorted([*(x for x in choices if x != c), c + "x"])
    assert surface != PINNED_CLI


def test_br_131_parser_mutants_cover_the_table() -> None:
    assert (
        len(MUTANTS)
        == 15 + 62 + sum(len(o) for v in PINNED_CLI.values() for o in v.values()) + 12 + 15 + 3
    )


# -- AC-08 : clean break, the old spellings are refused with the usage error ----------------------

OLD_CASES = (
    [(f"group-{g}", [g, "list"]) for g in OLD_GROUPS]
    + [(f"verb-{g}-{v}", [g, v, *rest]) for g, v, rest in OLD_VERBS]
    + [(f"flag-{flag[0]}", [*base, *flag]) for base, flag in OLD_FLAGS]
)


def _fingerprint(gw: Gateway) -> list[tuple[Any, ...]]:
    conn = gw.connect()
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return [(t, conn.execute(f"SELECT * FROM {t}").fetchall()) for t in sorted(tables)]  # noqa: S608
    finally:
        conn.close()


def test_br_131_old_spellings_count() -> None:
    assert (len(OLD_GROUPS), len(OLD_VERBS), len(OLD_FLAGS), len(OLD_CASES)) == (2, 24, 18, 44)


@pytest.mark.parametrize("argv", [a for _, a in OLD_CASES], ids=[i for i, _ in OLD_CASES])
def test_br_131_old_spellings_refused(gw: Gateway, argv: list[str]) -> None:
    before = _fingerprint(gw)
    result = gw.cli(*argv, stdin="value\n")
    assert result.code == 2, result
    assert result.err.startswith("refused: usage: "), result
    assert result.out == ""
    assert _fingerprint(gw) == before


# -- AC-14 : JSON and YAML shapes, key sets and closed value sets --------------------------------

STATUS_KEYS = {
    "expiring", "access_ends_at",
    "name", "service", "type", "application", "prefix", "account", "scopes", "state",
    "stored_state", "key", "expires_at", "expiry_estimated", "not_before", "last_outcome",
    "placements", "application_to_redeclare", "upstream", "refusal",
}  # fmt: skip
SERVICE_KEYS = {
    "id", "model", "url", "scopes", "internal_host", "endpoints", "resource", "upstream",
}  # fmt: skip
SERVICE_UPSTREAM_KEYS = {"type", "mcp_url", "mcp_internal_host"}
APPLICATION_KEYS = {
    "id", "service", "mode", "client_id", "redirect_uri", "secret", "secret_expires_at", "method",
}  # fmt: skip
TOOLS_KEYS = {"connection", "role", "version", "tools", "invalid_names", "purged_names"}
TOOL_KEYS = {"name", "upstream_name"}
TOKEN_KEYS = {"id", "type", "holder", "path", "issued_at", "expires_at", "state"}
CLIENT_KEYS = {"client_id", "kind", "path", "redirect_uris", "name", "state"}
STATE_VALUES = {
    "pending",
    "active",
    "reauth_required",
    "key_absent",
    "key_invalid",
    "key_different",
}
STORED_STATE_VALUES = {"pending", "active", "reauth_required"}
TOKEN_TYPES = {"operator", "oauth"}
TOKEN_STATES = {"active", "expired", "revoked"}
CLIENT_STATES = {"active", "removed"}


@pytest.fixture
def seeded(gw: Gateway) -> Gateway:
    standard_tree(gw)
    gw.service()
    gw.upnet.upstream("sh.mcp.test", "modern")
    gw.ok("service", "upstream", "gitea-dev", "--type", "mcp-public", "--mcp-url",
          "https://sh.mcp.test/mcp")  # fmt: skip
    gw.static("mpx/gitea-dev", "gd")
    gw.ok("connection", "place", "mpx/gitea-dev", "mpx/cairn/staging/dev")
    gw.application()
    gw.open_oauth("mpx/pending", "pe")
    gw.oauth("mpx/oauth", "oa")
    gw.issue("mpx", "one")
    tid, _ = gw.issue("mpx", "two")
    gw.ok("token", "revoke", tid)
    gw.preregister("mpx", "c-live")
    gw.preregister("mpx", "c-gone")
    gw.ok("client", "remove", "c-gone")
    return gw


def test_br_131_json_shape_connection_status(seeded: Gateway) -> None:
    views = json.loads(seeded.ok("connection", "status", "--json"))
    assert {v["name"] for v in views} == {"mpx/gitea-dev", "mpx/pending", "mpx/oauth"}
    for v in views:
        assert set(v) == STATUS_KEYS
        assert v["state"] in STATE_VALUES
        assert v["stored_state"] in STORED_STATE_VALUES
        assert all(set(p) == {"role", "sensitive"} for p in v["placements"])
    assert {v["state"] for v in views} == {"pending", "active"}
    seeded.connections_key_path.unlink()
    views = json.loads(seeded.ok("connection", "status", "--json"))
    assert {(v["state"], v["key"]) for v in views} == {("key_absent", "key_absent")}


def test_br_131_json_shape_service_application_tokens_clients(seeded: Gateway) -> None:
    [service] = json.loads(seeded.ok("service", "list", "--json"))
    assert set(service) == SERVICE_KEYS
    assert set(service["upstream"]) == SERVICE_UPSTREAM_KEYS
    assert service["upstream"]["type"] in {"mcp-public", "mcp-internal"}
    assert service["model"] in {"gitea", "generic"}
    [app] = json.loads(seeded.ok("application", "list", "--json"))
    assert set(app) == APPLICATION_KEYS
    assert app["mode"] in {"declared", "dynamic"}
    assert app["redirect_uri"] == "http://127.0.0.1/oauth/callback"
    tokens = json.loads(seeded.ok("token", "list", "--json"))
    assert all(set(t) == TOKEN_KEYS for t in tokens)
    assert {t["type"] for t in tokens} <= TOKEN_TYPES
    assert {t["state"] for t in tokens} == {"active", "revoked"} <= TOKEN_STATES
    clients = json.loads(seeded.ok("client", "list", "--json"))
    assert all(set(c) == CLIENT_KEYS for c in clients)
    assert {c["state"] for c in clients} == CLIENT_STATES


TREE_KEYS = {"path", "level", "segment", "name", "sensitive", "locked_by"}
TREE_ROLE_KEYS = TREE_KEYS | {"url"}
TREE_ORG_KEYS = TREE_KEYS | {"public_id"}  # F-014 §3.3: the organisation's item only


def test_br_131_json_shape_tree_show(seeded: Gateway) -> None:
    items = json.loads(seeded.ok("tree", "show", "/", "--json"))
    shapes = {
        "org": TREE_ORG_KEYS,
        "client": TREE_KEYS,
        "project": TREE_KEYS,
        "env": TREE_KEYS,
        "role": TREE_ROLE_KEYS,
    }
    assert {i["level"] for i in items} == set(shapes)
    for item in items:
        assert set(item) == shapes[item["level"]], item
    assert [i["public_id"] for i in items if "public_id" in i] == [seeded.org_id]


def test_br_131_json_shape_connection_tools(seeded: Gateway) -> None:
    data = json.loads(seeded.ok("connection", "tools", "mpx/gitea-dev", "--role",
                                "mpx/cairn/staging/dev", "--json"))  # fmt: skip
    assert set(data) == TOOLS_KEYS
    assert data["tools"]
    assert all(set(t) == TOOL_KEYS for t in data["tools"])


EXPORT_KEYS = {
    "connection_expiry_days",
    "format", "organisation", "level", "segment", "name", "sensitive", "settings", "clients",
    "projects", "environments", "roles", "services", "applications", "connections", "id", "model",
    "url", "scopes", "internal_host", "service", "mode", "client_id", "prefix", "application",
    "state", "placements", "token_duration", "lock", "pinning", "response_cap",
    "token_rate_limit", "connection_rate_limit",  # F-020: two inherited settings
    "tools", "path", "pin", "open", "deny", "allowlist", "pinned_tools", "arguments",
}  # fmt: skip  # format 4 (F-015): placements as mappings, service catalogue; 5 (F-014): `id`
EXPORT_LEVELS = {"organisation", "client", "project", "environment", "role"}


def test_br_131_export_format_3(seeded: Gateway) -> None:
    """Named after the format F-010 pinned (BR-131 control note); format 5 since F-014 (BR-070),
    the organisation's `id` first under `organisation:`."""
    seeded.ok("settings", "set", "mpx", "token_duration", "10d")
    text = seeded.ok("export")
    assert text.startswith("# hullgate")
    keys = set(re.findall(r"^\s*(?:- )?([a-z_]+):", text, re.MULTILINE))
    assert keys == EXPORT_KEYS
    assert set(re.findall(r"level: (\w+)", text)) == EXPORT_LEVELS
    assert re.search(r'^organisation:\n  id: "[a-z2-7]{12}"\n  level: organisation$', text, re.M)
    assert "format: 5\n" in text
    assert export.FORMAT == 5


def test_br_131_export_shape_mutant_is_seen(seeded: Gateway) -> None:
    """Negative control: a renamed key of the export no longer equals the pinned set."""
    text = seeded.ok("export").replace("environments:", "environnements:")
    keys = set(re.findall(r"^\s*(?:- )?([a-z_]+):", text, re.MULTILINE))
    assert keys != EXPORT_KEYS


# -- CHECK lists of a fresh migrated database ------------------------------------------------------

PINNED_CHECKS = {
    "connection.kind": {"oauth", "provided"},
    "connection.state": {"pending", "active", "reauth_required"},
    "connection.last_outcome": {"renewed", "permanent", "uncertain", "transient", "anomaly"},
    "service.model": {"gitea", "generic"},
    "service.upstream_type": {"mcp-public", "mcp-internal"},
    "application.mode": {"declared", "dynamic"},
    "application.auth_method": {"none", "client_secret_basic", "client_secret_post"},
    "client.kind": {"preregistered", "cimd", "dcr"},
    "node.level": {"org", "client", "project", "env", "role"},
    # F-015 migration 7
    "placement.mode": {"read", "write", "destructive"},
    "placement.pin": {"on", "off"},
    "tool_catalog.manual_class": {"read", "write", "destructive"},
    "tool_catalog.annotation_class": {"read", "write", "destructive"},
    "placement_tool.list": {"open", "deny", "allowlist"},
    # F-016 lot A, migration 8
    "administrator.state": {"pending", "active"},
    "enrolment.kind": {"bootstrap", "recovery"},
    "webauthn_challenge.purpose": {"sign_in", "enrol", "fresh", "passkey", "barrier"},
}


def test_br_131_check_lists_are_english(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "fresh.db", isolation_level=None)
    migrations.migrate(conn)
    found: dict[str, set[str]] = {}
    for table, sql in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table'"):
        for col, values in re.findall(r"(\w+) IN \(('[^)]*')\)", sql or ""):
            found[f"{table}.{col}"] = set(re.findall(r"'([^']*)'", values))
    conn.close()
    assert found == PINNED_CHECKS


# -- AC-15 : CLI output tour --------------------------------------------------------------------

LINES_FILE = Path(__file__).with_name("cli_output_lines.txt")
DISTINCT_LINES = 222  # pinned at the first green run (2026-10-31); F-015 lots A to C; F-016 lot A;
# F-014: the export's organisation `id`; F-020: settings, consent
FRENCH_STEMS = re.compile(
    r"connexion|jeton|erreur|valeur|créé|supprimé|déplacé|placée|révoqué|motif|avant|après"
    r"|\b(?:creer|lister|revoquer|ajouter|autoriser|reautoriser|renouveler|prefixe|placer|etat"
    r"|outils|declarer|redecouvrir|amont|reenregistrer|retirer|projet|environnement|rôle"
    r"|fourni|generique|declaree|dynamique|verrou|rendu|definitive|incertaine|transitoire"
    r"|anomalie|en_attente|reauth_requise|duree_jetons|placee|creee?|supprimee?|deplacee?"
    r"|revoquee?|apres|sensible|compte|portees|echeance)\b"
    r"|[éèêàçù]",
    re.IGNORECASE,
)


def test_br_131_cli_output_tour(gw: Gateway) -> None:
    lines, played = run_tour(gw)
    verbs = {(g, v) for g, verbs in PINNED_CLI.items() for v in verbs} | {("export", "")}
    assert played == verbs  # every verb is played once at least
    if os.environ.get("UPDATE_CLI_LINES") == "1":
        LINES_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert lines == LINES_FILE.read_text(encoding="utf-8").splitlines()
    assert len(set(lines)) == DISTINCT_LINES
    hits = [line for line in lines if FRENCH_STEMS.search(line)]
    assert hits == []


def test_br_131_stem_check_sees_french() -> None:
    """Positive fixture of the stem check: unaccented French is caught."""
    for sample in ("placee sur", "connexion creee", "etat active", "valeur remplacee", "créé"):
        assert FRENCH_STEMS.search(sample), sample


# -- AC-16 : runbooks follow the CLI ---------------------------------------------------------------

RUNBOOKS = {  # CLI invocations in the merged guides; parser guarantees unchanged.
    "keys-and-upstreams.md": 77,
    "update.md": 1,
    "admin.md": 7,  # F-023: D2 carries AC-OP-03's bootstrap and admin list
}

_INVOCATION = re.compile(r"(?:\$C(?: hullgate admin)?|(?<!\$C )hullgate admin) ([^`\n]*)")


def _runbook_invocations(text: str) -> list[list[str]]:
    """Every CLI invocation of a runbook: `$C …` (C ends with `hullgate admin`, or with `exec -T
    hullgate` and the command repeats `hullgate admin`) and `hullgate admin …`, in a code block or
    quoted in the prose, cut at the first shell operator."""
    found: list[list[str]] = []
    for piece in re.split(r"&&|\|\||;|\||`|\n", text):
        match = _INVOCATION.search(piece)
        if match is None:
            continue
        command = re.split(r"\s[<>]\s|\s2?>", match.group(1))[0]
        command = re.sub(r"<[^<>]*>", "PLACEHOLDER", command)  # `<rôle sensible>`: one word
        try:
            words = shlex.split(command)
        except ValueError:  # an unbalanced quote cut by the operators above
            words = command.split()
        words = [w for w in words if w != "\\"]  # a line continuation
        if not words or words[0] in ("python", "sh", "hullgate", "…"):
            continue
        if words[0] in cli.ADMIN_VERBS:  # `hullgate admin bootstrap`: the admin group (F-016)
            words = ["admin", *words]
        found.append(words)
    return found


def test_br_131_runbook_commands_parse() -> None:
    counts = {}
    for name in RUNBOOKS:
        text = (ROOT / "docs" / "guides" / name).read_text(encoding="utf-8")
        invocations = _runbook_invocations(text)
        counts[name] = len(invocations)
        for words in invocations:
            try:
                ns = cli._parser().parse_args(words)
            except Exception as exc:
                pytest.fail(f"{name}: {' '.join(words)} -> {exc}")
            if (ns.group, ns.cmd) == ("tree", "add"):
                assert ns.level in PINNED_LEVELS, (name, words)
            if (ns.group, ns.cmd) in (("settings", "set"), ("settings", "unset")):
                assert ns.key in PINNED_SETTINGS, (name, words)
    assert counts == RUNBOOKS


def test_br_131_runbook_extractor_sees_an_old_verb() -> None:
    """Negative control: an old spelling in a runbook line is refused by the parser."""
    [words] = _runbook_invocations('C="x hullgate admin" && $C connexion etat')
    with pytest.raises(Exception):  # noqa: B017, PT011
        cli._parser().parse_args(words)


# -- AC-17 : the bench drives the new CLI ----------------------------------------------------------

BENCH = ROOT / "poc" / "isolation-bench" / "runner"
BENCH_FILES = {
    "adapter_v1.py": 48,  # F-015 lot C: catalogue, classes, deny, AC-2 reviewer (BR-071 v3)
    "harness/v1-harness.py": 9,  # F-014: `tree show / --json` reads the identifier, in each
}


def _value(arg: ast.expr) -> str:
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    return "x/y/z/w"  # a value computed at run time


def _cli_calls(path: Path) -> list[list[str]]:
    """Arguments of every `<name>.cli(...)` call; `*args` bound by a `for` over a literal tuple
    of tuples gives one call per tuple, any other `*name` (`*extra`) is dropped."""
    module = ast.parse(path.read_text(encoding="utf-8"))
    loops: dict[str, list[list[str]]] = {}
    for node in ast.walk(module):
        if (
            isinstance(node, ast.For)
            and isinstance(node.target, ast.Name)
            and isinstance(node.iter, ast.Tuple)
            and all(isinstance(e, ast.Tuple) for e in node.iter.elts)
        ):
            loops[node.target.id] = [
                [_value(v) for v in e.elts] for e in node.iter.elts if isinstance(e, ast.Tuple)
            ]
    calls: list[list[str]] = []
    for node in ast.walk(module):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "cli" or not isinstance(node.func.value, ast.Name):
            continue
        starred = [a.value for a in node.args if isinstance(a, ast.Starred)]
        if len(node.args) == 1 and starred and isinstance(starred[0], ast.Name):
            calls.extend(loops.get(starred[0].id, []))
            continue
        # `*extra`: `("--confirm-account",)`, checked below
        calls.append([_value(a) for a in node.args if not isinstance(a, ast.Starred)])
    return calls


def test_br_131_bench_adapter_parses() -> None:
    counts = {}
    for name in BENCH_FILES:
        calls = _cli_calls(BENCH / name)
        counts[name] = len(calls)
        for args in calls:
            try:
                cli._parser().parse_args(args)
            except Exception as exc:
                pytest.fail(f"{name}: {args} -> {exc}")
    assert counts == BENCH_FILES
    adapter = (BENCH / "adapter_v1.py").read_text(encoding="utf-8")
    assert 'extra = ("--confirm-account",)' in adapter
    # the adapter reads only pinned fields of the status JSON, the harness pinned token values
    read = set(re.findall(r'st(?:\.get\(|\[)"(\w+)"', adapter))
    assert read
    assert read <= STATUS_KEYS
    harness = (BENCH / "harness" / "v1-harness.py").read_text(encoding="utf-8")
    assert re.findall(r't\["type"\] == "(\w+)"', harness) == ["oauth"]
    assert re.findall(r't\["state"\] == "(\w+)"', harness) == ["active"]


# -- sweeps (F-010 §3.5) --------------------------------------------------------------------------

ROOTS = [
    "src",
    "scripts",
    "deploy",
    "Makefile",
    "Dockerfile",
    "compose.yaml",
    "pyproject.toml",
    "uv.lock",
    "sonar-project.properties",
    "poc/isolation-bench/runner",
    "poc/isolation-bench/mapping.json",
    "poc/isolation-bench/README.md",
    "tests",
    "docs/guides",
    "README.md",
]
# Pinned exclusions, each with its reason (F-010 §3.5 pins six; HULL-58 removed the cutover runbook
# and its test, the sixth and seventh).
EXCLUSIONS = {
    "src/hullgate/migrations.py": "migrations 1 to 6 carry the old values by design",
    "tests/unit/test_br_132_vocabulary_migration.py": "fabricated pre-migration database",
    "tests/unit/test_br_131_contract_surface.py": "holds the sweeps",
    "tests/unit/test_br_069_migrations.py": "builds v3 to v5 databases needing old values",
    "tests/unit/test_br_069_migration_f008.py": "builds v4 and v5 databases with old values",
}
# F-011 §3.4: the names bound to data, built from fragments so that no test spells a former name.
_OLD = "clo" + "isonne"
OLD_VOLUME = _OLD + "_data"
OLD_FILE = _OLD + ".db"
OLD_LABEL_STEM = _OLD + "/connexions/controle-de-cle"
OLD_PREFIX = "clo" + "_"
OLD_DIRECTORY = "<compose-root>/" + _OLD
_SWEEP_H = (
    re.escape(OLD_VOLUME)
    + "|"
    + re.escape(OLD_FILE)
    + "|"
    + re.escape(OLD_LABEL_STEM)
    + r"|\b"
    + OLD_PREFIX
    + "|"
    + re.escape(OLD_DIRECTORY)
    + "(?!-poc-go)"
    # Server directory split from its parent (review 554, B2): clone from the compose root
    # … <old> && cd <old>`, its error line and the backup cron filter; the application name as C7
    # quotes it (B3), the one form of that bare name sweep H can see without HULL-58's text
    + r"|\bcd "
    + _OLD
    + r"\b(?!-)|git clone \S+ "
    + _OLD
    + r"\b(?!-)|destination path '"
    + _OLD
    + "'|'"
    + _OLD
    + " && make backup'|nom `"
    + _OLD
    + "`"
)
SWEEPS = {
    "A": (
        r'"(jeton|connexion)"|\b(jeton (creer|lister|revoquer)|connexion (ajouter|autoriser'
        r"|reautoriser|renouveler|revoquer|prefixe|placer|retirer-placement|etat|lister|retirer"
        r"|outils)|service (declarer|lister|retirer|redecouvrir|amont)|application (declarer"
        r'|lister|retirer|reenregistrer))\b|"(creer|revoquer|ajouter|autoriser|reautoriser'
        r"|renouveler|prefixe|placer|retirer-placement|etat|outils|declarer|redecouvrir|amont"
        r'|reenregistrer)"|--(chemin|duree|serveur|modele|portees|hote-interne|hote-interne-mcp'
        r"|retirer|secret-fichier|fourni|fichier|prefixe|compte|retour|confirme-compte"
        r'|confirme-partage|purger-noms|url-mcp)\b|"(projet|environnement|rôle)"'
    ),
    "B1": (
        r"\b(non_placee|non_autorisee|reauthentification_requise|temporairement_indisponible"
        r'|cle_indisponible|erreur_integrite|hors_client)\b|"sensibilite"'
    ),
    "B2": (
        r"\b(amont_indisponible|amont_occupe|echeance_depassee|issue_incertaine"
        r"|reponse_trop_grande|requete_trop_grande|connexion_a_reautoriser|erreur_interne"
        r'|erreur_outil)\b|["`](refuse|annule)["`]'
        # the spec's `M_* = "…"` alternative is narrowed to the retired values: as written it would
        # also match the new ones (F-010 §3.5, deviation noted in the PR B report)
        r'|\bM_(DOWN|DEADLINE|SENSITIVE) *= *"(panne|échéance|sensible)"'
        r"|réessayer dans|\[masqué\]|plus de 1 000 noms|\"nom invalide\""
    ),
    "C": (
        r"\b(en_attente|reauth_requise|mcp-distant|mcp-local|duree_jetons)\b|\"(fourni|generique"
        r'|declaree|dynamique|verrou|rendu|definitive|incertaine|transitoire|anomalie)"'
        r"|'(fourni|generique|declaree|dynamique|rendu|definitive|incertaine)'|sans refresh_token"
        r"|bail échu|écriture impossible|application réenregistrée|refus signalé"
        r"|refus amont signalé|échéance trop courte"
    ),
    "D": (
        r"VERIFY-(RELAIS|CONNEXIONS|SOCLE)|verify[-_](relais|connexions|socle)|ÉCHEC"
        r"|\bAMONTS_NETWORK\b|"
        + _OLD
        + r"[-_]amonts|amonts\.compose|networks: \[amonts\]|^ *amonts: "
        r"|^ *- amonts|\b(CONNEXION|SENSIBLE|OUTIL)\b"
    ),
    "E": r'clé (absente|invalide|différente)|"cle"|oauth/retour',
    "F": r'"(actif|expiré|révoqué|retiré|operateur|inconnu|chiffré)"|\(supprimé\)|amont MCP non déclaré',  # noqa: E501
    "H": _SWEEP_H,
}
# Tokens stripped from each line before matching (never the whole line), with their reason. None
# since HULL-58: sweep G and its four kept tokens left for the former-name guard; a carve-out on any
# sweep is a review finding.
KEPT: dict[str, list[tuple[str, str]]] = {}
POSITIVE = {
    "A": [
        '"jeton"', '"connexion"', "jeton creer", "jeton lister", "jeton revoquer",
        "connexion ajouter", "connexion autoriser", "connexion reautoriser", "connexion renouveler",
        "connexion revoquer", "connexion prefixe", "connexion placer",
        "connexion retirer-placement",
        "connexion etat", "connexion lister", "connexion retirer", "connexion outils",
        "service declarer", "service lister", "service retirer", "service redecouvrir",
        "service amont", "application declarer", "application lister", "application retirer",
        "application reenregistrer", '"creer"', '"revoquer"', '"ajouter"', '"autoriser"',
        '"reautoriser"', '"renouveler"', '"prefixe"', '"placer"', '"retirer-placement"', '"etat"',
        '"outils"', '"declarer"', '"redecouvrir"', '"amont"', '"reenregistrer"', "--chemin",
        "--duree", "--serveur", "--modele", "--portees", "--hote-interne", "--hote-interne-mcp",
        "--retirer", "--secret-fichier", "--fourni", "--fichier", "--prefixe", "--compte",
        "--retour", "--confirme-compte", "--confirme-partage", "--purger-noms", "--url-mcp",
        '"projet"', '"environnement"', '"rôle"',
    ],
    "B1": [
        "non_placee", "non_autorisee", "reauthentification_requise", "temporairement_indisponible",
        "cle_indisponible", "erreur_integrite", "hors_client", '"sensibilite"',
    ],
    "B2": [
        "amont_indisponible", "amont_occupe", "echeance_depassee", "issue_incertaine",
        "reponse_trop_grande", "requete_trop_grande", "connexion_a_reautoriser", "erreur_interne",
        "erreur_outil", '"refuse"', '"annule"', "`annule`", "`refuse`",
        'M_DOWN = "panne"', 'M_DEADLINE = "échéance"',
        'M_SENSITIVE = "sensible"', "(réessayer dans 3 s)", "[masqué]", "plus de 1 000 noms",
        '"nom invalide"',
    ],
    "C": [
        "en_attente", "reauth_requise", "mcp-distant", "mcp-local", "duree_jetons", '"fourni"',
        '"generique"', '"declaree"', '"dynamique"', '"verrou"', '"rendu"', '"definitive"',
        '"incertaine"', '"transitoire"', '"anomalie"', "'fourni'", "'generique'", "'declaree'",
        "'dynamique'", "'rendu'", "'definitive'", "'incertaine'", "sans refresh_token",
        "bail échu", "écriture impossible", "application réenregistrée", "refus signalé",
        "refus amont signalé", "échéance trop courte",
    ],
    "D": [
        "VERIFY-RELAIS", "VERIFY-CONNEXIONS", "VERIFY-SOCLE", "verify-relais", "verify_connexions",
        "verify-socle", "ÉCHEC", "AMONTS_NETWORK", _OLD + "_amonts", _OLD + "-amonts",
        "amonts.compose", "    networks: [amonts]", "  amonts: {}", "      - amonts", "CONNEXION=",
        "SENSIBLE=", "OUTIL=",
    ],
    "E": ["clé absente", "clé invalide", "clé différente", '"cle"', "oauth/retour"],
    "F": [
        '"actif"', '"expiré"', '"révoqué"', '"retiré"', '"operateur"', '"inconnu"', '"chiffré"',
        "(supprimé)", "amont MCP non déclaré",
    ],
    "H": [
        "volume " + OLD_VOLUME, "/data/" + OLD_FILE, OLD_LABEL_STEM + "/v1", OLD_PREFIX + "x",
        "cd " + OLD_DIRECTORY + " && make", OLD_DIRECTORY + "/secrets/connections_key",
        "x.git " + _OLD + " && cd " + _OLD + " && git log", "git clone a:b/c.git " + _OLD,
        "destination path '" + _OLD + "' already", "grep -v '" + _OLD + " && make backup'",
        "OAuth2**, nom `" + _OLD + "`, URI",
    ],
}  # fmt: skip
# Negative fixture of sweep H: the new names, the POC paths and a word that ends in the prefix.
NEGATIVE_H = [
    "hullgate_data", "/data/hullgate.db", "hullgate/connections/key-check/v1", "hg_x",
    "/opt/hullgate", OLD_DIRECTORY + "-poc-go/", "x" + OLD_PREFIX + "x",
    "cd hullgate && git log", "cd " + _OLD + "-poc-go", "nom `hullgate`",
    "git clone x " + _OLD + "-y",
]  # fmt: skip


def _files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--", *ROOTS], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    assert len(out) > 200, "the sweep roots resolve to too few files"
    return [f for f in out if (ROOT / f).is_file()]


def sweep(name: str, files: list[str] | None = None) -> list[str]:
    pattern = re.compile(SWEEPS[name], re.MULTILINE)
    kept = KEPT.get(name, [])
    hits: list[str] = []
    for rel in files if files is not None else _files():
        if rel in EXCLUSIONS:
            continue
        try:
            lines = (ROOT / rel).read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for i, line in enumerate(lines, 1):
            text = line
            for kept_pattern, _ in kept:
                text = re.sub(kept_pattern, "", text)
            if pattern.search(text):
                hits.append(f"{rel}:{i}: {line.strip()[:140]}")
    return hits


@pytest.mark.parametrize("name", list(POSITIVE))
def test_br_131_sweep_positive_fixture(name: str) -> None:
    pattern = re.compile(SWEEPS[name], re.MULTILINE)
    for sample in POSITIVE[name]:
        assert pattern.search(sample), (name, sample)


def test_br_131_exclusions_exist() -> None:
    assert {rel for rel in EXCLUSIONS if (ROOT / rel).is_file()} == set(EXCLUSIONS)
    assert len(EXCLUSIONS) == 5  # F-010 §3.5 pins six; HULL-58 removed the cutover runbook
    assert not (ROOT / "deploy" / "smoke" / "cutover.sh").exists()  # F-011 AC-09


@pytest.mark.parametrize("name", ["A", "B1", "C", "E", "F"])
def test_br_131_sweeps_phase1(name: str) -> None:
    assert sweep(name) == []


def test_br_131_sweep_b2_phase2() -> None:
    assert sweep("B2") == []


def test_br_131_sweep_h() -> None:
    """F-011 AC-07, AC-10: no name bound to data in its former spelling, outside the exclusions
    (runbooks included, so the fresh-start steps F1 to F6 spell none)."""
    assert sweep("H") == []


def test_br_131_sweep_h_negative_fixture() -> None:
    """The new names, the POC directory and a longer word ending in the old prefix do not fire."""
    pattern = re.compile(SWEEPS["H"], re.MULTILINE)
    for sample in NEGATIVE_H:
        assert not pattern.search(sample), sample


def test_br_131_sweep_d_phase3() -> None:
    """AC-23, AC-24: ops lines, make targets, variables, modules and infrastructure names."""
    assert sweep("D") == []


def test_br_131_no_kept_tokens() -> None:
    """Review A of F-010, minor-2, after HULL-58: no sweep strips tokens any more."""
    assert KEPT == {}


def test_br_131_sweep_a_sees_the_old_check_names(tmp_path: Path) -> None:
    """The check names PR A carved out (`"etat"`, `"jeton"`) are seen by sweep A again."""
    probe = ROOT / "tests" / "unit" / "_sweep_probe_a.txt"
    probe.write_text('CHECKS = ("etat", "jeton")\n', encoding="utf-8")
    try:
        assert len(sweep("A", ["tests/unit/_sweep_probe_a.txt"])) == 1
    finally:
        probe.unlink()


def test_br_131_old_key_check_label_is_absent_from_src() -> None:
    """F-011 §3.4 (BR-131 v2): the key-check label is English; the former label's stem is
    nowhere in `src/`, migrations included (the label never was in a migration)."""
    files = [f for f in _files() if f.startswith("src/")]
    assert files
    hits = [f for f in files if OLD_LABEL_STEM in (ROOT / f).read_text(encoding="utf-8")]
    assert hits == []


def test_br_131_data_names_agree() -> None:
    """F-011 AC-02: the database file and the volume default, in code and in `deploy/`."""
    assert config.DB_FILENAME == "hullgate.db"
    ops = (ROOT / "deploy" / "ops.sh").read_text(encoding="utf-8")
    assert re.findall(r"^DB=(\S+)$", ops, re.MULTILINE) == ["/data/hullgate.db"]
    assert re.findall(r"env_get HULLGATE_VOLUME (\S+)\)", ops) == ["hullgate_data"]
    prod = (ROOT / "deploy" / "compose.prod.yaml").read_text(encoding="utf-8")
    assert _compose_volumes(prod) == {"data": "${HULLGATE_VOLUME:-hullgate_data}"}


# -- ops and infrastructure (F-010 §3.2 ops table; AC-23, AC-24) ---------------------------------

PINNED_MAKE_TARGETS = {
    "help", "sync", "gate", "fmt", "lint", "typecheck", "test", "test-container", "qa", "qa-cov",
    "qa-audit", "qa-sonar", "sonar", "image", "image-multiarch", "clean-images", "prod-smoke",
    "bench-sdk", "deploy", "backup", "restore-check", "verify", "verify-auth",
    "verify-connections", "verify-relay", "status", "logs",
    "qa-web-audit", "test-browser", "verify-admin",  # F-016 lot A
    "qa-secrets",  # F-022 slice 3
}  # fmt: skip
PINNED_OPS_VARIABLES = {"REF", "FILE", "ROLE", "CONNECTION", "SENSITIVE", "TOOL"}
PINNED_ENV_KEYS = {
    "ADMIN_HOST", "ADMIN_URL", "ADMIN_RP_ID", "TRUSTED_PROXIES",
    "MCP_HOST", "TRAEFIK_NETWORK", "TRAEFIK_ENTRYPOINT", "TRAEFIK_CERTRESOLVER",
    "TRAEFIK_HTTPS_PORT", "ADMIN_LOCAL_PORT", "SIGNING_KEY_PATH", "AUTHORIZE_TOTP_PATH",
    "CONNECTIONS_KEY_PATH", "HULLGATE_VOLUME", "HULLGATE_IMAGE", "GITEA_URL", "UPSTREAMS_NETWORK",
}  # fmt: skip
UPSTREAMS_NAME = "${UPSTREAMS_NETWORK:-hullgate_upstreams}"
PINNED_COMPOSE = {  # file: (project, services, {network key: name})
    "compose.yaml": ("hullgate-dev", {"hullgate"}, {}),
    "deploy/compose.admin.yaml": ("", {"hullgate"}, {}),
    "deploy/compose.prod.yaml": (
        "hullgate",
        {"hullgate"},
        {"traefik": "${TRAEFIK_NETWORK}", "upstreams": UPSTREAMS_NAME},
    ),
    "deploy/upstreams.compose.yaml": (
        "hullgate-upstreams",
        {"gitea-mcp"},
        {"upstreams": UPSTREAMS_NAME},
    ),
    "deploy/smoke/compose.traefik.yaml": (
        "hullgate-smoke-traefik",
        {"traefik"},
        {"traefik": "${TRAEFIK_NETWORK}"},
    ),
}


# F-011 §3.4: the volume default enters the pinned tables (the data volume of production only).
PINNED_VOLUMES = {
    "compose.yaml": {"data": ""},  # development: named by its project, `hullgate-dev_data`
    "deploy/compose.prod.yaml": {"data": "${HULLGATE_VOLUME:-hullgate_data}"},
}


def _make_targets(makefile: Path) -> tuple[set[str], str]:
    """`.PHONY` prerequisites read from `make -pn` (the database make itself builds)."""
    assert shutil.which("make"), "make is needed by the gate (toolbox, F-004)"
    out = subprocess.run(
        ["make", "-pn", "-f", str(makefile)], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    phony = [line for line in out.splitlines() if line.startswith(".PHONY:")]
    assert len(phony) == 1, phony
    return set(phony[0].split(":", 1)[1].split()), out


def test_br_131_make_targets_are_the_pinned_set() -> None:
    targets, database = _make_targets(ROOT / "Makefile")
    assert targets == PINNED_MAKE_TARGETS
    ops = re.findall(r"^(verify-auth|verify-connections|verify-relay):", database, re.MULTILINE)
    assert len(ops) == 3  # AC-23: `make -pn | grep -cE '^(verify-auth|…):'` prints 3


def test_br_131_make_old_targets_fail() -> None:
    """AC-23: the new targets dry-run; the old ones no longer exist (clean break)."""
    new = ["make", "-n", "verify-auth", "verify-connections", "verify-relay"]
    assert subprocess.run(new, cwd=ROOT, capture_output=True, check=False).returncode == 0
    # F-011 AC-09: the cutover rehearsal is deleted with its target
    for old in ("verify-socle", "verify-connexions", "verify-relais", "prod-smoke-cutover"):
        result = subprocess.run(["make", "-n", old], cwd=ROOT, capture_output=True, check=False)
        assert result.returncode != 0, old


def test_br_131_make_target_mutant_is_seen(tmp_path: Path) -> None:
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    mutant = tmp_path / "Makefile"
    mutant.write_text(text.replace(".PHONY: verify-relay\n", ".PHONY: verify-rel\n"), "utf-8")
    assert _make_targets(mutant)[0] != PINNED_MAKE_TARGETS


def test_br_131_make_ops_variables_are_the_pinned_set() -> None:
    lines = (ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
    used = {v for ln in lines if "$(OPS)" in ln for v in re.findall(r"\$\(([A-Z_]+)\)", ln)}
    assert used - {"OPS"} == PINNED_OPS_VARIABLES


def _env_keys(text: str) -> set[str]:
    return {m.group(1) for m in re.finditer(r"^([A-Z][A-Z0-9_]*)=", text, re.MULTILINE)}


def _compose(text: str) -> tuple[str, set[str], dict[str, str]]:
    """Project name, service keys and network names of a compose file (block style, 2 spaces)."""
    project = re.search(r"^name: (\S+)$", text, re.MULTILINE)
    blocks: dict[str, list[str]] = {}
    current = ""
    for line in text.splitlines():
        if re.match(r"^[a-z]", line):
            current = line.split(":", 1)[0]
            blocks.setdefault(current, [])
        elif current and line.strip() and not line.lstrip().startswith("#"):
            blocks[current].append(line)
    services = set()
    for line in blocks.get("services", []):
        if found := re.match(r"^  ([\w-]+):", line):
            services.add(found.group(1))
    networks: dict[str, str] = {}
    key = ""
    for line in blocks.get("networks", []):
        if found := re.match(r"^  ([\w-]+):", line):
            key = found.group(1)
            networks[key] = ""
        elif found := re.match(r"^    name: (\S+)$", line):
            networks[key] = found.group(1)
    return (project.group(1) if project else ""), services, networks


def _compose_volumes(text: str) -> dict[str, str]:
    """Volume keys and their `name:` of a compose file's top-level `volumes:` block."""
    volumes: dict[str, str] = {}
    inside = False
    key = ""
    for line in text.splitlines():
        if re.match(r"^[a-z]", line):
            inside = line.startswith("volumes:")
        elif inside and (found := re.match(r"^  ([\w-]+):", line)):
            key = found.group(1)
            volumes[key] = ""
        elif inside and key and (found := re.match(r"^    name: (\S+)$", line)):
            volumes[key] = found.group(1)
    return volumes


def test_br_131_env_example_keys_are_the_pinned_set() -> None:
    text = (ROOT / "deploy" / "env.example").read_text(encoding="utf-8")
    assert _env_keys(text) == PINNED_ENV_KEYS
    assert re.search(r"^UPSTREAMS_NETWORK=hullgate_upstreams$", text, re.MULTILINE)
    # F-011 AC-02, AC-08: the volume and the secret paths in the server directory
    assert re.search(r"^HULLGATE_VOLUME=hullgate_data$", text, re.MULTILINE)
    secrets = re.findall(r"^[A-Z_]*_PATH=/opt/hullgate/secrets/", text, re.MULTILINE)
    assert len(secrets) == 3


@pytest.mark.parametrize("rel", sorted(PINNED_COMPOSE))
def test_br_131_compose_names_are_the_pinned_table(rel: str) -> None:
    """AC-24: project, services and network names of each compose file; every variable a compose
    file of `deploy/` reads is a key of `env.example` (the smoke files read the test env)."""
    text = (ROOT / rel).read_text(encoding="utf-8")
    assert _compose(text) == PINNED_COMPOSE[rel]
    assert _compose_volumes(text) == PINNED_VOLUMES.get(rel, {})  # F-011 §3.4
    if rel.startswith("deploy/") and "smoke" not in rel:
        assert set(re.findall(r"\$\{([A-Z_]+)", text)) <= PINNED_ENV_KEYS


def test_br_131_compose_mutant_is_seen() -> None:
    text = (ROOT / "deploy" / "upstreams.compose.yaml").read_text(encoding="utf-8")
    mutant = text.replace("UPSTREAMS_NETWORK:-hullgate_upstreams", "UPSTREAMS_NETWORK:-x")
    assert _compose(mutant) != PINNED_COMPOSE["deploy/upstreams.compose.yaml"]


def test_br_131_compose_files_are_the_pinned_set() -> None:
    """`deploy/amonts.compose.yaml` is renamed (F-010 §3.2): no other compose file exists."""
    found = {
        str(p.relative_to(ROOT)) for p in [ROOT / "compose.yaml", *ROOT.glob("deploy/**/*.yaml")]
    }
    assert found == set(PINNED_COMPOSE)
    assert not (ROOT / "deploy" / "amonts.compose.yaml").exists()


def test_br_131_import_and_undo_root_arguments() -> None:
    groups = next(a for a in cli._parser()._actions if isinstance(a, argparse._SubParsersAction))
    pinned = {
        "import": {"--preview", "--apply", "--apply-deletions", "--confirm-share", "file"},
        "undo": {"seq", "--preview", "--impact", "--confirm-share"},
    }
    for group, expected in pinned.items():
        actual: set[str] = set()
        for action in groups.choices[group]._actions:
            if not isinstance(action, argparse._HelpAction):
                actual.update(action.option_strings or [action.dest])
        assert actual == expected
