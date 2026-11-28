"""BR-201 (AC-01) — the first administrator comes from the host, and only from it."""

from __future__ import annotations

from pathlib import Path

from hullgate.admin import accounts
from tests.admin_support import LINK, Console, SoftAuthenticator, bundle, counts
from tests.support import Gateway


def _admins(gw: Gateway) -> list[tuple[str, str]]:
    conn = gw.connect()
    try:
        return [(r[0], r[1]) for r in conn.execute("SELECT email, state FROM administrator")]
    finally:
        conn.close()


def test_br_201_fresh_instance_offers_no_account(gw: Gateway, tmp_path: Path) -> None:
    console = Console(gw, static_dir=bundle(tmp_path))
    with console.client() as client:
        shell = client.get("/admin")
        assert shell.status_code == 200
        assert "<div id=app>" in shell.text
        # every sign-in fails: no credential is registered, and nothing creates one
        response = console.sign_in(client, SoftAuthenticator())
        assert response.status_code == 401
        assert response.json()["code"] == "sign_in_failed"
    assert _admins(gw) == []


def test_br_201_bootstrap_prints_the_link_once(gw: Gateway) -> None:
    out = gw.ok("admin", "bootstrap", "Ops@Example.test")
    assert len(LINK.findall(out)) == 1
    assert LINK.findall(out)[0].startswith("http://localhost:14791/admin/enrol/")
    assert _admins(gw) == [("ops@example.test", "pending")]
    listed = gw.ok("admin", "list")
    assert "ops@example.test" in listed
    assert "ENROLMENT-LINK" not in listed
    assert accounts.ENROL_PATH not in listed


def test_br_201_rerun_before_enrolment_replaces_the_link(gw: Gateway) -> None:
    console = Console(gw)
    first = console.bootstrap()
    second = console.bootstrap()
    with console.client() as client:
        old = console.call(client, "POST", "enrol/options", {"link": first}, csrf="")
        assert old.status_code == 404
        assert console.enrol(client, second, SoftAuthenticator()).status_code == 200
    assert _admins(gw) == [("ops@example.test", "active")]


def test_br_201_rerun_on_another_email_replaces_the_pending_account(gw: Gateway) -> None:
    console = Console(gw)
    typo = console.bootstrap("osp@example.test")
    good = console.bootstrap("ops@example.test")
    assert _admins(gw) == [("ops@example.test", "pending")]
    with console.client() as client:
        assert (
            console.call(client, "POST", "enrol/options", {"link": typo}, csrf="").status_code
            == 404
        )
        assert console.enrol(client, good, SoftAuthenticator()).status_code == 200
    removed = [e for e in gw.audit() if e["action"] == "admin.remove"]
    assert len(removed) == 1
    assert removed[0]["reason"] == "replaced_at_bootstrap"


def test_br_201_run_after_enrolment_is_refused(gw: Gateway) -> None:
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
    result = gw.cli("admin", "bootstrap", "other@example.test")
    assert result.code == 2
    assert "active administrator exists" in result.err
    assert "ENROLMENT-LINK" not in result.out
    assert _admins(gw) == [("ops@example.test", "active")]


def test_br_201_no_route_adds_or_removes_an_administrator(gw: Gateway) -> None:
    """Every write route, called by a signed-in administrator, leaves the accounts as they are."""
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        before = (counts(gw)["administrator"], _admins(gw))
        for spec in console.app.api_routes:
            if spec.method == "GET" or spec.path == "session/signout":
                continue
            path = spec.path.replace("{credential_id}", "x")
            for body in ({}, {"email": "intruder@example.test", "state": "active"}):
                console.call(client, spec.method, path, body)
        assert (counts(gw)["administrator"], _admins(gw)) == before
    assert not any("admin" in s.path.split("/")[0] for s in console.app.api_routes)
