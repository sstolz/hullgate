"""BR-330: inspect literal URL authorities and e-mail domains without resolving them."""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

from operator_names import tracked

URL = re.compile(r"(?:https?|wss?)://([^\s\"'<>`\\]+)", re.I)
EMAIL = re.compile(r"[\w.+-]+@([\w.-]+\.[A-Za-z]{2,})")
PUBLIC_SERVICES = {
    "claude.ai": "OAuth client origin exercised by name",
    "claude.com": "OAuth client origin exercised by name",
    "docker.gitea.com": "Gitea image registry boundary fixture for BR-016",
    "gitea.com": "Gitea MCP default host forbidden by BR-016",
    "github.com": "Repository default and advisory URL fixtures",
    "pypi.org": "SDK distribution source fixture",
    "cdn.jsdelivr.net": "Pinned browser dependency download",
}
# These are local Compose service labels, not invented internet domains. They must stay
# consistent with the bench's service discovery; no suffix or arbitrary host is accepted.
LOCAL_SERVICES = {"front", "front-ro", "gitea", "upstream", "gateway"}
# Deliberately malformed/noncanonical authorities in parser refusal tests, exact file pins.
INVALID_AUTHORITIES = {
    "tests/unit/test_br_084_outbound_bounds.py": {"127.1", "0x7f000001"},
    "tests/unit/test_br_107_upstream_bounds.py": {"127.1", "0x7f000001"},
    "tests/unit/test_br_060_csp_origin.py": {"exa", "exa;mple.test"},
}


def reserved(host: str) -> bool:
    host = host.lower().rstrip(".")
    return host == "localhost" or any(
        host == suffix or host.endswith("." + suffix)
        for suffix in ("test", "invalid", "example", "example.com", "example.org", "example.net")
    )


def literal_hosts(text: str) -> list[tuple[int, str]]:
    result = []
    for pattern in (URL, EMAIL):
        for match in pattern.finditer(text):
            authority = match.group(1).split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
            authority = authority.rsplit("@", 1)[-1]
            if authority.startswith("["):
                host = authority.partition("]")[0] + "]"
            else:
                host = authority.split(":", 1)[0].rstrip(",);]}")
            # Interpolated expressions and wildcard assertion fragments are not literals.
            if not host or any(c in host for c in "$%{}*|") or host == "[]":
                continue
            result.append((text.count("\n", 0, match.start()) + 1, host.lower().rstrip(".")))
    return result


def findings(root: Path, paths: list[str] | None = None) -> list[str]:
    bad = []
    for path in paths if paths is not None else tracked(root):
        if not path.startswith(("tests/", "poc/", "web/src/")):
            continue
        try:
            text = (root / path).read_text()
        except UnicodeError:
            continue
        for line, host in literal_hosts(text):
            if reserved(host) or host in PUBLIC_SERVICES:
                continue
            try:
                ipaddress.ip_address(host.strip("[]"))
                continue  # Address properties and mocked comments are reviewed separately.
            except ValueError:
                pass
            if host in INVALID_AUTHORITIES.get(path, set()):
                continue
            if host in LOCAL_SERVICES and (
                path.startswith("poc/isolation-bench/")
                or path == "tests/unit/test_br_230_verification.py"
            ):
                continue
            bad.append(f"{path}:{line}: unreserved fixture host {host}")
    return bad
