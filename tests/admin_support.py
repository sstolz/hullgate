"""Test support of the admin console (F-016 lot A): a software WebAuthn authenticator and a console
driver over the ASGI client.

`SoftAuthenticator` builds the responses a browser returns from `navigator.credentials.create()`
and `.get()` with the `cryptography` package: attestation `none`, ES256, flags UP and UV, a
signature counter. It never talks to a browser; `tests/browser` uses Chromium's virtual
authenticator instead.

Every enrolment link and session value the suite produces is registered in `REGISTRY`, so that the
audit sweep of BR-066 (and of BR-213 for the API) looks for them.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from starlette.testclient import TestClient

from hullgate import config
from hullgate.admin.app import API_PREFIX, Guard, build_admin_app
from hullgate.admin.sessions import COOKIE
from tests.support import PUBLIC_URL, REGISTRY, Gateway

ADMIN_URL = "http://localhost:14791"
CLIENT_IP = "203.0.113.7"
LINK = re.compile(r"^ENROLMENT-LINK: (\S+)$", re.MULTILINE)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass
class SoftAuthenticator:
    """One discoverable ES256 credential; `user_verified=False` models an authenticator that
    cannot verify its user."""

    user_verified: bool = True
    key: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP256R1())
    )
    credential_id: bytes = field(default_factory=lambda: secrets.token_bytes(16))
    sign_count: int = 0
    user_handle: bytes = b""

    @property
    def id(self) -> str:
        return b64(self.credential_id)

    def _cose(self) -> bytes:
        numbers = self.key.public_key().public_numbers()
        return cbor2.dumps(
            {
                1: 2,
                3: -7,
                -1: 1,
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )

    def _flags(self, attested: bool, uv: bool | None) -> int:
        verified = self.user_verified if uv is None else uv
        return 0x01 | (0x04 if verified else 0) | (0x40 if attested else 0)

    def create(
        self, options: dict[str, Any], origin: str = ADMIN_URL, *, uv: bool | None = None
    ) -> dict[str, Any]:
        rp_id = options["rp"]["id"]
        self.user_handle = unb64(options["user"]["id"])
        client_data = json.dumps(
            {"type": "webauthn.create", "challenge": options["challenge"], "origin": origin,
             "crossOrigin": False}
        ).encode()  # fmt: skip
        auth_data = (
            hashlib.sha256(rp_id.encode()).digest()
            + bytes([self._flags(True, uv)])
            + self.sign_count.to_bytes(4, "big")
            + bytes(16)
            + len(self.credential_id).to_bytes(2, "big")
            + self.credential_id
            + self._cose()
        )
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {
            "id": self.id,
            "rawId": self.id,
            "type": "public-key",
            "response": {
                "clientDataJSON": b64(client_data),
                "attestationObject": b64(attestation),
                "transports": ["internal"],
            },
            "clientExtensionResults": {},
        }

    def get(
        self,
        options: dict[str, Any],
        origin: str = ADMIN_URL,
        *,
        uv: bool | None = None,
        rp_id: str | None = None,
        count: int | None = None,
    ) -> dict[str, Any]:
        self.sign_count = self.sign_count + 1 if count is None else count
        client_data = json.dumps(
            {"type": "webauthn.get", "challenge": options["challenge"], "origin": origin,
             "crossOrigin": False}
        ).encode()  # fmt: skip
        auth_data = (
            hashlib.sha256((rp_id or options["rpId"]).encode()).digest()
            + bytes([self._flags(False, uv)])
            + self.sign_count.to_bytes(4, "big")
        )
        signature = self.key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        return {
            "id": self.id,
            "rawId": self.id,
            "type": "public-key",
            "response": {
                "clientDataJSON": b64(client_data),
                "authenticatorData": b64(auth_data),
                "signature": b64(signature),
                "userHandle": b64(self.user_handle),
            },
            "clientExtensionResults": {},
        }


def link_token(out: str) -> str:
    """The token of the one `ENROLMENT-LINK:` line of a bootstrap output; registered."""
    links = LINK.findall(out)
    assert len(links) == 1, out
    token = str(links[0]).rsplit("/", 1)[1]
    REGISTRY.secrets.append(token)
    return token


class Console:
    """The admin listener of a `Gateway`, driven like the console drives it."""

    def __init__(
        self,
        gw: Gateway,
        *,
        admin_url: str = ADMIN_URL,
        rp_id: str = "",
        address: str = CLIENT_IP,
        static_dir: Path | None = None,
        proxies: str = "",
    ) -> None:
        self.gw = gw
        self.origin = config.admin_origin(admin_url, rp_id)
        env = {"TRUSTED_PROXIES": proxies} if proxies else {}
        self.proxies = config.load({**gw.env, **env, "ADMIN_URL": admin_url}).trusted_proxies
        self.app: Guard = build_admin_app(
            gw.db,
            self.origin,
            public_url=PUBLIC_URL,
            proxies=self.proxies,
            clock=gw.clock,
            static_dir=static_dir,
            connections_key=gw.connections_key(),
            outbound=gw.outbound,
            upstream=gw.upnet.client(),
        )
        self.address = address
        self.base = self.origin.origin
        self.csrf = ""

    @contextmanager
    def client(self, address: str | None = None) -> Iterator[TestClient]:
        with TestClient(
            self.app, base_url=self.base, client=(address or self.address, 50000)
        ) as client:
            yield client

    def headers(self, csrf: str | None = None, *, activity: bool = True) -> dict[str, str]:
        out = {"Origin": self.origin.origin}
        token = self.csrf if csrf is None else csrf
        if token:
            out["X-CSRF-Token"] = token
        if activity:
            out["X-Hullgate-Activity"] = "1"
        return out

    def call(
        self,
        client: TestClient,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        **headers: Any,
    ) -> Any:
        return client.request(
            method,
            f"{API_PREFIX}/{path}",
            json=body if method != "GET" else None,
            headers=self.headers(**headers),
        )

    # -- ceremonies ---------------------------------------------------------------------------

    def bootstrap(self, email: str = "ops@example.test", *extra: str) -> str:
        return link_token(self.gw.ok("admin", "bootstrap", email, *extra))

    def enrol(
        self, client: TestClient, token: str, authenticator: SoftAuthenticator, label: str = "key"
    ) -> Any:
        options = self.call(client, "POST", "enrol/options", {"link": token}, csrf="")
        assert options.status_code == 200, options.text
        credential = authenticator.create(options.json()["publicKey"], self.origin.origin)
        response = self.call(
            client,
            "POST",
            "enrol/verify",
            {"link": token, "label": label, "credential": credential},
            csrf="",
        )
        self._signed_in(client, response)
        return response

    def sign_in(self, client: TestClient, authenticator: SoftAuthenticator, **get: Any) -> Any:
        options = self.call(client, "POST", "auth/options", {}, csrf="")
        if options.status_code != 200:
            return options
        credential = authenticator.get(
            options.json()["publicKey"], **{"origin": self.origin.origin, **get}
        )
        response = self.call(client, "POST", "auth/assert", {"credential": credential}, csrf="")
        self._signed_in(client, response)
        return response

    def _signed_in(self, client: TestClient, response: Any) -> None:
        if response.status_code == 200:
            self.csrf = response.json()["csrf_token"]
            value = client.cookies.get(COOKIE)
            if value:
                REGISTRY.secrets.append(value)

    def admin(self, client: TestClient, email: str = "ops@example.test") -> SoftAuthenticator:
        """Bootstrap and enrol one administrator; the client holds its session."""
        authenticator = SoftAuthenticator()
        response = self.enrol(client, self.bootstrap(email), authenticator)
        assert response.status_code == 200, response.text
        return authenticator

    def fresh(self, client: TestClient, authenticator: SoftAuthenticator) -> Any:
        options = self.call(client, "POST", "session/fresh/options", {})
        assert options.status_code == 200, options.text
        credential = authenticator.get(options.json()["publicKey"], self.origin.origin)
        return self.call(client, "POST", "session/fresh", {"credential": credential})

    def add_passkey(
        self, client: TestClient, authenticator: SoftAuthenticator, label: str = "second"
    ) -> Any:
        options = self.call(client, "POST", "passkeys/options", {})
        assert options.status_code == 200, options.text
        credential = authenticator.create(options.json()["publicKey"], self.origin.origin)
        return self.call(client, "POST", "passkeys", {"label": label, "credential": credential})


# Concrete values for the route sweeps (BR-068 v2, BR-205, BR-213, BR-234): a `GET` route that
# needs a query to answer 200 on an empty base, and the path parameters of the API. A route added
# with a parameter or a required query and no entry here fails the sweeps.
SAMPLES = {"impacts": "impacts?act=tree.rename&path=%2F", "changes": "changes?seq=1"}
PATH_PARAMS = {"{credential_id}": "x"}


def concrete(path: str) -> str:
    """The API path of a route declaration, made concrete for the sweeps (prefix excluded)."""
    out = SAMPLES.get(path, path)
    for name, value in PATH_PARAMS.items():
        out = out.replace(name, value)
    return out


def counts(gw: Gateway) -> dict[str, int]:
    """Row count of every table, for the sweeps that prove a request wrote nothing."""
    conn = gw.connect()
    try:
        names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {n: int(conn.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0]) for n in names}  # noqa: S608
    finally:
        conn.close()


def bundle(tmp_path: Path) -> Path:
    """A stand-in for the built console: `index.html` and one asset."""
    root = tmp_path / "console"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(
        '<!doctype html><html><head><script type="module" src="/admin/assets/app.js">'
        "</script></head><body><div id=app></div></body></html>",
        encoding="utf-8",
    )
    (root / "assets" / "app.js").write_text("export {};\n", encoding="utf-8")
    return root
