"""AC-29: host recovery revokes every credential/session; never an HTTP act."""

from tests.admin_support import Console, SoftAuthenticator, link_token
from tests.policy_support import rows
from tests.support import Gateway


def test_br_228_recover_ends_sessions_and_restores_access(gw: Gateway) -> None:
    console = Console(gw)
    with console.client() as first, console.client() as second:
        old = console.admin(first)
        console.fresh(first, old)
        spare = SoftAuthenticator()
        assert console.add_passkey(first, spare).status_code == 201
        assert console.sign_in(second, spare).status_code == 200
        token = link_token(gw.ok("admin", "recover", "ops@example.test"))
        assert rows(gw, "SELECT count(*) FROM passkey") == [(0,)]
        assert rows(gw, "SELECT count(*) FROM admin_session") == [(0,)]
        assert rows(gw, "SELECT state FROM administrator") == [("active",)]
        for client in (first, second):
            assert console.call(client, "GET", "session").status_code == 401
        assert console.sign_in(first, old).status_code == 401
        assert console.sign_in(second, spare).status_code == 401
        new = SoftAuthenticator()
        assert console.enrol(first, token, new).status_code == 200
        assert console.sign_in(second, new).status_code == 200
    assert all("recover" not in r.path for r in console.app.api_routes)
    assert any(a["action"] == "admin.recover" for a in gw.audit())


def test_br_228_pending_refuses_and_reissue_replaces_link(gw: Gateway) -> None:
    console = Console(gw)
    pending = console.bootstrap()
    refused = gw.cli("admin", "recover", "ops@example.test")
    assert refused.code == 2
    assert "admin bootstrap" in refused.err
    with console.client() as client:
        console.enrol(client, pending, SoftAuthenticator())
        first = link_token(gw.ok("admin", "recover", "ops@example.test"))
        gw.clock.advance(1801)
        second = link_token(gw.ok("admin", "recover", "ops@example.test"))
        assert (
            console.call(client, "POST", "enrol/options", {"link": first}, csrf="").status_code
            == 404
        )
        assert console.enrol(client, second, SoftAuthenticator()).status_code == 200
