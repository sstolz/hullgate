"""BR-131 (F-010 AC-05): the nine names the gateway shows to clients, upstreams and authenticators
read `hullgate`, each asserted positively (sweep G only proves the old forms are gone)."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from hullgate import keys, oauth, outbound
from hullgate.outbound import Failure, Outbound
from tests.fake_upstream import Fault
from tests.integration import test_br_120_progress_cancel as t120
from tests.relay_bed import A, Bed
from tests.support import Gateway, mcp, org, standard_tree

ROLE = "mpx/cairn/staging/dev"


@pytest.fixture
def bed(gw: Gateway) -> Bed:
    return Bed(gw)


def test_br_131_product_name_shown_to_clients(gw: Gateway, tmp_path: Path) -> None:
    standard_tree(gw)
    _, token = gw.issue(ROLE)
    with gw.client() as client:
        assert isinstance(client, TestClient)
        # (1) MCP serverInfo.name
        init = mcp(client, ROLE, token, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        }, version="2025-06-18")  # fmt: skip
        assert init.json()["result"]["serverInfo"]["name"] == "hullgate"
        # (2) OAuth resource_name
        prm = client.get(f"/.well-known/oauth-protected-resource/o/{org()}/p/{ROLE}/mcp").json()
        assert prm["resource_name"].startswith("hullgate ")
    # (3) TOTP issuer
    assert "issuer=hullgate&" in keys.generate_totp_secret(str(tmp_path / "totp"), "x").detail
    # (4) consent cookie
    assert oauth.CONSENT_COOKIE == "hullgate_consent"
    # (5) export header
    assert gw.ok("export").startswith("# hullgate: ")


def test_br_131_product_name_outbound_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    def stop(self: Outbound, method: str, host: str, port: int, target: str,
             headers: dict[str, str], *rest: object) -> None:  # fmt: skip
        seen.update(headers)
        raise Failure(outbound.TRANSIENT, "stop")

    monkeypatch.setattr(Outbound, "_exchange", stop)
    with pytest.raises(Failure):
        Outbound().request("GET", "https://provider.example.test/")
    # (6) outbound OAuth client User-Agent
    assert seen["User-Agent"] == "hullgate"


def test_br_131_product_name_shown_to_upstream(bed: Bed) -> None:
    up = bed.connection("pg", A, generation="session")
    with t120.served(bed) as base:
        token = bed.token(A)
        assert "pg__read" in t120._announce(base, token)
        up.faults.append(Fault("progress", on="tools/call"))
        t120._call(base, A, token, "pg__read", {"q": "x"}, t120.LEGACY, "client-token")
    (init,) = up.calls("initialize")
    assert init.body is not None
    # (7) upstream clientInfo.name
    assert init.body["params"]["clientInfo"]["name"] == "hullgate"
    # (8) upstream User-Agent
    assert init.headers["user-agent"] == "hullgate"
    # (9) progress-token prefix
    (call,) = up.calls("tools/call")
    assert call.body is not None
    assert str(call.body["params"]["_meta"]["progressToken"]).startswith("hullgate-")
