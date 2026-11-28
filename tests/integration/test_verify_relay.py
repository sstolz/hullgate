"""F-008 AC-OP, instrument `make verify-relay` (`hullgate.verify_relay`): five checks, one exact
pattern per line, banner `VERIFY-RELAY: n/5 OK` on every path, no token in the output (F-010
§3.2: check names `list call refusal sensitive output`, verdicts `OK`/`KO`/`SKIP`)."""

from __future__ import annotations

import io
import re
from typing import Any

import pytest
from starlette.testclient import TestClient

from hullgate import verify_relay
from tests.relay_bed import A, Bed, S
from tests.support import Gateway, mcp, org

LINE = re.compile(
    r"^VERIFY-RELAY (?:\S+: (?:OK|KO .+|SKIP .+)|reading .+)$|^VERIFY-RELAY: \d/5 OK$"
)


def _post(client: TestClient) -> verify_relay.Post:
    def post(target: str, token: str, body: dict[str, Any]) -> tuple[int, Any]:
        # F-014: ROLE stays a tree path, the instrument posts to the new access point URL
        prefix = f"/o/{org()}/p/"
        assert target.startswith(prefix), target
        assert target.endswith("/mcp"), target
        role = target.removeprefix(prefix).removesuffix("/mcp")
        params = body.get("params")
        r = mcp(client, role, token, body["method"], params)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, None

    return post


def _run(
    gw: Gateway,
    capsys: pytest.CaptureFixture[str],
    client: TestClient,
    role: str,
    sensitive: str | None,
    tool: str | None,
    tokens: list[str],
) -> tuple[int, str]:
    code = verify_relay.run(role, sensitive, tool, tokens, gw.env, post=_post(client))
    return code, capsys.readouterr().out


def test_verify_relay_five_ok(gw: Gateway, capsys: pytest.CaptureFixture[str]) -> None:
    bed = Bed(gw)
    bed.connection("gd", A)
    with bed.serve() as client:
        tokens = [bed.token(A), bed.token(S)]
        code, out = _run(gw, capsys, client, A, S, "gd__read", tokens)
    assert code == 0, out
    lines = out.strip().splitlines()
    assert all(LINE.match(line) for line in lines), out
    assert lines[-1] == "VERIFY-RELAY: 5/5 OK"
    for check in verify_relay.CHECKS:
        assert f"VERIFY-RELAY {check}: OK" in lines
    assert "VERIFY-RELAY reading number of tools: 2" in lines
    assert "VERIFY-RELAY reading version negotiated with the upstream (audit): 2026-07-28" in lines
    assert not any(t in out for t in tokens)


def test_verify_relay_without_sensitive_role_or_tool(
    gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    bed = Bed(gw)
    bed.connection("gd", A)
    with bed.serve() as client:
        code, out = _run(gw, capsys, client, A, None, None, [bed.token(A)])
    assert code == 1
    assert "VERIFY-RELAY sensitive: SKIP no sensitive role named" in out
    assert "VERIFY-RELAY call: KO TOOL absent" in out
    assert out.strip().splitlines()[-1] == "VERIFY-RELAY: 3/5 OK"


def test_verify_relay_empty_list_and_missing_token(
    gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    bed = Bed(gw)
    with bed.serve() as client:
        code, out = _run(gw, capsys, client, A, None, "gd__read", [bed.token(A)])
        assert code == 1
        assert "VERIFY-RELAY list: KO no tool" in out
        code, out = _run(gw, capsys, client, A, None, None, [])
    assert "operator token absent" in out
    assert out.strip().splitlines()[-1] == "VERIFY-RELAY: 1/5 OK"


def test_verify_relay_main_reads_tokens_from_stdin(
    gw: Gateway, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("hg_" + "x" * 30 + "\n"))
    code = verify_relay.main(["mal/forme"])
    out = capsys.readouterr().out
    assert code == 1
    assert "ROLE absent or malformed" in out
    assert "hg_" not in out
    assert out.strip().splitlines()[-1] == "VERIFY-RELAY: 1/5 OK"


def test_verify_relay_leak_pattern_is_anchored() -> None:
    """F-011 AC-06 (BR-053): the `hg_` pattern holds a token back, and the shorter prefix does not
    fire inside another word (`xhg_` + 30 characters)."""
    assert verify_relay._LEAK.search("text hg_" + "Z9" * 15)
    assert verify_relay._LEAK.search("hg_" + "a" * 20)
    assert not verify_relay._LEAK.search("xhg_" + "a" * 30)
    assert not verify_relay._LEAK.search("_hg_" + "a" * 30)


@pytest.mark.parametrize("leak", ["operator", "prefix", "bearer"])
def test_verify_relay_holds_back_an_output_that_leaks(
    gw: Gateway, capsys: pytest.CaptureFixture[str], leak: str
) -> None:
    """Review 462, m3: the `isError` text of an upstream tool carries the operator token read, an
    `hg_…` string or `Bearer `; the output is withheld (four `KO output withheld` lines, `output:
    KO`), no other line is printed and the value does not reach the terminal."""
    bed = Bed(gw)
    bed.connection("gd", A)
    with bed.serve() as client:
        tokens = [bed.token(A)]
        secret = {"operator": tokens[0], "prefix": "hg_" + "Z9" * 15, "bearer": "Bearer abc"}[leak]
        honest = _post(client)

        def post(target: str, token: str, body: dict[str, Any]) -> tuple[int, Any]:
            if body["method"] == "tools/call" and body["params"]["name"] == "gd__read":
                result = {"content": [{"type": "text", "text": secret}], "isError": True}
                return 200, {"jsonrpc": "2.0", "id": 1, "result": result}
            return honest(target, token, body)

        code = verify_relay.run(A, None, "gd__read", tokens, gw.env, post=post)
        out = capsys.readouterr().out
    assert code == 1
    lines = out.strip().splitlines()
    expected = [f"VERIFY-RELAY {c}: KO output withheld" for c in verify_relay.CHECKS[:-1]]
    assert lines == [
        *expected,
        "VERIFY-RELAY output: KO secret value in the output",
        "VERIFY-RELAY: 0/5 OK",
    ], out
    assert secret not in out
    assert tokens[0] not in out
