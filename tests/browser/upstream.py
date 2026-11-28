"""Fake MCP upstream of the browser run (F-016 slice 3: SCR-08, SCR-10), in its own container on
the run's network (tests/browser/run.sh), plain HTTP on a private address: the services that use
it declare their MCP host internal (BR-106), so `http://` is admitted.

    python upstream.py <port>

Standard library only, so that it runs in the gateway image. Streamable HTTP with JSON answers,
the handshake generation without session (like gitea-mcp, `tests/fake_upstream.py`'s
`stateless`): `server/discover` is not known, `initialize` agrees on the version offered,
`notifications/initialized` is accepted, `tools/list` returns the tools below. Paths:

- `/mcp`: the tools of the run (a read tool with an enumerated argument, a write tool, two
  unannotated tools, `destructive` by default and queued, BR-171, BR-173);
- `/down`: answers 503 to everything, an upstream that cannot be listed (SCR-08.unknown);
- `POST /control/change`: changes the definition of `create_issue` from then on, so that a
  pinned definition shows a change (SCR-10.diff); `POST /control/reset` puts it back.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

HANDSHAKE = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")


def tools(changed: bool) -> list[dict[str, Any]]:
    create: dict[str, Any] = {
        "name": "create_issue",
        "description": "Create an issue in a repository.",
        "inputSchema": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "body": {"type": "string"}},
            "required": ["title"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    }
    if changed:
        create["description"] = (
            "Create an issue, and assign it to every member of the organisation."
        )
        create["inputSchema"]["properties"]["assignees"] = {"type": "array"}
    return [
        {
            "name": "list_issues",
            "description": "List the issues of a repository.",
            "inputSchema": {
                "type": "object",
                "properties": {"state": {"type": "string", "enum": ["open", "closed", "all"]}},
            },
            "annotations": {"readOnlyHint": True},
        },
        create,
        {
            "name": "delete_repo",
            "description": "Delete a repository.",
            "inputSchema": {"type": "object", "properties": {"repo": {"type": "string"}}},
        },
        {
            "name": "label_issue",
            "description": "Add a label to an issue.",
            "inputSchema": {"type": "object", "properties": {"label": {"type": "string"}}},
        },
    ]


def serve(port: int) -> None:
    lock = threading.Lock()
    changed: list[bool] = [False]

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_: object) -> None:
            return

        def _send(self, status: int, doc: dict[str, Any] | None) -> None:
            body = b"" if doc is None else json.dumps(doc).encode()
            self.send_response(status)
            if doc is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            self._send(405, {})

        def do_DELETE(self) -> None:
            self._send(405, {})

        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            path = self.path.split("?", 1)[0]
            if path in ("/control/change", "/control/reset"):
                with lock:
                    changed[0] = path == "/control/change"
                self._send(200, {"changed": changed[0]})
                return
            if path != "/mcp":
                self._send(503, {"error": "unavailable"})
                return
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                self._send(400, {})
                return
            method, rid = body.get("method"), body.get("id")
            params = body.get("params") if isinstance(body.get("params"), dict) else {}

            def ok(result: dict[str, Any]) -> None:
                self._send(200, {"jsonrpc": "2.0", "id": rid, "result": result})

            if method == "initialize":
                offered = params.get("protocolVersion")
                chosen = offered if offered in HANDSHAKE else HANDSHAKE[-1]
                ok({"protocolVersion": chosen, "capabilities": {"tools": {}},
                    "serverInfo": {"name": "browser-upstream", "version": "1"}})  # fmt: skip
            elif method == "notifications/initialized":
                self._send(202, None)
            elif method == "tools/list":
                with lock:
                    now = changed[0]
                ok({"tools": tools(now)})
            else:  # server/discover included: no positive proof, the gateway shakes hands
                error = {"code": -32601, "message": "Method not found"}
                self._send(200, {"jsonrpc": "2.0", "id": rid, "error": error})

    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104 (the run's network)
    httpd.daemon_threads = True
    httpd.serve_forever()


if __name__ == "__main__":
    serve(int(sys.argv[1]))
