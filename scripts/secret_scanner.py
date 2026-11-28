"""Hullgate offline secret scanner 1.1.0 (BR-326/327).

Byte-oriented, including binary files. No network, decoders, subprocesses or value output.
Findings retain only coordinates and SHA-256/96 fingerprints; never candidate bytes.
The source checksum and execution-image digest are pinned in qa/secrets-scanner.json.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from base64 import b64decode
from dataclasses import asdict, dataclass

VERSION = "1.1.0"
MASK = "[REDACTED]"

# The named value group is the complete candidate. No entropy exemption: even
# low-entropy dummies must be accounted for. Labelled values use minimum lengths
# and alphabets to limit noise; unlabelled strings remain outside heuristic coverage.
PATTERNS = {
    "private-key": (
        rb"(?P<value>-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
        rb"[\s\S]*?-----END (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
        rb"|-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----[^\r\n]*)"
    ),
    "github-token": (
        rb"\b(?P<value>(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255}))\b"
    ),
    "aws-access-key": rb"\b(?P<value>(?:AKIA|ASIA)[A-Z0-9]{16})\b",
    "google-api-key": rb"\b(?P<value>AIza[A-Za-z0-9_-]{35})\b",
    "openai-token": rb"\b(?P<value>sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,255})\b",
    "slack-token": rb"\b(?P<value>xox[baprs]-[A-Za-z0-9-]{10,255})\b",
    "stripe-key": rb"\b(?P<value>(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,255})\b",
    "hullgate-token": rb"\b(?P<value>hg_[A-Za-z0-9_-]{20,255})\b",
    "jwt": rb"\b(?P<value>eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})\b",
    "bearer": rb"(?i)\bbearer[ \t]+(?P<value>[A-Za-z0-9_.~+/-]{16,}={0,2})",
    "credential-url": rb"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s/:@]+:(?P<value>[^\s/@]{8,})@",
    "secret-assignment": (
        rb"(?i)\b[\w.-]{0,80}(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)"
        rb"[\w.-]{0,80}[\"']?[ \t]*[:=][ \t]*[\"']?(?P<value>[A-Za-z0-9_./+~=-]{16,})"
        rb"(?:[\"']|(?=[ \t]*(?:[#\r\n]|$)))"
    ),
    "authorization-token": (
        rb"(?i)\bauthorization[\"']?[ \t]*:[ \t]*[\"']?token[ \t]+"
        rb"(?P<value>[0-9a-f]{40})(?![A-Za-z0-9_])"
    ),
    "authorization-basic": (
        rb"(?i)\bauthorization[\"']?[ \t]*:[ \t]*[\"']?basic[ \t]+"
        rb"(?P<value>[A-Za-z0-9+/]{8,}={0,2})(?![A-Za-z0-9+/=])"
    ),
    "key-assignment": (
        rb"(?i)\b[\w.-]{1,80}[_-]key[\"']?[ \t]*[:=][ \t]*[\"']?"
        rb"(?P<value>[A-Za-z0-9_+/-]{16,}={0,2})"
        rb"(?:[\"']|(?=[ \t]*(?:[#\r\n]|$)))"
    ),
    "totp-seed": (
        rb"(?i)\b(?:[\w.-]{0,80}totp(?:[_-](?:seed|key|secret))?|otp[_-]seed)"
        rb"[\"']?[ \t]*[:=][ \t]*[\"']?(?P<value>[A-Z2-7]{16,}={0,6})"
        rb"(?:[\"']|(?=[ \t]*(?:[#\r\n]|$)))"
    ),
    "password-assignment": (
        rb"(?i)\b[\w.-]{0,80}(?:password|passwd|pwd)[\"']?[ \t]*[:=][ \t]*"
        rb"(?P<quote>[\"'])?(?P<value>"
        rb"(?(quote)[^\r\n\"']{4,}?|[A-Za-z0-9!@#$%^&*_+./?~=-]{4,}))"
        rb"(?(quote)(?P=quote)|(?=[ \t]*(?:[#\r\n]|$)))"
    ),
    "pgp-private-key": (
        rb"(?P<value>-----BEGIN PGP "
        rb"PRIVATE KEY BLOCK-----"
        rb"[\s\S]*?-----END PGP PRIVATE KEY BLOCK-----"
        rb"|-----BEGIN PGP "
        rb"PRIVATE KEY BLOCK-----[^\r\n]*)"
    ),
    "gitlab-token": rb"\b(?P<value>glpat-[A-Za-z0-9_-]{20,255})(?![A-Za-z0-9_-])",
}
DETECTORS = {name: re.compile(pattern) for name, pattern in PATTERNS.items()}


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    detector: str
    fingerprint: str

    @property
    def key(self) -> tuple[str, str, str]:
        return self.path, self.detector, self.fingerprint


def scan(path: str, data: bytes) -> list[Finding]:
    findings = set()
    for detector, pattern in DETECTORS.items():
        for match in pattern.finditer(data):
            findings.add(
                Finding(
                    path,
                    data.count(b"\n", 0, match.start("value")) + 1,
                    detector,
                    hashlib.sha256(match.group("value")).hexdigest()[:24],
                )
            )
    return sorted(findings)


def main() -> int:
    """Container protocol: byte payloads on stdin; only masked coordinates on stdout."""
    try:
        payload = json.load(sys.stdin)
        result = [
            asdict(f) for path, data in payload for f in scan(path, b64decode(data, validate=True))
        ]
    except (ValueError, TypeError):
        print("scanner input error", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
