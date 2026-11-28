#!/usr/bin/env python3
"""Frontale journalisante (F-002, BR-013) : placée entre la passerelle et le serveur MCP Gitea, qu'on ne peut pas instrumenter.

Relais transparent (aval fixe : FRONT_UPSTREAM) ; journalise TOUTE requête reçue (attrape-tout) avec l'empreinte du jeton
reçu, jamais le jeton. Lecture sur le port de contrôle comme l'amont du banc : /now, /journal?since_seq=N.
Bibliothèque standard seulement.
"""
import hashlib
import http.client
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

UP = os.environ.get("FRONT_UPSTREAM", "gitea-mcp:8080")
TOKEN = os.environ["FRONT_CONTROL_TOKEN"]
LOCK, JOURNAL = threading.Lock(), []


def fp(auth):
    if not auth:
        return None
    return hashlib.sha256((auth.split(" ", 1)[1] if " " in auth else auth).encode()).hexdigest()[:12]


class Relay(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def handle_any(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        tool = None
        try:
            m = json.loads(body)
            tool = m.get("method") if m.get("method") != "tools/call" else (m.get("params") or {}).get("name")
        except Exception:
            pass
        with LOCK:
            JOURNAL.append({"seq": len(JOURNAL) + 1, "ts": time.time(), "component": "gitea-mcp", "account": None,
                            "endpoint": urlparse(self.path).path, "tool": tool, "outcome": "received",
                            "method": self.command, "path": urlparse(self.path).path, "token_fp": fp(self.headers.get("Authorization", ""))})
        host, _, port = UP.partition(":")
        c = http.client.HTTPConnection(host, int(port or 80), timeout=60)
        hdr = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection", "content-length", "accept-encoding")}
        try:
            c.request(self.command, self.path, body or None, hdr)
            r = c.getresponse()
            data = r.read()
            self.send_response(r.status)
            for k, v in r.getheaders():
                if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                    self.send_header(k, v)
        except Exception as e:
            data = json.dumps({"error": "front_upstream_error", "detail": str(e)[:80]}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = do_HEAD = do_OPTIONS = handle_any


class Control(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if not secrets.compare_digest(self.headers.get("X-Control-Token", ""), TOKEN):
            out, code = {"error": "forbidden"}, 403
        else:
            u = urlparse(self.path)
            if u.path == "/now":
                out, code = {"now": time.time(), "seq": len(JOURNAL)}, 200
            elif u.path == "/journal":
                with LOCK:
                    out, code = {"now": time.time(), "entries": JOURNAL[int(parse_qs(u.query).get("since_seq", ["0"])[0]):]}, 200
            else:
                out, code = {"error": "not_found"}, 404
        data = json.dumps(out).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(h, port):
    s = ThreadingHTTPServer(("0.0.0.0", port), h)
    s.daemon_threads = True
    s.serve_forever()


if __name__ == "__main__":
    threading.Thread(target=serve, args=(Control, 9201), daemon=True).start()
    serve(Relay, 8080)
