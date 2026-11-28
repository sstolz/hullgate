"""BR-216, BR-082 v3 (AC-15) — a connection is authorised from the console, with the code out of
every log.

Against the fake provider: an application with its web return registered opens the authorisation
with `${ADMIN_URL}/admin/oauth/callback`, the console's callback route posts `state` and `code`
under the session, and the exchange sends the `redirect_uri` chosen at opening (the fake provider
refuses any other). Without the web return the paste path of F-006 stays. The callback route of the
console is the data-free shell and writes nothing; the admin listener writes no access log.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from hullgate import __main__ as entry
from hullgate import config, connections
from tests import fake_provider
from tests.admin_support import ADMIN_URL, Console, bundle, counts
from tests.support import Gateway, standard_tree

WEB = f"{ADMIN_URL}/admin/oauth/callback"


def _redirect(url: str) -> str:
    return parse_qs(urlsplit(url).query)["redirect_uri"][0]


def _setup(gw: Gateway) -> fake_provider.Provider:
    standard_tree(gw)
    gw.service()
    gw.application()
    return gw.provider()


def test_br_216_web_return_flow_ends_in_an_active_connection(gw: Gateway) -> None:
    provider = _setup(gw)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        meta = console.call(client, "GET", "meta").json()
        assert meta["features"]["web_return"] is True
        assert meta["web_redirect_uri"] == WEB
        flagged = console.call(client, "PUT", "applications/web-return",
                               {"id": "app", "registered": True})  # fmt: skip
        assert flagged.json() == {"id": "app", "web_return": True}
        added = console.call(client, "POST", "connections", {
            "name": "mpx/gitea-dev", "service": "gitea-dev", "application": "app", "prefix": "gd",
        })  # fmt: skip
        assert added.status_code == 201, added.text
        assert added.json()["redirect"] == "web"
        url = added.json()["authorization_url"]
        assert _redirect(url) == WEB
        back = urlsplit(provider.approve(url))
        assert f"{back.scheme}://{back.netloc}{back.path}" == WEB
        query = {k: v[0] for k, v in parse_qs(back.query).items()}
        done = console.call(client, "POST", "connections/callback",
                            {"state": query["state"], "code": query["code"]})  # fmt: skip
        assert done.status_code == 200, done.text
        listed = console.call(client, "GET", "connections").json()["connections"]
    [view] = listed
    assert view["name"] == "mpx/gitea-dev"
    assert view["stored_state"] == "active"
    assert view["web_return"] is True
    [exchange] = provider.calls(fake_provider.TOKEN, "authorization_code")
    assert exchange.form["redirect_uri"] == WEB
    actors = {e["actor"] for e in gw.audit() if e["action"] == "connection.authorize"}
    assert len(actors) == 1
    assert next(iter(actors)).startswith("admin:")


def test_br_216_without_web_return_the_paste_path_stays(gw: Gateway) -> None:
    provider = _setup(gw)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        added = console.call(client, "POST", "connections", {
            "name": "mpx/gitea-dev", "service": "gitea-dev", "application": "app", "prefix": "gd",
        }).json()  # fmt: skip
        assert added["redirect"] == "paste"
        assert _redirect(added["authorization_url"]) == connections.REDIRECT_URI
        pasted = console.call(client, "POST", "connections/authorize", {
            "name": "mpx/gitea-dev", "callback": provider.approve(added["authorization_url"]),
        })  # fmt: skip
        assert pasted.status_code == 200, pasted.text
    [exchange] = provider.calls(fake_provider.TOKEN, "authorization_code")
    assert exchange.form["redirect_uri"] == connections.REDIRECT_URI


def test_br_216_a_state_is_used_once(gw: Gateway) -> None:
    provider = _setup(gw)
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        console.call(client, "PUT", "applications/web-return", {"id": "app", "registered": True})
        url = console.call(client, "POST", "connections", {
            "name": "mpx/gitea-dev", "service": "gitea-dev", "application": "app", "prefix": "gd",
        }).json()["authorization_url"]  # fmt: skip
        query = {k: v[0] for k, v in parse_qs(urlsplit(provider.approve(url)).query).items()}
        body = {"state": query["state"], "code": query["code"]}
        assert console.call(client, "POST", "connections/callback", body).status_code == 200
        again = console.call(client, "POST", "connections/callback", body)
        unknown = console.call(client, "POST", "connections/callback", {"state": "x", "code": "y"})
    assert again.status_code == 409
    assert unknown.status_code == 409
    assert unknown.json()["code"] == "refused"
    assert query["code"] not in again.text + unknown.text


def test_br_216_the_callback_route_needs_the_session(gw: Gateway) -> None:
    """The provider's redirect carries no `SameSite=Strict` cookie: the route itself has no
    effect, and the API refuses the POST without a session."""
    console = Console(gw)
    with console.client() as client:
        response = console.call(client, "POST", "connections/callback", {"state": "s", "code": "c"})
    assert response.status_code == 401


def test_br_216_the_console_route_is_the_shell_and_writes_nothing(
    gw: Gateway, tmp_path: Path
) -> None:
    console = Console(gw, static_dir=bundle(tmp_path))
    with console.client() as client:
        before = counts(gw)
        page = client.get("/admin/oauth/callback?code=secret-code&state=s")
        assert counts(gw) == before
    assert page.status_code == 200
    assert "secret-code" not in page.text
    assert page.headers["referrer-policy"] == "no-referrer"


def test_br_216_the_admin_listener_writes_no_access_log(gw: Gateway) -> None:
    cfg = config.load({**gw.env, "SIGNING_KEY_FILE": str(gw.key_path)})
    _public, admin = entry.configs(cfg, gw.db)
    assert admin.access_log is False


def test_br_216_dynamic_registration_declares_both_uris(gw: Gateway) -> None:
    standard_tree(gw)
    gw.service()
    provider = gw.provider()
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        made = console.call(client, "POST", "applications",
                            {"id": "dyn", "service": "gitea-dev", "mode": "dynamic"})  # fmt: skip
        assert made.status_code == 201, made.text
        assert made.json()["application"]["web_return"] is True
    [register] = provider.calls(fake_provider.REGISTER)
    assert register.body["redirect_uris"] == [connections.REDIRECT_URI, WEB]
