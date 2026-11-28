"""Fake OAuth provider (F-006 lot B): one local HTTPS server, several hosts.

A single TLS server (throwaway certificate `*.example.test`) serves every provider, routed by the
`Host` header. The gateway's bounded client (`hullgate.outbound.Outbound`) reaches it through an
injected resolver — each `*.example.test` host resolves to a fictitious **public** address — and
a `dial` function that redirects the checked address to this server: the address policy of
BR-084 is thus played as is, without ever reaching a real host.

Each provider keeps an upstream journal (`journal`) of what it receives; each token it issues is
entered in the suite's secret registry (sweeps of BR-066, BR-072, BR-087).
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import ipaddress
import json
import secrets
import ssl
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from hullgate.outbound import Outbound

AUTHORIZE = "/login/oauth/authorize"
TOKEN = "/login/oauth/access_token"  # noqa: S105 (a path, not a secret)
USERINFO = "/login/oauth/userinfo"
REVOKE = "/login/oauth/revoke"
REGISTER = "/login/oauth/register"
AS_METADATA = "/.well-known/oauth-authorization-server"


def make_cert(folder: Path, names: list[str]) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])])
    now = datetime.datetime.now(datetime.UTC)
    alt: list[x509.GeneralName] = [x509.DNSName(n) for n in names]
    alt.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName(alt), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = folder / "cert.pem", folder / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@dataclass
class Reply:
    """Scripted reply of the token endpoint (consumed in order).

    `normal`: the request is first handled normally (code consumed, refresh token rotated), then
    `delay`, `cut` or the forced status apply. `cut`: connection closed without a reply.
    `raw`: bytes sent as is.
    """

    status: int = 200
    body: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0
    cut: bool = False
    normal: bool = False
    raw: bytes | None = None
    size: int = 0  # body of `size` bytes (reply too large)


@dataclass
class Call:
    method: str
    path: str
    form: dict[str, str]
    body: dict[str, Any]
    client_id: str | None
    authorization: str | None
    at: float


@dataclass
class Provider:
    host: str
    model: str = "gitea"
    on_secret: Callable[[str], None] = lambda _v: None
    account: str | None = "cairn-ai"
    userinfo: bool = True
    expires_in: int | None = 3600
    refresh_expires_in: int | None = None
    give_refresh: bool = True
    rotate: bool = True  # single-use refresh token
    scope: str | None = None  # scopes returned (None: those requested)
    revocation: bool = True
    revoke_status: int = 200  # 500: provider down at revocation
    registration: bool = True
    prm: bool = False  # RFC 9728 (generic model)
    resource: str | None = None
    dcr_secret: bool = False
    dcr_secret_expires_at: int = 0
    metadata_patch: dict[str, Any] = field(default_factory=dict)
    script: list[Reply] = field(default_factory=list)
    journal: list[Call] = field(default_factory=list)
    codes: dict[str, dict[str, Any]] = field(default_factory=dict)
    refresh_tokens: dict[str, dict[str, Any]] = field(default_factory=dict)
    access_tokens: dict[str, str | None] = field(default_factory=dict)
    revoked: list[tuple[str, str | None, str]] = field(
        default_factory=list
    )  # (hint, client, token)
    clients: dict[str, str | None] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def base(self) -> str:
        return f"https://{self.host}"

    def metadata(self) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "issuer": self.base + "/",
            "authorization_endpoint": self.base + AUTHORIZE,
            "token_endpoint": self.base + TOKEN,
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
        }
        if self.userinfo:
            doc["userinfo_endpoint"] = self.base + USERINFO
        if self.revocation:
            doc["revocation_endpoint"] = self.base + REVOKE
        if self.registration:
            doc["registration_endpoint"] = self.base + REGISTER
        doc.update(self.metadata_patch)
        return doc

    # -- view of the "operator's browser" --------------------------------------------------------

    def approve(self, auth_url: str, account: str | None = None) -> str:
        """The operator logs in and authorizes: returns the callback URL (code, state)."""
        query = {k: v[0] for k, v in parse_qs(urlsplit(auth_url).query).items()}
        code = "code-" + secrets.token_urlsafe(24)
        self.on_secret(code)
        with self.lock:
            self.codes[code] = {
                "challenge": query["code_challenge"],
                "method": query.get("code_challenge_method"),
                "redirect_uri": query["redirect_uri"],
                "client_id": query["client_id"],
                "scope": query.get("scope", ""),
                "resource": query.get("resource"),
                "account": self.account if account is None else account,
                "used": False,
            }
        return f"{query['redirect_uri']}?{urlencode({'code': code, 'state': query['state']})}"

    def calls(self, path: str, grant: str | None = None) -> list[Call]:
        with self.lock:
            return [
                c
                for c in self.journal
                if c.path == path and (grant is None or c.form.get("grant_type") == grant)
            ]

    # -- issuance ----------------------------------------------------------------------------

    def _issue(self, client_id: str | None, scope: str, account: str | None) -> dict[str, Any]:
        access = "at-" + secrets.token_urlsafe(24)
        self.on_secret(access)
        self.access_tokens[access] = account
        doc: dict[str, Any] = {"access_token": access, "token_type": "bearer"}
        if self.refresh_expires_in is not None:
            doc["refresh_token_expires_in"] = self.refresh_expires_in
        if self.expires_in is not None:
            doc["expires_in"] = self.expires_in
        if self.give_refresh:
            refresh = "rt-" + secrets.token_urlsafe(24)
            self.on_secret(refresh)
            self.refresh_tokens[refresh] = {
                "client_id": client_id,
                "scope": scope,
                "account": account,
                "live": True,
            }
            doc["refresh_token"] = refresh
        doc["scope"] = self.scope if self.scope is not None else scope
        return doc

    def token(self, form: dict[str, str], client_id: str | None) -> tuple[int, dict[str, Any]]:
        with self.lock:
            grant = form.get("grant_type")
            if grant == "authorization_code":
                entry = self.codes.get(form.get("code", ""))
                if entry is None or entry["used"]:
                    return 400, {"error": "invalid_grant"}
                entry["used"] = True
                verifier = form.get("code_verifier", "")
                digest = hashlib.sha256(verifier.encode()).digest()
                challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
                if (
                    challenge != entry["challenge"]
                    or entry["method"] != "S256"
                    or form.get("redirect_uri") != entry["redirect_uri"]
                    or client_id != entry["client_id"]
                ):
                    return 400, {"error": "invalid_grant"}
                return 200, self._issue(client_id, entry["scope"], entry["account"])
            if grant == "refresh_token":
                entry = self.refresh_tokens.get(form.get("refresh_token", ""))
                if entry is None or not entry["live"]:
                    return 400, {"error": "invalid_grant"}
                if self.rotate:
                    entry["live"] = False
                doc = self._issue(client_id, entry["scope"], entry["account"])
                if not self.rotate and "refresh_token" in doc:
                    del self.refresh_tokens[doc.pop("refresh_token")]
                return 200, doc
            return 400, {"error": "unsupported_grant_type"}

    def register(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        with self.lock:
            client_id = "dyn-" + secrets.token_hex(6)
            doc: dict[str, Any] = {
                "client_id": client_id,
                "redirect_uris": body.get("redirect_uris"),
                "token_endpoint_auth_method": "none",
            }
            secret = None
            if self.dcr_secret:
                secret = "cs-" + secrets.token_urlsafe(24)
                self.on_secret(secret)
                doc["client_secret"] = secret
                doc["client_secret_expires_at"] = self.dcr_secret_expires_at
                doc["token_endpoint_auth_method"] = "client_secret_post"  # noqa: S105
            self.clients[client_id] = secret
            return 201, doc


class Net:
    """The shared HTTPS server and the "DNS resolution" of the `*.example.test` hosts."""

    def __init__(self, on_secret: Callable[[str], None]) -> None:
        self.on_secret = on_secret
        self.folder = Path(tempfile.mkdtemp(prefix="fake-provider-"))
        self.cert_path, key_path = make_cert(self.folder, ["*.example.test", "localhost"])
        self.providers: dict[str, Provider] = {}
        self.addresses: dict[str, list[str]] = {}
        self.resolved: list[str] = []
        self.dialed: list[tuple[str, int]] = []
        self.lock = threading.Lock()
        net = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_: object) -> None:
                return

            def _provider(self) -> Provider | None:
                host = (self.headers.get("Host") or "").split(":")[0].lower()
                return net.provider(host) if host.endswith(".example.test") else None

            def _send(self, status: int, doc: dict[str, Any], extra: dict[str, str]) -> None:
                body = json.dumps(doc).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for k, v in extra.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                p = self._provider()
                path = urlsplit(self.path).path
                if p is None:
                    self._send(404, {}, {})
                    return
                with p.lock:
                    p.journal.append(
                        Call(
                            "GET",
                            path,
                            {},
                            {},
                            None,
                            self.headers.get("Authorization"),
                            time.time(),
                        )
                    )
                if path.startswith(("/.well-known/openid-configuration", AS_METADATA)):
                    self._send(200, p.metadata(), {})
                elif path.startswith("/.well-known/oauth-protected-resource") and p.prm:
                    doc = {"resource": p.resource or p.base, "authorization_servers": [p.base]}
                    self._send(200, doc, {})
                elif path == USERINFO and p.userinfo:
                    token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                    with p.lock:
                        known = token in p.access_tokens
                        account = p.access_tokens.get(token)
                    if not known:
                        self._send(401, {"error": "invalid_token"}, {})
                        return
                    self._send(
                        200,
                        {}
                        if account is None
                        else {
                            "preferred_username": account,
                            "sub": "id-" + account,
                        },
                        {},
                    )
                elif path == "/api/v1/user":
                    token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                    with p.lock:
                        known = token in p.access_tokens
                    self._send(200 if known else 401, {}, {})
                else:
                    self._send(404, {}, {})

            def do_POST(self) -> None:
                p = self._provider()
                path = urlsplit(self.path).path
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                if p is None:
                    self._send(404, {}, {})
                    return
                form: dict[str, str] = {}
                body: dict[str, Any] = {}
                if "json" in (self.headers.get("Content-Type") or ""):
                    body = json.loads(raw or b"{}")
                else:
                    form = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
                auth = self.headers.get("Authorization")
                client_id = form.get("client_id")
                if auth and auth.startswith("Basic "):
                    pair = base64.b64decode(auth[6:]).decode()
                    client_id = unquote(pair.split(":", 1)[0])
                with p.lock:
                    p.journal.append(Call("POST", path, form, body, client_id, auth, time.time()))
                if path == TOKEN:
                    self._token(p, form, client_id)
                elif path == REVOKE:
                    with p.lock:
                        p.revoked.append(
                            (form.get("token_type_hint", ""), client_id, form.get("token", ""))
                        )
                    self._send(p.revoke_status, {}, {})
                elif path == REGISTER and p.registration:
                    self._send(*p.register(body), {})
                else:
                    self._send(404, {}, {})

            def _token(self, p: Provider, form: dict[str, str], client_id: str | None) -> None:
                with p.lock:
                    reply = p.script.pop(0) if p.script else None
                if reply is None or reply.normal:
                    status, doc = p.token(form, client_id)
                else:
                    status, doc = reply.status, reply.body or {}
                if reply is not None:
                    if reply.delay:
                        time.sleep(reply.delay)
                    if reply.cut:
                        self.close_connection = True
                        return
                    if reply.raw is not None:
                        self.wfile.write(reply.raw)
                        self.close_connection = True
                        return
                    if reply.size:
                        data = b"{" + b" " * reply.size + b"}"
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                        return
                    if reply.normal and reply.status != 200:
                        status = reply.status
                        doc = reply.body or {}
                    elif reply.normal and reply.body is not None:
                        doc = {**doc, **reply.body}
                        doc = {k: v for k, v in doc.items() if v is not None}
                    self._send(status, doc, reply.headers)
                    return
                self._send(status, doc, {})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(self.cert_path, key_path)
        self.httpd.socket = tls.wrap_socket(self.httpd.socket, server_side=True)
        self.port = int(self.httpd.server_address[1])
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.context = ssl.create_default_context(cafile=str(self.cert_path))

    def reset(self) -> None:
        with self.lock:
            self.providers.clear()
            self.addresses.clear()
            self.resolved.clear()
            self.dialed.clear()

    def provider(self, host: str, model: str = "gitea") -> Provider:
        host = host.lower()
        with self.lock:
            if host not in self.providers:
                self.providers[host] = Provider(host, model, self.on_secret)
            return self.providers[host]

    def resolve(self, host: str, port: int) -> list[str]:
        with self.lock:
            self.resolved.append(host)
            if host in self.addresses:
                return list(self.addresses[host])
        if host.endswith(".example.test"):
            n = int(hashlib.sha256(host.encode()).hexdigest()[:4], 16)
            return [f"11.{n >> 8}.{n & 255}.1"]  # adresse publique fictive, jamais jointe
        raise OSError("unknown host")

    def dial(self, address: str, port: int) -> tuple[str, int]:
        with self.lock:
            self.dialed.append((address, port))
        return "127.0.0.1", self.port

    def outbound(self) -> Outbound:
        return Outbound(context=self.context, resolve=self.resolve, dial=self.dial)


_NET: list[Net] = []


def shared_net(on_secret: Callable[[str], None]) -> Net:
    if not _NET:
        _NET.append(Net(on_secret))
    return _NET[0]


def reset_shared() -> None:
    if _NET:
        _NET[0].reset()
