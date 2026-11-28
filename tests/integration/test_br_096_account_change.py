"""BR-096 — a reauthorization does not change the account silently (AC-30)."""

from __future__ import annotations

import json

from tests.support import Gateway, auth_url, standard_tree

ROLE = "mpx/cairn/staging/dev"
NAME = "mpx/gitea-dev"


def _reauthorize(gw: Gateway, account: str | None, *flags: str) -> tuple[int, str, str]:
    provider = gw.provider()
    url = auth_url(gw.ok("connection", "reauthorize", NAME))
    result = gw.cli(
        "connection",
        "authorize",
        NAME,
        "--callback",
        "-",
        *flags,
        stdin=provider.approve(url, account),
    )
    return result.code, result.out, result.err


def test_br_096_other_account_refused_then_confirmed(gw: Gateway) -> None:
    standard_tree(gw)
    provider = gw.oauth()
    gw.ok("connection", "place", NAME, ROLE)
    before = gw.access(NAME, ROLE).authorization
    blob = gw.row(NAME)["tokens_enc"]
    issued = len(provider.access_tokens)
    code, _out, err = _reauthorize(gw, "alice")
    assert code == 2
    assert "different account: cairn-ai → alice; add --confirm-account" in err
    assert gw.access(NAME, ROLE).authorization == before
    assert gw.row(NAME)["tokens_enc"] == blob  # no new token in the database
    newest = list(provider.access_tokens)[issued]
    assert newest in {token for _h, _c, token in provider.revoked}  # token obtained, revoked
    # unknown account (userinfo endpoint no longer answering): same refusal
    provider.userinfo = False
    code, _out, err = _reauthorize(gw, None)
    assert code == 2
    assert "cairn-ai → unknown" in err
    provider.userinfo = True
    # with --confirm-account: new account, audit before and after
    code, out, _err = _reauthorize(gw, "alice", "--confirm-account")
    assert code == 0
    assert "account alice" in out
    assert json.loads(gw.ok("connection", "status", NAME, "--json"))[0]["account"] == "alice"
    entry = [e for e in gw.audit() if e["action"] == "connection.authorize"][-1]
    assert json.loads(entry["before"])["account"] == "cairn-ai"
    assert json.loads(entry["after"])["account"] == "alice"
    assert "scopes" in json.loads(entry["before"])
    assert "scopes" in json.loads(entry["after"])
    assert entry["reason"] == "account changed, confirmed"
    refused = [e for e in gw.audit() if e["action"] == "connection.authorize_refused"]
    assert [e["reason"] for e in refused] == ["different account", "different account"]


def test_br_096_same_account_accepted_and_old_tokens_revoked(gw: Gateway) -> None:
    standard_tree(gw)
    provider = gw.oauth()
    old = gw.tokens(NAME)
    assert old is not None
    code, out, _err = _reauthorize(gw, "cairn-ai")
    assert code == 0
    assert "account cairn-ai" in out
    revoked = {token for _h, _c, token in provider.revoked}
    assert {old["access_token"], old["refresh_token"]} <= revoked
