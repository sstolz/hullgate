"""BR-161 — a route answers only under the instance's own identifier (F-014 AC-06, AC-07, AC-09).

Per route family (access point, the five authorization actions, RFC 9728, RFC 8414): every
non-canonical form answers 404 with no `Location` and no audit entry; on each route a foreign
well-formed identifier answers byte for byte as an unknown role under the right one; with the
store unreadable, every well-formed route answers 503, malformed ones stay 404. Tokens bound to a
pre-feature URL are refused at the new URLs without any revocation.

Transport (AC-06): an HTTP client normalises what it is given, so every non-canonical form is sent
as a hand-built ASGI scope, `raw_path` holding the exact bytes and `path` decoded as uvicorn
decodes it (`unquote(raw_path.decode("ascii"))`); canonical forms go through `TestClient`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlencode

import anyio
import pytest
from starlette.testclient import TestClient
from starlette.types import ASGIApp, Message

from hullgate import access_tokens
from tests.support import (
    CLIENT_ID,
    PUBLIC_URL,
    Gateway,
    grant,
    mcp,
    refresh,
    resource,
    standard_tree,
    token_request,
)

ROLE = "mpx/cairn/staging/dev"
UNKNOWN_ROLE = "mpx/cairn/staging/nobody"
FOREIGN = "aaaaaaaaaaaa"  # well-formed, never the instance's (its draw is checked below)


@dataclass(frozen=True)
class Family:
    """A route family: `before` + `/o/<id>` + `after` with `{path}` filled in."""

    name: str
    method: str
    before: str
    after: str

    def raw(self, org: str, path: str = ROLE) -> str:
        return f"{self.before}/o/{org}{self.after.format(path=path)}"

    def old(self, path: str = ROLE) -> str:
        """The form in force before F-014."""
        return f"{self.before}{self.after.format(path=path)}"

    def without_suffix(self, org: str) -> str:
        """The canonical form with its last component dropped."""
        return self.raw(org).rsplit("/", 1)[0]


FAMILIES = [
    Family("access point", "POST", "", "/p/{path}/mcp"),
    *(
        Family(f"oauth {action}", method, "", f"/oauth/p/{{path}}/{action}")
        for action, method in (
            ("authorize", "GET"),
            ("consent", "GET"),
            ("token", "POST"),
            ("revoke", "POST"),
            ("register", "POST"),
        )
    ),
    Family("rfc 9728", "GET", "/.well-known/oauth-protected-resource", "/p/{path}/mcp"),
    Family("rfc 8414", "GET", "/.well-known/oauth-authorization-server", "/oauth/p/{path}"),
]
IDS = [f.name for f in FAMILIES]


def _pct(char: str) -> str:
    return f"%{ord(char):02X}"


def _forms(f: Family, org: str) -> dict[str, bytes | None]:
    """Every non-canonical form of AC-06, as raw path bytes (None: no `raw_path` in the scope)."""
    canonical = f.raw(org)
    after = f.after.format(path=ROLE)
    pct_path = ROLE.replace("/", "%2F", 1)
    forms = {
        "pre-feature path": f.old(),
        "foreign identifier": f.raw(FOREIGN),
        "uppercase identifier": f.raw(org.upper()),
        "11 characters": f.raw(org[:11]),
        "13 characters": f.raw(org + "a"),
        "character outside the alphabet": f.raw(org[:11] + "1"),
        "doubled prefix": f"{f.before}/o/{org}/o/{org}{after}",
        "double slash": f"{f.before}/o/{org}/{after}",
        "dot dot": f"{f.before}/o/{org}/..{after}",
        "trailing slash": canonical + "/",
        "suffix dropped": f.without_suffix(org),
        "%2F in the prefix (o)": f"{f.before}/o%2F{org}{after}",
        "%2F in the prefix (after the identifier)": f"{f.before}/o/{org}%2F{after[1:]}",
        "%2F inside the path": f.raw(org, pct_path),
        "percent-encoded identifier character": f.raw(_pct(org[0]) + org[1:]),
        "percent-encoded path character": f.raw(org, _pct(ROLE[0]) + ROLE[1:]),
    }
    out: dict[str, bytes | None] = {k: v.encode("ascii") for k, v in forms.items()}
    out["no raw_path"] = None
    out["non-ASCII raw_path"] = canonical.replace(ROLE, "mpx/cairn/staging/dév").encode("utf-8")
    return out


@dataclass
class Answer:
    status: int
    headers: list[tuple[bytes, bytes]]
    body: bytes

    def header(self, name: str) -> str | None:
        found = [v for k, v in self.headers if k.lower() == name.encode("ascii")]
        return found[0].decode("latin-1") if found else None


def _send(app: ASGIApp, method: str, raw: bytes | None, path: str | None = None) -> Answer:
    """One request through a hand-built scope: `raw_path` exact, `path` decoded as uvicorn."""
    decoded = path if path is not None else unquote((raw or b"").decode("latin-1"))
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": decoded,
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"gw.example.test"),
            (b"content-type", b"application/x-www-form-urlencoded"),
            (b"accept", b"application/json, text/event-stream"),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("gw.example.test", 443),
    }
    if raw is not None:
        scope["raw_path"] = raw
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    anyio.run(app, scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return Answer(start["status"], list(start.get("headers", [])), body)


def _audit_count(gw: Gateway) -> int:
    conn = sqlite3.connect(gw.path)
    try:
        return int(conn.execute("SELECT count(*) FROM audit").fetchone()[0])
    finally:
        conn.close()


@pytest.fixture
def bed(gw: Gateway) -> Iterator[tuple[Gateway, TestClient]]:
    standard_tree(gw)
    gw.preregister()
    gw.ok("client", "dcr", "on")  # `register` served, so its canonical form is not a 404
    assert gw.org_id != FOREIGN
    with gw.client() as client:
        yield gw, client


def _canonical(client: TestClient, f: Family, raw: str) -> Any:
    if f.method == "GET":
        return client.get(raw)
    return client.post(raw, content=b"", headers={"Content-Type": "application/json"})


# -- AC-06 -------------------------------------------------------------------------------------


@pytest.mark.parametrize("family", FAMILIES, ids=IDS)
def test_br_161_canonical_form_reaches_the_route(
    bed: tuple[Gateway, TestClient], family: Family
) -> None:
    """Control: the canonical form, sent through the same hand-built transport, is not a 404."""
    gw, client = bed
    answer = _send(client.app, family.method, family.raw(gw.org_id).encode("ascii"))
    assert answer.status != 404, (family.name, answer)


@pytest.mark.parametrize("family", FAMILIES, ids=IDS)
def test_br_161_every_other_form_is_404_without_location(
    bed: tuple[Gateway, TestClient], family: Family
) -> None:
    gw, client = bed
    before = _audit_count(gw)
    forms = _forms(family, gw.org_id)
    assert len(forms) == 18
    for name, raw in forms.items():
        path = family.raw(gw.org_id) if raw is None else None  # no raw_path: `path` canonical
        answer = _send(client.app, family.method, raw, path)
        assert answer.status == 404, (family.name, name, raw, answer)
        assert answer.header("location") is None, (family.name, name)
        assert gw.org_id.encode("ascii") not in answer.body, (family.name, name)
    assert _audit_count(gw) == before  # as for any unknown path today: no audit entry


@pytest.mark.parametrize(
    "raw", ["/o/{id}", "/o/{id}/", "/o/{id}/mcp", "/o/{id}/p", "/o/", "/o", "/p/", "/oauth/p/"]
)
def test_br_161_bare_prefix_is_404(bed: tuple[Gateway, TestClient], raw: str) -> None:
    gw, client = bed
    for method in ("GET", "POST"):
        answer = _send(client.app, method, raw.format(id=gw.org_id).encode("ascii"))
        assert answer.status == 404, (raw, method)
        assert answer.header("location") is None


@pytest.mark.parametrize("family", FAMILIES, ids=IDS)
def test_br_161_foreign_identifier_is_byte_identical_to_unknown_role(
    bed: tuple[Gateway, TestClient], family: Family
) -> None:
    gw, client = bed
    foreign = _canonical(client, family, family.raw(FOREIGN))
    unknown = _canonical(client, family, family.raw(gw.org_id, UNKNOWN_ROLE))
    assert foreign.status_code == unknown.status_code == 404
    assert foreign.content == unknown.content
    assert foreign.headers.multi_items() == unknown.headers.multi_items()
    assert "location" not in foreign.headers


# -- AC-07 -------------------------------------------------------------------------------------


@contextmanager
def _store_down(gw: Gateway) -> Iterator[None]:
    gw.db.close()
    try:
        yield
    finally:
        assert gw.db.open()


@pytest.mark.parametrize("family", FAMILIES, ids=IDS)
def test_br_161_store_unreadable_is_503_before_any_identifier_404(
    bed: tuple[Gateway, TestClient], family: Family
) -> None:
    gw, client = bed
    with _store_down(gw):
        for org in (gw.org_id, FOREIGN):
            answer = _canonical(client, family, family.raw(org))
            assert answer.status_code == 503, (family.name, org, answer.text)
            assert answer.json() == {"error": "temporarily_unavailable"}
        for name in ("uppercase identifier", "11 characters", "pre-feature path", "dot dot"):
            raw = _forms(family, gw.org_id)[name]
            assert _send(client.app, family.method, raw).status == 404, (family.name, name)
        assert _send(client.app, "POST", f"/o/{gw.org_id}/mcp".encode("ascii")).status == 404
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 503  # answers at the root (BR-068)


# -- AC-04 and AC-05 on the new URLs ----------------------------------------------------------


def test_br_161_metadata_and_jwt_carry_the_prefix(bed: tuple[Gateway, TestClient]) -> None:
    gw, client = bed
    org = gw.org_id
    rs = client.get(f"/.well-known/oauth-protected-resource/o/{org}/p/{ROLE}/mcp").json()
    issuer = f"{PUBLIC_URL}/o/{org}/oauth/p/{ROLE}"
    assert rs["resource"] == f"{PUBLIC_URL}/o/{org}/p/{ROLE}/mcp"
    assert rs["authorization_servers"] == [issuer]
    server = client.get(f"/.well-known/oauth-authorization-server/o/{org}/oauth/p/{ROLE}").json()
    assert server["issuer"] == issuer
    assert server["token_endpoint"] == f"{issuer}/token"
    challenge = mcp(client, ROLE)
    assert challenge.status_code == 401
    metadata = f"{PUBLIC_URL}/.well-known/oauth-protected-resource/o/{org}/p/{ROLE}/mcp"
    assert f'resource_metadata="{metadata}"' in challenge.headers["www-authenticate"]
    body = grant(gw, client, ROLE)
    claims = access_tokens.verify(gw.key, body["access_token"], PUBLIC_URL, gw.clock())
    assert claims is not None
    assert claims["iss"] == issuer
    assert claims["aud"] == rs["resource"]
    assert mcp(client, ROLE, body["access_token"]).status_code == 200


# -- AC-09 -------------------------------------------------------------------------------------


def _old_urls(path: str) -> tuple[str, str]:
    return f"{PUBLIC_URL}/oauth/p/{path}", f"{PUBLIC_URL}/p/{path}/mcp"


def _pre_feature_tokens(gw: Gateway, client: TestClient) -> tuple[str, str, str]:
    """An access token and a refresh bound to the pre-feature URL: a lineage as issued before F-014
    (its `resource` the old URL) and a JWT whose `iss` and `aud` are the old forms."""
    body = grant(gw, client, ROLE)
    old_iss, old_aud = _old_urls(ROLE)
    conn = sqlite3.connect(gw.path)
    try:
        lineage_id, role_id = conn.execute(
            "SELECT id, role_id FROM lineage ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        conn.execute("UPDATE lineage SET resource = ? WHERE id = ?", (old_aud, lineage_id))
        token, _ = access_tokens.issue(
            conn,
            gw.key,
            issuer=old_iss,
            audience=old_aud,
            role_id=role_id,
            client_id=CLIENT_ID,
            scope="mcp offline_access",
            lineage_id=lineage_id,
            now=int(gw.clock()),
        )
        conn.commit()
    finally:
        conn.close()
    return token, str(body["refresh_token"]), lineage_id


def _refresh_used(gw: Gateway) -> list[int]:
    conn = sqlite3.connect(gw.path)
    try:
        return [int(r[0]) for r in conn.execute("SELECT used FROM refresh ORDER BY created_at")]
    finally:
        conn.close()


def _revoked(gw: Gateway, lineage_id: str) -> int:
    conn = sqlite3.connect(gw.path)
    try:
        return int(
            conn.execute("SELECT revoked FROM lineage WHERE id = ?", (lineage_id,)).fetchone()[0]
        )
    finally:
        conn.close()


def test_br_161_pre_feature_tokens_are_refused_without_revocation(
    bed: tuple[Gateway, TestClient],
) -> None:
    gw, client = bed
    access, refresh_value, lineage_id = _pre_feature_tokens(gw, client)
    response = mcp(client, ROLE, access)
    assert response.status_code == 401
    assert 'error="invalid_token"' in response.headers["www-authenticate"]
    old_resource = _old_urls(ROLE)[1]
    form: dict[str, str | list[str]] = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_value,
        "client_id": CLIENT_ID,
        "resource": old_resource,
    }
    for answer in (token_request(client, ROLE, form), refresh(client, ROLE, refresh_value)):
        assert answer.status_code == 400, answer.text
        assert answer.json()["error"] == "invalid_target"
    assert _refresh_used(gw) == [0]  # neither input consumed the refresh
    assert _revoked(gw, lineage_id) == 0  # nothing was revoked


def test_br_161_operator_token_issued_before_is_accepted_at_the_new_url(
    bed: tuple[Gateway, TestClient],
) -> None:
    gw, client = bed
    _, value = gw.issue(ROLE)  # bound to a tree path, not to a URL (BR-050)
    assert mcp(client, ROLE, value).status_code == 200
    old = client.post(_old_urls(ROLE)[1].removeprefix(PUBLIC_URL), headers={"Authorization": "x"})
    assert old.status_code == 404


def test_br_161_query_string_is_not_part_of_the_route(bed: tuple[Gateway, TestClient]) -> None:
    gw, client = bed
    raw = f"/o/{gw.org_id}/oauth/p/{ROLE}/authorize"
    assert client.get(f"{raw}?{urlencode({'client_id': CLIENT_ID})}").status_code != 404
    assert _send(client.app, "GET", f"{raw}%3Fx".encode("ascii")).status == 404


def test_br_161_resource_helper_states_the_new_form(bed: tuple[Gateway, TestClient]) -> None:
    gw, _ = bed
    assert resource(ROLE) == f"{PUBLIC_URL}/o/{gw.org_id}/p/{ROLE}/mcp"
