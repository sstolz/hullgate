"""Mounted secrets: the signing key of OAuth access tokens and the TOTP secret of the barrier.

BR-032 v2: both secrets are files named by `SIGNING_KEY_FILE` and
`AUTHORIZE_TOTP_SECRET_FILE`. BR-033 v2, BR-064: "key loaded" is part of the availability
predicate, evaluated on every request; a removed or unreadable file is seen from the next
request on (the file is reread on every request). No secret is logged.

Outside the gateway, `python -m hullgate.keys` generates both files; see
`docs/guides/keys-and-upstreams.md` for provisioning and recovery. The bench and restore check
also generate throwaway keys:

    python -m hullgate.keys signing-key <file>
    python -m hullgate.keys totp-secret <file> [--label <name>]
    python -m hullgate.keys connections-key <file>   # connections key (F-006, BR-032 v3)
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import os
import secrets
import struct
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import quote

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

log = logging.getLogger("hullgate.keys")

ALGORITHM = "ES256"  # BR-054: asymmetric signature, algorithm enforced at verification
TOTP_STEP = 30
TOTP_DIGITS = 6
TOTP_SKEW = 1  # ±1 step of tolerance (BR-060)
SECRET_MAX = 64 * 1024


def _read(path: str) -> bytes | None:
    try:
        with open(path, "rb") as f:
            data = f.read(SECRET_MAX + 1)
    except OSError:
        return None
    return data if len(data) <= SECRET_MAX else None


class SigningKey:
    """EC P-256 private key read from a PEM file, reread on every call (operator's decision).

    `private()` returns the key or None (file absent, unreadable, unparsable, or a key of another
    type): a file removed, made unreadable or replaced is seen from the next request on.
    """

    def __init__(self, path: str) -> None:
        self.path = path

    def private(self) -> ec.EllipticCurvePrivateKey | None:
        data = _read(self.path)
        if data is None:
            return None
        try:
            key = serialization.load_pem_private_key(data, password=None)
        except (ValueError, TypeError):
            log.warning("invalid signing key (PEM expected): %s", self.path)
            return None
        if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
            log.warning("signing key: EC P-256 expected (ES256): %s", self.path)
            return None
        return key

    def available(self) -> bool:
        return self.private() is not None

    def public(self) -> ec.EllipticCurvePublicKey | None:
        key = self.private()
        return None if key is None else key.public_key()


# ---------------------------------------------------------------------------------------------
# TOTP (RFC 6238, HMAC-SHA1, 30 s, 6 digits)
# ---------------------------------------------------------------------------------------------


def decode_base32(text: str) -> bytes | None:
    clean = "".join(text.split()).upper().rstrip("=")
    if not clean:
        return None
    try:
        raw = base64.b32decode(clean + "=" * (-len(clean) % 8))
    except (binascii.Error, ValueError):
        return None
    return raw if len(raw) >= 10 else None  # at least 80 bits


class TotpSecret:
    """Base32 TOTP secret read from a file on every use (file absent: barrier closed)."""

    def __init__(self, path: str) -> None:
        self.path = path

    def secret(self) -> bytes | None:
        data = _read(self.path)
        if data is None:
            return None
        try:
            return decode_base32(data.decode("ascii"))
        except UnicodeDecodeError:
            return None

    def configured(self) -> bool:
        return self.secret() is not None


def totp(secret: bytes, counter: int) -> str:
    digest = hmac.new(secret, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**TOTP_DIGITS).zfill(TOTP_DIGITS)


def totp_counter(now: float) -> int:
    return int(now // TOTP_STEP)


def totp_match(secret: bytes, code: str, now: float) -> int | None:
    """Counter of the step that produces `code` (current step ± 1), or None.

    The three steps are always compared, in constant time.
    """
    if len(code) != TOTP_DIGITS or not code.isdigit():
        return None
    current = totp_counter(now)
    found: int | None = None
    for counter in range(current - TOTP_SKEW, current + TOTP_SKEW + 1):
        if hmac.compare_digest(totp(secret, counter), code) and found is None:
            found = counter
    return found


# ---------------------------------------------------------------------------------------------
# Generation (outside the gateway)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Generated:
    path: str
    detail: str


def _write_private(path: str, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def generate_signing_key(path: str) -> Generated:
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    _write_private(path, pem)
    return Generated(path, "EC P-256 key (ES256), PEM PKCS#8")


def generate_totp_secret(path: str, label: str) -> Generated:
    value = base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")
    _write_private(path, (value + "\n").encode("ascii"))
    uri = (
        f"otpauth://totp/{quote(label)}?secret={value}&issuer=hullgate"
        f"&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_STEP}"
    )
    return Generated(path, uri)


def _forget_connections_key_check() -> int:
    """`connections-key-forget --confirm`: forgets the key check of the connections key
    when no ciphertext remains (docs/guides/keys-and-upstreams.md, step C12)."""
    import time

    from hullgate import cli, config
    from hullgate.cipher import forget_key_check
    from hullgate.db import StoreUnavailable

    try:
        conn = cli.open_store(config.load(os.environ).db_path)
    except (config.ConfigError, StoreUnavailable) as exc:
        print(f"database unavailable: {exc}", file=sys.stderr)
        return 1
    try:
        remaining = forget_key_check(conn, time.time())
    except StoreUnavailable as exc:
        print(f"database unavailable: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    if remaining:
        print(
            "refusal: ciphertexts still live (" + ", ".join(remaining) + "): remove the "
            "connections and the applications with a secret first, or restore the original key",
            file=sys.stderr,
        )
        return 2
    print("key check forgotten: the next key is adopted at the first ciphertext")
    return 0


def main(argv: Sequence[str]) -> int:
    usage = (
        "usage: python -m hullgate.keys signing-key <file> | "
        "totp-secret <file> [--label <name>] | connections-key <file> | "
        "connections-key-forget --confirm"
    )
    args = list(argv)
    if args == ["connections-key-forget", "--confirm"]:
        return _forget_connections_key_check()
    if len(args) < 2 or args[0] not in ("signing-key", "totp-secret", "connections-key"):
        print(usage, file=sys.stderr)
        return 2
    try:
        if args[0] == "signing-key":
            done = generate_signing_key(args[1])
            print(f"written: {done.path} ({done.detail}, mode 0400)")
            return 0
        if args[0] == "connections-key":
            if len(args) != 2:
                print(usage, file=sys.stderr)
                return 2
            from hullgate.cipher import generate_connections_key

            generate_connections_key(args[1])
            print(f"written: {args[1]} (connections key, 32 bytes in base64, mode 0400)")
            return 0
        label = "hullgate"
        if len(args) == 4 and args[2] == "--label":
            label = args[3]
        elif len(args) != 2:
            print(usage, file=sys.stderr)
            return 2
        done = generate_totp_secret(args[1], label)
    except FileExistsError:
        print(
            f"refusal: the file already exists: {args[1]} (nothing is overwritten)", file=sys.stderr
        )
        return 2
    except OSError as exc:
        print(f"write failed: {exc}", file=sys.stderr)
        return 1
    print(f"written: {done.path} (base32 TOTP secret, mode 0400)")
    print("to enrol once only in the operator's authenticator app:")
    print(done.detail)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
