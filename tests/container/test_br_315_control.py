"""BR-315 control rejects misleading smoke evidence; no Docker needed for these checks."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import ROOT


def test_br_315_harness_preserves_forged_forwarding_headers() -> None:
    compose = (ROOT / "deploy/smoke/compose.traefik.yaml").read_text(encoding="utf-8")
    assert "- --entrypoints.websecure.forwardedHeaders.insecure=true" in {
        line.strip() for line in compose.splitlines()
    }


@pytest.mark.parametrize(
    "fault",
    ["none", "forged", "shared", "unlimited", "no-limit-row", "response", "trusted", "expired"],
)
def test_br_315_control_checks_addresses_and_independent_counts(tmp_path: Path, fault: str) -> None:
    addresses = ["172.30.247.10", "172.30.247.11"]
    role = "address/smoke/test/principal"
    rows: list[dict[str, object]] = []
    for index, address in enumerate(addresses):
        responses = [True] * 31
        if fault == "response":
            responses[-1] = False
        (tmp_path / f"{index}.json").write_text(
            json.dumps(
                {
                    "address": address,
                    "started": 100,
                    "finished": 161 if fault == "expired" else 102,
                    "responses": responses,
                }
            )
        )
        observed = "203.0.113.9" if fault == "forged" else address
        if fault != "shared" or index == 0:
            rows.extend(
                {
                    "path": role,
                    "action": "access.refused",
                    "reason": "invalid_token",
                    "result": "refused",
                    "detail": json.dumps({"address": observed}),
                }
                for _ in range(31 if fault == "unlimited" else 30)
            )
        if fault != "no-limit-row":
            rows.append(
                {
                    "path": role,
                    "action": "rate_limited.refused",
                    "reason": "rate_limited",
                    "result": "refused",
                    "detail": json.dumps(
                        {
                            "address": observed,
                            "key": observed,
                            "surface": "credential",
                            "suppressed": 0,
                        }
                    ),
                }
            )
    audit_file = tmp_path / "audit.json"
    audit_file.write_text(json.dumps(rows))
    trusted = "172.30.247.0/24" if fault == "trusted" else "172.30.247.2/32"
    result = subprocess.run(
        [
            sys.executable,
            "deploy/smoke/probe.py",
            "client-address",
            role,
            trusted,
            str(tmp_path / "0.json"),
            str(tmp_path / "1.json"),
            str(audit_file),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == (0 if fault == "none" else 1), result.stderr
    assert f"CHECK client-address {'OK' if fault == 'none' else 'KO'}" in result.stdout
