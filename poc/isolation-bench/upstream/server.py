#!/usr/bin/env python3
"""Amont instrumenté du banc d'isolation (F-001, décision B).

Un « fournisseur » jetable : serveur d'autorisation OAuth (code + PKCE, rafraîchissement avec rotation,
client_credentials, introspection) et serveur MCP (streamable HTTP, réponses JSON) exposant des outils aux
données témoins distinctes par compte. Deux comptes témoins : witness-1 (client 1) et witness-2 (client 2).

Journal d'appels (BR-002) : chaque requête reçue par le serveur MCP ou par le serveur d'autorisation est
journalisée (horodatage, composant, compte, point d'accès, outil, issue), sans jeton ni secret.
Le journal se lit sur le port de contrôle, avec un jeton de contrôle propre au banc.
Bibliothèque standard seulement.
"""
import base64
import hashlib
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

PUBLIC = int(os.environ.get("PUBLIC_PORT", "9100"))
CONTROL = int(os.environ.get("CONTROL_PORT", "9101"))
CONTROL_TOKEN = os.environ["UP_CONTROL_TOKEN"]
TTL = {"access": int(os.environ.get("ACCESS_TTL", "300"))}   # durée de vie des jetons d'accès émis ensuite (réglable, T11)
BASE = os.environ.get("PUBLIC_BASE", "http://upstream:9100")

CLIENTS = {
    os.environ["UP_CLIENT_ID_1"]: {"secret": os.environ["UP_CLIENT_SECRET_1"], "account": "witness-1"},
    os.environ["UP_CLIENT_ID_2"]: {"secret": os.environ["UP_CLIENT_SECRET_2"], "account": "witness-2"},
}
DATA = {
    "witness-1": {"note": "donnee-temoin-du-compte-1", "mailbox": ["m1-a", "m1-b"]},
    "witness-2": {"note": "donnee-temoin-du-compte-2", "mailbox": ["m2-a"]},
}

LOCK = threading.Lock()
JOURNAL = []          # entrées {seq, ts, component, account, endpoint, tool, outcome}
CODES = {}            # code -> dict
ACCESS = {}           # access token -> dict(account, client, resource, exp)
REFRESH = {}          # refresh token -> dict(account, client, resource, used)
WRITES = []           # écritures effectives (T16)
FAULT = {"flaky_remaining": 0}


REQ = threading.local()   # requête en cours : méthode, chemin, empreinte du jeton reçu (jamais le jeton)


def fingerprint(auth):
    if not auth:
        return None
    tok = auth.split(" ", 1)[1] if " " in auth else auth
    return hashlib.sha256(tok.encode()).hexdigest()[:12]


def log(component, account, endpoint, tool, outcome):
    with LOCK:
        JOURNAL.append({"seq": len(JOURNAL) + 1, "ts": time.time(), "component": component,
                        "account": account, "endpoint": endpoint, "tool": tool, "outcome": outcome,
                        "method": getattr(REQ, "method", None), "path": getattr(REQ, "path", None), "token_fp": getattr(REQ, "fp", None)})
    REQ.logged = True


def new_pair(account, client, resource, scope):
    at = secrets.token_urlsafe(32)
    rt = secrets.token_urlsafe(32)
    with LOCK:
        ACCESS[at] = {"account": account, "client": client, "resource": resource, "scope": scope, "exp": time.time() + TTL['access']}
        REFRESH[rt] = {"account": account, "client": client, "resource": resource, "scope": scope, "used": False}
    return at, rt


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # pas de journal HTTP bavard (et jamais d'en-têtes)
        pass

    def parse_request(self):
        return super().parse_request()

    def finish_request_log(self, code):
        pass

    # --- utilitaires
    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def send(self, code, obj=None, headers=None, raw=None, ctype="application/json"):
        data = raw if raw is not None else (json.dumps(obj).encode() if obj is not None else b"")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)
        self.finish_request_log(code)

    def client_auth(self, form):
        cid, sec = form.get("client_id", [None])[0], form.get("client_secret", [None])[0]
        h = self.headers.get("Authorization", "")
        if h.lower().startswith("basic "):
            try:
                cid, sec = base64.b64decode(h[6:]).decode().split(":", 1)
            except Exception:
                return None
        c = CLIENTS.get(cid)
        if c and sec is not None and secrets.compare_digest(c["secret"], sec):
            return cid
        return None


class Public(Handler):
    # --- N4 / BR-013 : toute requête est tracée, quelle que soit sa méthode, son chemin ou son issue (attrape-tout)
    def parse_request(self):
        ok = super().parse_request()
        REQ.method, REQ.path, REQ.logged = self.command, urlparse(self.path).path, False
        REQ.fp = fingerprint(self.headers.get("Authorization", "")) if ok else None
        return ok

    def finish_request_log(self, code):
        if not getattr(REQ, "logged", True):
            log("mcp" if REQ.path.startswith("/mcp") else "as", None, REQ.path, None, f"untraced_{code}")

    def handle_one_request(self):
        # filet par construction : une requête dont le traitement lève une exception avant toute réponse est tracée aussi
        REQ.logged = True
        try:
            super().handle_one_request()
        finally:
            if not getattr(REQ, "logged", True):
                log("mcp" if (REQ.path or "").startswith("/mcp") else "as", None, REQ.path, None, "untraced_exception")

    def _any(self):   # PUT, PATCH, HEAD, OPTIONS : tracés aussi
        self.body()
        self.send(405, {"error": "method_not_allowed"})

    do_PUT = do_PATCH = do_HEAD = do_OPTIONS = _any

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/.well-known/oauth-authorization-server":
            return self.send(200, {"issuer": BASE, "authorization_endpoint": BASE + "/authorize", "token_endpoint": BASE + "/token",
                                   "introspection_endpoint": BASE + "/introspect", "response_types_supported": ["code"],
                                   "grant_types_supported": ["authorization_code", "refresh_token", "client_credentials"],
                                   "code_challenge_methods_supported": ["S256"],
                                   "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"]})
        if u.path == "/authorize":
            cid = q.get("client_id", [None])[0]
            if cid not in CLIENTS:
                log("as", None, "/authorize", None, "unknown_client")
                return self.send(400, {"error": "invalid_client"})
            code = secrets.token_urlsafe(24)
            with LOCK:
                CODES[code] = {"client": cid, "account": CLIENTS[cid]["account"], "redirect_uri": q.get("redirect_uri", [""])[0],
                               "resource": q.get("resource", [None])[0], "scope": q.get("scope", [""])[0],
                               "challenge": q.get("code_challenge", [None])[0], "exp": time.time() + 120}
            log("as", CLIENTS[cid]["account"], "/authorize", None, "code_issued")
            loc = q.get("redirect_uri", [""])[0] + ("&" if "?" in q.get("redirect_uri", [""])[0] else "?") + urlencode({"code": code, "state": q.get("state", [""])[0]})
            return self.send(302, headers={"Location": loc})
        self.send(404, {"error": "not_found"})

    def do_POST(self):
        u = urlparse(self.path)
        raw = self.body()
        if u.path == "/token":
            return self.token(parse_qs(raw.decode()))
        if u.path == "/introspect":
            form = parse_qs(raw.decode())
            if not self.client_auth(form):
                return self.send(401, {"error": "invalid_client"})
            t = ACCESS.get(form.get("token", [""])[0])
            log("as", t["account"] if t else None, "/introspect", None, "ok")
            if not t or t["exp"] < time.time():
                return self.send(200, {"active": False})
            return self.send(200, {"active": True, "sub": t["account"], "aud": t["resource"], "exp": int(t["exp"]), "iss": BASE})
        if u.path.startswith("/mcp"):
            return self.mcp(raw)
        self.send(404, {"error": "not_found"})

    def do_DELETE(self):
        self.send(405, {"error": "method_not_allowed"})

    # --- serveur d'autorisation
    def token(self, form):
        gt = form.get("grant_type", [""])[0]
        cid = self.client_auth(form)
        if not cid:
            log("as", None, "/token", gt, "invalid_client")
            return self.send(401, {"error": "invalid_client"})
        acct = CLIENTS[cid]["account"]
        resource = form.get("resource", [None])[0]
        if gt == "authorization_code":
            c = CODES.pop(form.get("code", [""])[0], None)
            ok = c and c["client"] == cid and c["exp"] > time.time()
            if ok and c["challenge"]:
                v = form.get("code_verifier", [""])[0]
                dig = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
                ok = secrets.compare_digest(dig, c["challenge"])
            if not ok:
                log("as", acct, "/token", gt, "invalid_grant")
                return self.send(400, {"error": "invalid_grant"})
            at, rt = new_pair(acct, cid, c["resource"] or resource, c["scope"])
            log("as", acct, "/token", gt, "issued")
            return self.send(200, {"access_token": at, "token_type": "Bearer", "expires_in": TTL['access'], "refresh_token": rt, "scope": c["scope"]})
        if gt == "refresh_token":
            with LOCK:
                r = REFRESH.get(form.get("refresh_token", [""])[0])
                if r and r["client"] == cid and not r["used"]:
                    r["used"] = True
                    good = True
                else:
                    good = False
            if not good:
                log("as", acct, "/token", gt, "invalid_grant")
                return self.send(400, {"error": "invalid_grant"})
            at, rt = new_pair(acct, cid, resource or r["resource"], r["scope"])
            log("as", acct, "/token", gt, "issued")
            return self.send(200, {"access_token": at, "token_type": "Bearer", "expires_in": TTL['access'], "refresh_token": rt})
        if gt == "client_credentials":
            at = secrets.token_urlsafe(32)
            with LOCK:
                ACCESS[at] = {"account": acct, "client": cid, "resource": resource, "scope": "", "exp": time.time() + TTL['access']}
            log("as", acct, "/token", gt, "issued")
            return self.send(200, {"access_token": at, "token_type": "Bearer", "expires_in": TTL['access']})
        log("as", acct, "/token", gt, "unsupported_grant_type")
        self.send(400, {"error": "unsupported_grant_type"})

    # --- serveur MCP
    def mcp(self, raw):
        h = self.headers.get("Authorization", "")
        t = ACCESS.get(h[7:]) if h.lower().startswith("bearer ") else None
        if not t or t["exp"] < time.time():
            log("mcp", None, "/mcp", None, "401")
            return self.send(401, {"error": "invalid_token"}, headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})
        acct = t["account"]
        try:
            msg = json.loads(raw or b"{}")
        except Exception:
            log("mcp", acct, "/mcp", None, "400")
            return self.send(400, {"error": "bad_json"})
        method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method and method.startswith("notifications/"):
            log("mcp", acct, "/mcp", method, "202")
            return self.send(202)
        if method == "initialize":
            log("mcp", acct, "/mcp", "initialize", "200")
            return self.send(200, {"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                                   "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "bench-upstream", "version": "1"}}},
                             headers={"Mcp-Session-Id": secrets.token_hex(8)})
        if method == "tools/list":
            log("mcp", acct, "/mcp", "tools/list", "200")
            tools = [
                {"name": "whoami", "description": "Compte témoin appelé", "inputSchema": {"type": "object", "properties": {}}},
                {"name": "get_note", "description": "Donnée témoin du compte", "inputSchema": {"type": "object", "properties": {}}},
                {"name": "write_note", "description": "Écrit une note (effet de bord)", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
                {"name": "flaky", "description": "Échoue selon le contrôle du banc", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
                {"name": "hidden_admin", "description": "Outil masqué par la politique du profil", "inputSchema": {"type": "object", "properties": {}}},
            ]
            return self.send(200, {"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}})
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            log("mcp", acct, "/mcp", name, "200")
            if name == "whoami":
                out = {"account": acct}
            elif name == "get_note":
                out = {"account": acct, "note": DATA[acct]["note"]}
            elif name == "hidden_admin":
                out = {"account": acct, "admin": True}
            elif name == "write_note":
                with LOCK:
                    WRITES.append({"account": acct, "text": args.get("text"), "ts": time.time()})
                out = {"account": acct, "written": args.get("text")}
            elif name == "flaky":
                with LOCK:
                    fail = FAULT["flaky_remaining"] > 0
                    if fail:
                        FAULT["flaky_remaining"] -= 1
                    if not fail or FAULT.get("mode") == "write_then_error":   # l'effet de bord a lieu, puis l'erreur est renvoyée
                        WRITES.append({"account": acct, "text": args.get("text"), "ts": time.time()})
                if fail:
                    return self.send(200, {"jsonrpc": "2.0", "id": mid, "result": {"isError": True, "content": [{"type": "text", "text": "provider_error"}]}})
                out = {"account": acct, "ok": True}
            else:
                return self.send(200, {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool"}})
            return self.send(200, {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps(out)}], "isError": False}})
        log("mcp", acct, "/mcp", method, "200")
        self.send(200, {"jsonrpc": "2.0", "id": mid, "result": {}})


class Control(Handler):
    def authed(self):
        return secrets.compare_digest(self.headers.get("X-Control-Token", ""), CONTROL_TOKEN)

    def do_GET(self):
        if not self.authed():
            return self.send(403, {"error": "forbidden"})
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/now":
            return self.send(200, {"now": time.time(), "seq": len(JOURNAL)})
        if u.path == "/journal":
            since = int(q.get("since_seq", ["0"])[0])
            with LOCK:
                return self.send(200, {"now": time.time(), "entries": JOURNAL[since:]})
        if u.path == "/writes":
            with LOCK:
                return self.send(200, {"writes": list(WRITES)})
        self.send(404, {"error": "not_found"})

    def do_POST(self):
        if not self.authed():
            return self.send(403, {"error": "forbidden"})
        u = urlparse(self.path)
        data = json.loads(self.body() or b"{}")
        if u.path == "/fault":
            FAULT["flaky_remaining"] = int(data.get("flaky_remaining", 0))
            FAULT["mode"] = data.get("mode", "error_only")
            return self.send(200, FAULT)
        if u.path == "/ttl":
            TTL["access"] = int(data["seconds"])
            return self.send(200, TTL)
        if u.path == "/expire_access":    # force l'expiration des jetons d'accès d'un compte (support de T11)
            with LOCK:
                for v in ACCESS.values():
                    if v["account"] == data.get("account"):
                        v["exp"] = time.time() - 1
            return self.send(200, {"expired": True})
        if u.path == "/revoke_account":   # révocation côté fournisseur (support de T12)
            with LOCK:
                for d in (ACCESS, REFRESH):
                    for k in [k for k, v in d.items() if v["account"] == data.get("account")]:
                        del d[k]
            return self.send(200, {"revoked": True})
        self.send(404, {"error": "not_found"})


def serve(handler, port, tls=None):
    s = ThreadingHTTPServer(("0.0.0.0", port), handler)
    s.daemon_threads = True
    if tls is not None:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(*tls)
        s.socket = ctx.wrap_socket(s.socket, server_side=True)
    s.serve_forever()


if __name__ == "__main__":
    threading.Thread(target=serve, args=(Control, CONTROL), daemon=True).start()
    # Cible v1 (F-008) : la passerelle exige https vers un fournisseur (BR-084) ; même amont, même
    # journal, servi aussi en TLS (certificat d'une autorité jetable du banc, BR-010) quand fourni.
    if os.environ.get("UP_TLS_CERT"):
        tls = (os.environ["UP_TLS_CERT"], os.environ["UP_TLS_KEY"])
        threading.Thread(target=serve, args=(Public, int(os.environ.get("UP_TLS_PORT", "9102")), tls),
                         daemon=True).start()
    serve(Public, PUBLIC)
