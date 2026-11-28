"""F-010 AC-15: a tour of the CLI that plays every verb once on a seeded database (fake provider and
fake MCP upstream), and returns its captured stdout and stderr lines, normalised.

Volatile parts (identifiers, token values, timestamps, OAuth `state` and PKCE challenge, generated
client ids) are replaced by placeholders, so the lines can be pinned in `cli_output_lines.txt`.
"""

from __future__ import annotations

import re

from hullgate import oauth
from tests.admin_support import Console, SoftAuthenticator, link_token
from tests.fake_upstream import READ, tool
from tests.support import Gateway, auth_url

CONNECTION = "acme/gitea-dev"
SERVICE = "gitea-dev"
UPSTREAM = "https://tour.mcp.test/mcp"

VOLATILE = [
    (re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"), "<ts>"),
    (re.compile(r"(state|code_challenge|client_id|code)=[^&\s]+"), r"\1=<v>"),
    (re.compile(r"(?<![A-Za-z0-9_-])hg_[A-Za-z0-9_-]{20,}"), "<operator-token>"),
    (re.compile(r"\b(tok|lin)_[A-Za-z0-9_-]+"), r"\1_<id>"),
    (re.compile(r"\b[0-9a-f]{16,}\b"), "<hex>"),
    (re.compile(r"client_id [^\s,]+"), "client_id <client>"),
    (re.compile(r"client_id: [^\s,]+"), "client_id: <client>"),
    (re.compile(r"generation \d+"), "generation <n>"),
    (re.compile(r"\badm_[0-9a-f]+"), "adm_<id>"),  # F-016
    (re.compile(r"/admin/enrol/\S+"), "/admin/enrol/<link>"),
    # F-014: the organisation's public identifier, drawn at random by the migration (BR-160)
    (re.compile(r"/o/[a-z2-7]{12}/"), "/o/<org>/"),
    (re.compile(r"^(/  « .* »  organisation  )[a-z2-7]{12}$"), r"\1<org>"),
    (re.compile(r'^(  id: ")[a-z2-7]{12}(")$'), r"\1<org>\2"),
]


def normalise(line: str) -> str:
    for pattern, repl in VOLATILE:
        line = pattern.sub(repl, line)
    return line.rstrip()


def run_tour(gw: Gateway) -> tuple[list[str], set[tuple[str, str]]]:
    """Plays the tour; returns the normalised lines (stdout then stderr of each command, in order)
    and the set of (group, verb) played. Every command must succeed (exit code 0)."""
    lines: list[str] = []
    played: set[tuple[str, str]] = set()

    def run(*args: str, stdin: str = "") -> str:
        result = gw.cli(*args, stdin=stdin)
        assert result.code == 0, (args, result)
        played.add((args[0], args[1] if len(args) > 1 and args[0] != "export" else ""))
        lines.extend(normalise(x) for x in (result.out + result.err).splitlines())
        return result.out

    # tree
    run("tree", "add", "/", "client", "Acme")
    run("tree", "add", "acme", "project", "App")
    run("tree", "add", "acme/app", "environment", "Prod")
    run("tree", "add", "acme/app/prod", "role", "Ops")
    run("tree", "add", "acme", "project", "Lab")
    run("tree", "add", "acme/app", "env", "Old")
    run("tree", "rename", "acme/app/old", "Staging", "--slug", "staging")
    run("tree", "move", "acme/app/staging", "acme/lab", "--confirm", "acme/app/staging")
    run("tree", "delete", "acme/lab/staging", "--confirm", "acme/lab/staging")
    run("tree", "show")
    run("tree", "sensitive", "acme/app/prod", "on")
    run("tree", "sensitive", "acme/app/prod", "off")
    run("tree", "lock", "acme/app")
    run("tree", "unlock", "acme/app")
    # settings
    run("settings", "set", "acme", "token_duration", "10d")
    run("settings", "get", "acme/app")
    run("settings", "unset", "acme", "token_duration")
    # token
    out = run("token", "create", "--path", "acme/app", "--label", "tour", "--duration", "2d")
    token_id = next(x.split(": ", 1)[1] for x in out.splitlines() if x.startswith("id: "))
    run("token", "list")
    run("token", "revoke", token_id)
    # client
    run("client", "add", "tour-client", "--path", "acme", "--redirect-uri", "http://localhost/cb")
    run("client", "list")
    run("client", "dcr", "on")
    run("client", "remove", "tour-client")
    # service, application, connection
    gw.provider(SERVICE)
    run("service", "declare", SERVICE, "--server", f"https://{SERVICE}.example.test",
        "--model", "gitea", "--scopes", "write:issue")  # fmt: skip
    run("service", "rediscover", SERVICE, "--confirm", SERVICE)
    up = gw.upnet.upstream("tour.mcp.test", "modern")
    method = {
        "type": "object",
        "properties": {"method": {"type": "string", "enum": ["create", "merge"]}},
    }
    up.tools = [tool("read", annotations=READ), tool("write", method)]  # F-015 tour
    run("service", "upstream", SERVICE, "--type", "mcp-public", "--mcp-url", UPSTREAM)
    run("service", "list")
    run("service", "declare", "spare", "--server", "https://spare.example.test", "--model",
        "generic")  # fmt: skip
    run("service", "remove", "spare")
    run("application", "declare", "app", "--service", SERVICE, "--mode", "declared",
        "--client-id", "client-app")  # fmt: skip
    run("application", "declare", "dyn", "--service", SERVICE, "--mode", "dynamic")
    run("application", "list")
    run("application", "reregister", "dyn", "--confirm", "dyn")
    run("application", "remove", "dyn")
    run("connection", "add", CONNECTION, "--service", SERVICE, "--provided", "--file", "-",
        "--prefix", "gd", stdin="tok_tour-value-for-the-cli-tour\n")  # fmt: skip
    run("connection", "prefix", CONNECTION, "gt")
    run("connection", "place", CONNECTION, "acme/app/prod/ops")
    run("connection", "tools", CONNECTION, "--role", "acme/app/prod/ops")
    # policy, tool (F-015)
    ops = ("--role", "acme/app/prod/ops")
    run("tool", "queue")
    run("tool", "classify", SERVICE, "write", "destructive")
    run("tool", "accept", SERVICE, "read")
    run("policy", "mode", CONNECTION, *ops, "read")
    run("policy", "mode", CONNECTION, *ops, "inherit")
    run("policy", "add", CONNECTION, *ops, "allowlist", "--class", "read")
    run("policy", "add", CONNECTION, *ops, "open", "write")
    run("policy", "remove", CONNECTION, *ops, "allowlist", "read")
    run("policy", "add", CONNECTION, *ops, "deny", "read")
    run("policy", "pin", CONNECTION, *ops, "on")
    run("policy", "arg", CONNECTION, *ops, "write", "method", "create")  # entry listing: baselines
    run("policy", "pin-tool", CONNECTION, *ops, "read")
    run("policy", "show", CONNECTION, *ops)
    run("policy", "preview", "acme/app/prod/ops")
    up.tools[0]["description"] = "tool read, v2"  # a pinned definition changes upstream
    run("tool", "diff", "acme/app")
    run("tool", "validate", "acme/app", CONNECTION)
    run("policy", "unpin-tool", CONNECTION, *ops, "read")
    run("policy", "pin", CONNECTION, *ops, "inherit")
    run("policy", "arg-remove", CONNECTION, *ops, "write")
    run("connection", "unplace", CONNECTION, "acme/app/prod/ops")
    provider = gw.provider(SERVICE)
    out = run("connection", "add", "acme/oauth", "--service", SERVICE, "--application", "app",
              "--prefix", "go")  # fmt: skip
    run("connection", "authorize", "acme/oauth", "--callback", "-",
        stdin=provider.approve(auth_url(out), None) + "\n")  # fmt: skip
    run("connection", "renew", "acme/oauth")
    run("connection", "status")
    run("connection", "list")
    run("connection", "revoke", "acme/oauth")
    run("connection", "reauthorize", "acme/oauth")
    run("connection", "remove", "acme/oauth", "--confirm", "acme/oauth")
    # audit, export
    run("audit", "tail", "-n", "3")
    run("export")
    # admin (F-016 lot A), after the audit tail so that its lines stay as pinned
    link = link_token(run("admin", "bootstrap", "ops@example.test", "--name", "Ops"))
    run("admin", "list")
    run("admin", "attempts", "clear")
    run("admin", "allowlist", "clear")
    console = Console(gw)
    with console.client() as client:
        assert console.enrol(client, link, SoftAuthenticator()).status_code == 200
    link_token(run("admin", "recover", "ops@example.test"))
    # consent (F-020, BR-310): one pending request, written as `authorize` would
    conn = gw.connect()
    try:
        role = conn.execute("SELECT id FROM node WHERE slug = 'ops'").fetchone()[0]
        now = int(gw.clock())
        conn.execute(
            "INSERT INTO consent_request (hash, role_id, client_id, client_kind, redirect_uri,"
            " code_challenge, resource, scope, created_at, expires_at)"
            " VALUES (?, ?, 'tour-client', 'preregistered', 'http://localhost:4567/cb', 'c',"
            " 'r', 'mcp', ?, ?)",
            ("0" * 64, role, now, now + 600),
        )
        conn.commit()
    finally:
        conn.close()
    run("consent", "list")
    run("consent", "approve", oauth.reference("0" * 64))
    return lines, played
