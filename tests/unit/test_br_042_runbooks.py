"""BR-042 — each operator gesture on the server is a copy-paste runbook.

Checked form: each step is a `## Step <org-id> — <title>` section that carries at least one code
block, an "Expected result" line and an "If the result differs" line.
The installation covers O1 to O9 (§6.1). Commands are read in
review and at acceptance (AC-OP).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNBOOKS = {
    "installation": ROOT / "docs/guides/install.md",
    "mise-a-jour": ROOT / "docs/guides/update.md",
    "sauvegarde": ROOT / "docs/guides/backup-restore.md",
}
STEP = re.compile(r"^## Step (\S+) — .+$", re.MULTILINE)
REQUIRED = ("**Expected result**", "**If the result differs**")


def _steps(text: str) -> dict[str, str]:
    matches = list(STEP.finditer(text))
    steps: dict[str, str] = {}
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[match.end() : end]
        body = body.split("\n## ", 1)[0]
        steps[match.group(1)] = body
    return steps


@pytest.mark.parametrize("name", sorted(RUNBOOKS))
def test_br_042_runbook_exists_with_steps(name: str) -> None:
    path = RUNBOOKS[name]
    assert path.is_file(), path
    assert _steps(path.read_text(encoding="utf-8")), f"{path.name}: no step"


@pytest.mark.parametrize("name", sorted(RUNBOOKS))
def test_br_042_every_step_is_complete(name: str) -> None:
    steps = _steps(RUNBOOKS[name].read_text(encoding="utf-8"))
    for step_id, body in steps.items():
        assert re.search(r"^```[a-z]*\n.+?\n```$", body, re.MULTILINE | re.DOTALL), (
            f"{name} step {step_id}: code block absent"
        )
        for marker in REQUIRED:
            assert marker in body, f'{name} step {step_id}: "{marker}" absent'


def test_br_042_installation_covers_o1_to_o9() -> None:
    steps = _steps(RUNBOOKS["installation"].read_text(encoding="utf-8"))
    assert [s for s in steps if s.startswith("O")] == [f"O{n}" for n in range(1, 10)]


def test_br_042_fresh_start_steps_f1_to_f6() -> None:
    """F-011 §3.2, AC-10: the update runbook carries M1 to M4, then the fresh start F1 to F6; the
    old volume is removed in F6 only, in a block of its own, behind printed checks."""
    text = RUNBOOKS["mise-a-jour"].read_text(encoding="utf-8")
    steps = _steps(text)
    assert list(steps) == [f"M{n}" for n in range(1, 5)] + [f"F{n}" for n in range(1, 7)]
    removals = [s for s, body in steps.items() if "docker volume rm" in body]
    assert removals == ["F6"]
    [f6] = re.findall(r"^```sh\n(.+?)\n```$", steps["F6"], re.MULTILINE | re.DOTALL)
    assert f6.index(".env.pre-rebuild") < f6.index('test -n "$OLD"') < f6.index("docker ps -q")
    assert f6.index("docker ps -q") < f6.index('docker volume rm "$OLD"')
    assert "HULLGATE_VOLUME=hullgate_data" in steps["F2"]
    # review 554, B1: a plain `up -d` keeps the container on the old volume; F4 recreates it and
    # prints which volume the running container mounts, with its KO branch
    [f4] = re.findall(r"^```sh\n(.+?)\n```$", steps["F4"], re.MULTILINE | re.DOTALL)
    assert "--env-file .env up -d --force-recreate" in f4
    assert f4.index("--force-recreate") < f4.index('.Destination "/data"')
    assert 'if [ "$V" = hullgate_data ]; then echo "F4: OK' in f4
    assert "F4: KO container mounted on" in steps["F4"].split("**If the result differs**")[1]
    assert "keys-and-upstreams.md" in steps["F3"]
    assert "VERIFY-RELAY list: OK" in steps["F5"]


def test_br_042_parser_rejects_incomplete_step() -> None:
    text = "## Step X1 — trial\n\n```sh\nmake deploy\n```\n\n**Expected result**: OK\n"
    body = _steps(text)["X1"]
    assert "**If the result differs**" not in body


SOCLE = ROOT / "docs/guides/keys-and-upstreams.md"
REQUIRED_SOCLE = ("**Expected result**", "**If the result differs**")


def test_br_042_socle_runbook_steps_are_complete() -> None:
    """F-005 (AC-OP): same form, reports on HULL-7; S1 to S12, verify-auth read back from tree
    show."""
    text = SOCLE.read_text(encoding="utf-8")
    steps = _steps(text)
    assert [x for x in steps if x.startswith("S")] == [f"S{n}" for n in range(1, 13)]
    for step_id, body in steps.items():
        assert re.search(r"^```[a-z]*\n.+?\n```$", body, re.MULTILINE | re.DOTALL), step_id
        for marker in REQUIRED_SOCLE:
            assert marker in body, f'socle step {step_id}: "{marker}" absent'
    assert "make verify-auth ROLE=<role-path>" in steps["S8"]
    assert "VERIFY-AUTH: 4/4 OK" in steps["S8"]


CONNEXIONS = ROOT / "docs/guides/keys-and-upstreams.md"
REQUIRED_CONNEXIONS = (
    "**Expected result**",
    "**If the result differs**",
)


def test_br_042_connexions_runbook_steps_are_complete() -> None:
    """F-006 (AC-OP, review 417 o1 and o2): same form, reports on HULL-8; C1 to C13; the key is
    created before the deployment, `connection status` read back after it, the name read back from
    `connection list`, the recovery gesture after a key loss written down."""
    text = CONNEXIONS.read_text(encoding="utf-8")
    steps = _steps(text)
    assert [x for x in steps if x.startswith("C")] == [f"C{n}" for n in range(1, 14)]
    for step_id, body in steps.items():
        assert re.search(r"^```[a-z]*\n.+?\n```$", body, re.MULTILINE | re.DOTALL), step_id
        for marker in REQUIRED_CONNEXIONS:
            assert marker in body, f'connexions step {step_id}: "{marker}" absent'
    assert "secrets/connections_key" in steps["C2"]
    assert "make deploy REF=<ref>" in steps["C4"]
    assert "connection status" in steps["C5"]
    assert "connections key: usable" in steps["C5"]
    assert "http://127.0.0.1/oauth/callback" in steps["C7"]
    assert "--callback - < retour.txt" in steps["C8"]
    assert "make verify-connections CONNECTION=<connection-name>" in steps["C10"]
    assert "VERIFY-CONNECTIONS: 4/4 OK" in steps["C10"]
    assert "connections-key-forget --confirm" in steps["C12"]
    mise_a_jour = RUNBOOKS["mise-a-jour"].read_text(encoding="utf-8")
    assert "connection status" in _steps(mise_a_jour)["M3"]


RELAIS = ROOT / "docs/guides/keys-and-upstreams.md"
REQUIRED_RELAIS = (
    "**Expected result**",
    "**If the result differs**",
)


def test_br_042_relais_runbook_steps_are_complete() -> None:
    """F-008 (AC-OP): same form, reports on HULL-9; R1 to R9 (lot B) and R10 (lot C); gitea-mcp
    pinned on the upstreams network, upstream declared as an internal host, `verify-relay` with the
    tokens on standard input, stdio bridge without 401. Feedback from lot B's AC-OP (HULL-9): played
    as `claude`, `<…>` template detected in `.env`, blocks in short lines with variables, one-token
    form, identifier of the token to revoke."""
    text = RELAIS.read_text(encoding="utf-8")
    steps = _steps(text)
    assert [x for x in steps if x.startswith("R")] == [f"R{n}" for n in range(1, 11)]
    for step_id, body in steps.items():
        assert re.search(r"^```[a-z]*\n.+?\n```$", body, re.MULTILINE | re.DOTALL), step_id
        for marker in REQUIRED_RELAIS:
            assert marker in body, f'relais step {step_id}: "{marker}" absent'
    assert "make deploy REF=$R" in steps["R3"]
    assert "deploy/upstreams.compose.yaml" in steps["R4"]
    assert "--mcp-internal-host" in steps["R5"]
    assert "upstream: mcp-public gitea-mcp" in steps["R5"]  # line of `service list`
    assert "make verify-relay ROLE=$ROLE TOOL=$TOOL" in steps["R7"]
    assert "make verify-relay ROLE=$ROLE SENSITIVE=$SENS TOOL=$TOOL" in steps["R7"]
    assert "VERIFY-RELAY: 5/5 OK" in steps["R7"]
    assert "token list $ROLE" in steps["R7"]
    assert "token revoke $J" in steps["R7"]
    assert "never answer 401" in text
    assert "ssh -p <ssh-port> <user>@<server>" in text
    assert "sudo -iu <user>" in text
    assert "grep -n '<[A-Z_]*>' .env" in steps["R2"]
    assert "progressToken" in steps["R10"]
    assert '"stream": true' in steps["R10"]
    relay = text[text.index("## Step R1") :]
    for block in re.findall(r"^```sh\n(.+?)\n```$", relay, re.MULTILINE | re.DOTALL):
        for line in block.splitlines():
            assert len(line) <= 100, line


STEP_F = re.compile(r"^## Step (F\d) — .+$", re.MULTILINE)


def test_br_042_relais_f015_block_carries_ac_op() -> None:
    """F-015 AC-OP: the block F1 to F6 of the keys/upstreams guide gives the writes back after the
    deployment (spec §12), configures the reviewer of §3.3, plays the three assertions and posts
    the frozen readings; identifiers are read back from `connection list --json`."""
    text = RELAIS.read_text(encoding="utf-8")
    block = text.split("\n## F-015 block", 1)[1]
    found = list(STEP_F.finditer(block))
    assert [m.group(1) for m in found] == [f"F{n}" for n in range(1, 7)]
    steps = {
        m.group(1): block[m.end() : found[i + 1].start() if i + 1 < len(found) else len(block)]
        for i, m in enumerate(found)
    }
    assert "connection list --json" in steps["F1"]
    assert "tool classify gitea $T write" in steps["F3"]
    for needle in ("allowlist --class read", "open pull_request_review_write",
                   "pull_request_review_write method create submit",
                   "deny pull_request_write issue_write"):  # fmt: skip
        assert needle in steps["F4"], needle
    assert "make verify-relay ROLE=$RR TOOL=$TOOL" in steps["F5"]
    assert "VERIFY-RELAY: 5/5 OK" in steps["F5"]
    assert '"exposed": false' in steps["F5"]
    assert "relay.tool_call" in steps["F5"]
    assert "`issue` `refused`" in steps["F5"]
    assert "exposed: true" in steps["F6"]
    assert "wc -c" in steps["F6"]
    assert block.index("Step F3") > block.index("Step F2")  # writes given back after the deploy


# -- F-016 lot A: the console runbook, in English, same form ---------------------------------------

ADMIN = ROOT / "docs/guides/admin.md"
STEP_EN = re.compile(r"^## Step (\S+) — .+$", re.MULTILINE)
REQUIRED_EN = ("**Expected result**", "**If the result differs**")


def test_br_042_admin_runbook_steps_are_complete() -> None:
    text = ADMIN.read_text(encoding="utf-8")
    matches = list(STEP_EN.finditer(text))
    # F-020 phase 2 (P1, BR-302), slice 1 (A1 to A7), slice 2 (B1 to B4, F-016 AC-OP-01 and
    # AC-OP-02 without the preview), slice 3 (C1 to C3, AC-OP-02 with the rights preview),
    # slice 4 (D1 to D3).
    expected = (
        ["P1"] + [f"A{n}" for n in range(1, 8)] + [f"B{n}" for n in range(1, 5)]
        + [f"C{n}" for n in range(1, 4)] + [f"D{n}" for n in range(1, 4)]
    )  # fmt: skip
    assert [m.group(1) for m in matches] == expected
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[match.end() : end]
        assert re.search(r"^```[a-z]*\n.+?\n```$", body, re.MULTILINE | re.DOTALL), match.group(1)
        markers = REQUIRED_EN
        for marker in markers:
            assert marker in body, (match.group(1), marker)


def test_br_042_admin_runbook_names_the_ac_op_patterns() -> None:
    text = ADMIN.read_text(encoding="utf-8")
    for pattern in ("^VERIFY-ADMIN: 4/4 OK$", "^ENROLMENT-LINK: ", '"actor": "admin:'):
        assert pattern in text
    # BR-042 v1 form (F-016 §10): the ADMIN_URL step comes before `make deploy`
    a1 = text[text.index("## Step A1") : text.index("## Step A2")]
    assert a1.index("ADMIN_URL") < a1.index("make deploy")
