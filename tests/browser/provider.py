"""Fake OAuth provider of the browser run (BR-216, AC-15): one Gitea-like authorisation server
over HTTPS, in its own container on the run's network (tests/browser/run.sh).

    python provider.py <cert.pem> <key.pem> <host:port>

Standard library only, so that it runs in the gateway image. The authorisation endpoint approves
at once, as an operator who logs in and consents would, and sends the browser back to the
`redirect_uri` it was given with a fresh `code` and the `state`; the token endpoint checks PKCE,
the `redirect_uri` and the client, like `tests/fake_provider.py`, whose behaviour this mirrors
for the one flow the browser plays.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import ssl
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

AUTHORIZE = "/login/oauth/authorize"
TOKEN = "/login/oauth/access_token"  # noqa: S105 (a path, not a secret)
USERINFO = "/login/oauth/userinfo"
REVOKE = "/login/oauth/revoke"
ACCOUNT = "browser-bot"


def serve(cert: str, key: str, netloc: str) -> None:
    base = f"https://{netloc}"
    port = int(netloc.rsplit(":", 1)[1])
    codes: dict[str, dict[str, str]] = {}
    tokens: set[str] = set()
    lock = threading.Lock()

    metadata = {
        "issuer": base + "/",
        "authorization_endpoint": base + AUTHORIZE,
        "token_endpoint": base + TOKEN,
        "userinfo_endpoint": base + USERINFO,
        "revocation_endpoint": base + REVOKE,
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
    }

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_: object) -> None:
            return

        def _send(self, status: int, doc: dict[str, Any], location: str = "") -> None:
            body = json.dumps(doc).encode()
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parts = urlsplit(self.path)
            if parts.path.startswith("/.well-known/"):
                self._send(200, metadata)
            elif parts.path == AUTHORIZE:
                q = {k: v[0] for k, v in parse_qs(parts.query).items()}
                code = "code-" + secrets.token_urlsafe(24)
                with lock:
                    codes[code] = {
                        "challenge": q.get("code_challenge", ""),
                        "redirect_uri": q.get("redirect_uri", ""),
                        "client_id": q.get("client_id", ""),
                        "scope": q.get("scope", ""),
                    }
                back = urlencode({"code": code, "state": q.get("state", "")})
                self._send(302, {}, f"{q.get('redirect_uri', '')}?{back}")
            elif parts.path == USERINFO:
                token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                with lock:
                    known = token in tokens
                if known:
                    self._send(200, {"preferred_username": ACCOUNT, "sub": "id-" + ACCOUNT})
                else:
                    self._send(401, {"error": "invalid_token"})
            else:
                self._send(404, {})

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            form = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
            client_id = form.get("client_id")
            auth = self.headers.get("Authorization") or ""
            if auth.startswith("Basic "):
                client_id = base64.b64decode(auth[6:]).decode().split(":", 1)[0]
            if path == REVOKE:
                self._send(200, {})
                return
            if path != TOKEN:
                self._send(404, {})
                return
            if form.get("grant_type") == "authorization_code":
                with lock:
                    entry = codes.pop(form.get("code", ""), None)
                verifier = form.get("code_verifier", "").encode()
                challenge = (
                    base64.urlsafe_b64encode(hashlib.sha256(verifier).digest()).rstrip(b"=")
                ).decode()
                if (
                    entry is None
                    or challenge != entry["challenge"]
                    or form.get("redirect_uri") != entry["redirect_uri"]
                    or client_id != entry["client_id"]
                ):
                    self._send(400, {"error": "invalid_grant"})
                    return
                scope = entry["scope"]
            elif form.get("grant_type") == "refresh_token":
                scope = ""
            else:
                self._send(400, {"error": "unsupported_grant_type"})
                return
            access = "at-" + secrets.token_urlsafe(24)
            with lock:
                tokens.add(access)
            self._send(
                200,
                {
                    "access_token": access,
                    "token_type": "bearer",
                    "expires_in": 3600,
                    "refresh_token": "rt-" + secrets.token_urlsafe(24),
                    "scope": scope,
                },
            )

    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104 (the run's network)
    httpd.daemon_threads = True
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    httpd.socket = tls.wrap_socket(httpd.socket, server_side=True)
    httpd.serve_forever()


if __name__ == "__main__":
    serve(sys.argv[1], sys.argv[2], sys.argv[3])
