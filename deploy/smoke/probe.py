"""Checks of `make prod-smoke` (BR-034, BR-041), standard library only.

    python probe.py traefik <MCP_HOST> <entrypoint> <certresolver>
        played in a throwaway container on Traefik's network: requests through Traefik (SNI and
        Host = MCP_HOST) and reading of its API (routers, services).
    python probe.py client-address-send <MCP_HOST> <MCP_PATH> <CLIENT_IP>
        31 bad credentials through Traefik, first plain then with forged X-Forwarded-For.
    python probe.py client-address <ROLE> <TRUSTED_PROXIES> <A.json> <B.json> <AUDIT.json>
        verifies the two clients' responses and the gateway's audit (BR-315).
    python probe.py edge-readings <MCP_HOST> <TREE_JSON>
        raw requests through Traefik: BR-161 v2 readings and end-to-end 404 checks.
    python probe.py config <ADMIN_LOCAL_PORT>   < output of `docker compose config --format json`
        static reading of the production composition.

Each check prints `CHECK <name> OK|KO <detail>`; exit 0 if all are OK.
Normalisable edge forms only print `READING edge <family> <form-label> <status>`.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
import ssl
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, NamedTuple

PUBLIC_PORT = "4790"
ADMIN_PORT = "4791"
results: list[bool] = []


def check(name: str, ok: bool, detail: str) -> None:
    results.append(ok)
    print(f"CHECK {name} {'OK' if ok else 'KO'} {detail}")


def https_status(host: str, path: str) -> int:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # default certificate of the local Traefik
    conn = http.client.HTTPSConnection("traefik", 443, timeout=5, context=context)
    # Connection to Traefik, SNI and Host = MCP_HOST
    # (as a client resolving MCP_HOST to the production server).
    raw = socket.create_connection(("traefik", 443), timeout=5)
    conn.sock = context.wrap_socket(raw, server_hostname=host)
    conn.request("GET", path, headers={"Host": host})
    status = conn.getresponse().status
    conn.close()
    return status


def api(path: str) -> Any:
    conn = http.client.HTTPConnection("traefik", 8080, timeout=5)
    conn.request("GET", path)
    body = conn.getresponse().read()
    conn.close()
    return json.loads(body)


def traefik(host: str, entrypoint: str, resolver: str, admin_host: str = "") -> None:
    routers = [r for r in api("/api/http/routers") if r.get("provider") == "docker"]
    ours = [r for r in routers if r.get("name") == "hullgate@docker"]
    check("router-declared", len(ours) == 1, f"docker routers for {host}: {len(ours)}")
    if ours:
        r = ours[0]
        check("router-entrypoint", r.get("entryPoints") == [entrypoint], f"{r.get('entryPoints')}")
        resolver_got = r.get("tls", {}).get("certResolver")
        check("router-certresolver", resolver_got == resolver, f"{resolver_got}")
    healthz = https_status(host, "/healthz")
    check("route-healthz", healthz == 200, f"https://{host}/healthz -> {healthz}")
    services = [s for s in api("/api/http/services") if s.get("provider") == "docker"]
    urls = [
        srv.get("url", "") for s in services for srv in s.get("loadBalancer", {}).get("servers", [])
    ]
    if admin_host:
        admin_routers = [r for r in routers if r.get("name") == "hullgate-admin@docker"]
        expected = f"Host(`{admin_host}`) && PathPrefix(`/admin`)"
        correct = len(admin_routers) == 1 and admin_routers[0].get("rule") == expected
        if correct:
            r = admin_routers[0]
            correct = (
                r.get("entryPoints") == [entrypoint]
                and r.get("tls", {}).get("certResolver") == resolver
                and r.get("service") in ("hullgate-admin", "hullgate-admin@docker")
            )
        admin_services = [s for s in services if s.get("name") == "hullgate-admin@docker"]
        backends = [
            v.get("url", "")
            for s in admin_services
            for v in s.get("loadBalancer", {}).get("servers", [])
        ]
        check(
            "admin-routed",
            correct
            and bool(backends)
            and all(u.endswith(":4791") for u in backends)
            and https_status(admin_host, "/admin") == 200
            and https_status(admin_host, "/admin/api/v1/session") == 401,
            "TLS /admin shell and authenticated API, backend 4791",
        )
        if admin_host != host:
            check(
                "public-admin-404",
                https_status(host, "/admin") == 404,
                "MCP name stays public-only",
            )
    else:
        admin = https_status(host, "/admin")
        check("route-admin-404", admin == 404, f"https://{host}/admin -> {admin}")
        check(
            "services-public-only",
            bool(urls) and all(u.endswith(f":{PUBLIC_PORT}") for u in urls),
            f"servers {urls}",
        )
        check(
            "no-admin-backend",
            not any(u.endswith(f":{ADMIN_PORT}") for u in urls),
            f"no server on :{ADMIN_PORT}",
        )


def config(admin_local_port: str, admin_host: str = "") -> None:
    service = json.load(sys.stdin)["services"]["hullgate"]
    ports = service.get("ports", [])
    targets = [str(p.get("target")) for p in ports]
    check("no-public-port", PUBLIC_PORT not in targets, f"published ports {targets}")
    admin = [p for p in ports if str(p.get("target")) == ADMIN_PORT]
    check(
        "admin-loopback-only",
        len(admin) == 1
        and admin[0].get("host_ip") == "127.0.0.1"
        and str(admin[0].get("published")) == admin_local_port,
        f"{admin}",
    )
    labels: dict[str, str] = service.get("labels", {})
    admin_labels = [k for k, v in labels.items() if ADMIN_PORT in v]
    if admin_host:
        env = service["environment"]
        check(
            "admin-route-config",
            bool(admin_labels)
            and env.get("ADMIN_URL") == f"https://{admin_host}"
            and bool(env.get("TRUSTED_PROXIES")),
            "public console prerequisites",
        )
    else:
        check("no-admin-label", not admin_labels, f"labels targeting {ADMIN_PORT}: {admin_labels}")


def client_address_send(host: str, path: str, expected_address: str) -> None:
    """Each probe uses its own socket address, outside the sole trusted proxy's /32."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # local Traefik's default certificate
    started = time.time()
    observations = []
    for i in range(31):
        conn = http.client.HTTPSConnection("traefik", 443, timeout=5, context=context)
        raw = socket.create_connection(("traefik", 443), timeout=5)
        address = raw.getsockname()[0]
        conn.sock = context.wrap_socket(raw, server_hostname=host)
        headers = {"Host": host, "Authorization": "Bearer smoke-invalid"}
        if i:
            headers["X-Forwarded-For"] = "203.0.113.9"
        conn.request("POST", path, body=b"{}", headers=headers)
        response = conn.getresponse()
        observations.append(
            address == expected_address
            and response.status == 401
            and 'Bearer error="invalid_token"' in response.getheader("WWW-Authenticate", "")
            and "resource_metadata=" in response.getheader("WWW-Authenticate", "")
        )
        response.read()
        conn.close()
    print(
        json.dumps(
            {
                "address": expected_address,
                "started": started,
                "finished": time.time(),
                "responses": observations,
            }
        )
    )


def client_address(role: str, trusted: str, first: str, second: str, audit_file: str) -> None:
    """Require 30 ordinary refusals and one limited refusal per client within one window.

    A shared proxy key leaves the second client without its own 30 rows. Trusting the forged
    entry changes the address/counts. Failing to count credentials leaves 31 ordinary rows.
    """
    clients = [json.loads(Path(path).read_text()) for path in (first, second)]
    networks = [ipaddress.ip_network(value) for value in trusted.split(",")]
    addresses = {client["address"] for client in clients}
    valid = (
        len(addresses) == 2
        and all(
            not any(ipaddress.ip_address(address) in network for network in networks)
            for address in addresses
        )
        and all(client["responses"] == [True] * 31 for client in clients)
        and max(client["finished"] for client in clients)
        - min(client["started"] for client in clients)
        < 60
    )
    rows = [row for row in json.loads(Path(audit_file).read_text()) if row.get("path") == role]
    refused: Counter[str] = Counter()
    limited: Counter[str] = Counter()
    for row in rows:
        if row["action"] not in ("access.refused", "rate_limited.refused"):
            continue
        detail = json.loads(row["detail"] or "{}")
        address = detail.get("address", "")
        valid = valid and row["result"] == "refused"
        if row["action"] == "access.refused":
            valid = valid and row["reason"] == "invalid_token"
            refused[address] += 1
        else:
            valid = valid and (
                row["reason"] == "rate_limited"
                and detail.get("surface") == "credential"
                and detail.get("key") == address
                and detail.get("suppressed") == 0
            )
            limited[address] += 1
    valid = valid and refused == Counter({address: 30 for address in addresses})
    valid = valid and limited == Counter({address: 1 for address in addresses})
    check("client-address", valid, f"refusals={dict(refused)}, limited={dict(limited)}")


class EdgeForm(NamedTuple):
    family: str
    label: str
    method: str
    path: str
    expected_status: int | None


def edge_forms(tree: list[dict[str, Any]]) -> list[EdgeForm]:
    """Build the review's forms from the organisation and a role read back by the CLI."""
    identifiers = [node["public_id"] for node in tree if node["level"] == "org"]
    roles = [node["path"] for node in tree if node["level"] == "role"]
    if len(identifiers) != 1 or not roles:
        raise ValueError("edge readings need one organisation and an existing role")
    identifier, role = identifiers[0], roles[0]
    if not re.fullmatch(r"[a-z2-7]{12}", identifier) or not re.fullmatch(
        r"[a-z0-9]+(?:-[a-z0-9]+)*(?:/[a-z0-9]+(?:-[a-z0-9]+)*){3}", role
    ):
        raise ValueError("invalid identifier or role in tree show output")
    if identifier.upper() == identifier:
        raise ValueError("the identifier has no letters for an uppercase reading")
    foreign = ("b" if identifier[0] == "a" else "a") + identifier[1:]
    prefix = f"/o/{identifier}"
    encoded_role = f"%{ord(role[0]):02X}{role[1:]}"
    encoded_id = f"%{ord(identifier[0]):02X}{identifier[1:]}"
    forms: list[EdgeForm] = []
    for family, method, metadata, tail, canonical_status in (
        ("access-point", "POST", "", f"/p/{role}/mcp", 401),
        ("rfc9728", "GET", "/.well-known/oauth-protected-resource", f"/p/{role}/mcp", 200),
        ("rfc8414", "GET", "/.well-known/oauth-authorization-server", f"/oauth/p/{role}", 200),
        ("token", "POST", "", f"/oauth/p/{role}/token", 401),
    ):
        canonical = metadata + prefix + tail
        readings = {
            "dot-foreign-parent": f"{metadata}/o/{foreign}/../{identifier}{tail}",
            "dot-parent": metadata + prefix + "/x/.." + tail,
            "dot-current": metadata + prefix + "/." + tail,
            "dot-encoded-parent": metadata + prefix + "/x/%2e%2e" + tail,
            "dot-encoded-current": metadata + prefix + "/%2e" + tail,
            "slash-prefix": metadata + prefix + "/" + tail,
            "slash-leading": "/" + canonical,
            "unreserved-path": metadata + prefix + tail.replace(role, encoded_role, 1),
            "unreserved-id": f"{metadata}/o/{encoded_id}{tail}",
        }
        refusals = {
            "foreign-id": f"{metadata}/o/{foreign}{tail}",
            "pre-feature": metadata + tail,
            "encoded-slash-prefix": f"{metadata}/o%2F{identifier}{tail}",
            "encoded-slash-path": metadata
            + prefix
            + tail.replace(role, role.replace("/", "%2F", 1), 1),
            "trailing-slash": canonical + "/",
            "uppercase-id": f"{metadata}/o/{identifier.upper()}{tail}",
        }
        # One canonical request per family binds the refusals to a routed gateway.
        # Keep this to 64 requests, without retries: BR-306 counts unknown forms too,
        # while canonical token requests and their normalised readings are exempt.
        forms.append(EdgeForm(family, "canonical", method, canonical, canonical_status))
        forms.extend(
            EdgeForm(family, label, method, path, None) for label, path in readings.items()
        )
        forms.extend(EdgeForm(family, label, method, path, 404) for label, path in refusals.items())
    return forms


def edge_request(host: str, method: str, path: str) -> tuple[int, list[tuple[str, str]], bytes]:
    """Send exact ASCII request-target bytes; HTTPResponse only parses the response."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # local Traefik's default certificate
    request = (
        f"{method} {path} HTTP/1.1\r\nHost: {host}\r\n"
        "Content-Length: 0\r\nConnection: close\r\n\r\n"
    ).encode("ascii")
    with (
        socket.create_connection(("traefik", 443), timeout=5) as raw,
        context.wrap_socket(raw, server_hostname=host) as connection,
    ):
        connection.sendall(request)
        with http.client.HTTPResponse(connection) as response:
            response.begin()
            return response.status, response.getheaders(), response.read()


def edge_404(status: int | str, headers: list[tuple[str, str]], body: bytes) -> bool:
    if status != 404 or any(name.lower() == "location" for name, _ in headers):
        return False
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("error") == "not_found"


def edge_readings(host: str, tree_json: str) -> None:
    for form in edge_forms(json.loads(tree_json)):
        status: int | str
        try:
            status, headers, body = edge_request(host, form.method, form.path)
        except (OSError, http.client.HTTPException) as error:
            status, headers, body = f"ERROR:{type(error).__name__}", [], b""
        if form.expected_status is not None:
            check(
                "edge-404",
                edge_404(status, headers, body)
                if form.expected_status == 404
                else status == form.expected_status,
                f"{form.family} {form.label} {status} "
                f"expected={form.expected_status}"
                f"{'/json-not_found' if form.expected_status == 404 else ''} "
                f"location={any(name.lower() == 'location' for name, _ in headers)}",
            )
        else:
            print(f"READING edge {form.family} {form.label} {status}")


def main() -> int:
    if sys.argv[1:2] == ["traefik"] and len(sys.argv) in (5, 6):
        traefik(*sys.argv[2:])
    elif sys.argv[1:2] == ["config"] and len(sys.argv) in (3, 4):
        config(*sys.argv[2:])
    elif sys.argv[1:2] == ["client-address-send"] and len(sys.argv) == 5:
        client_address_send(*sys.argv[2:])
    elif sys.argv[1:2] == ["client-address"] and len(sys.argv) == 7:
        client_address(*sys.argv[2:])
    elif sys.argv[1:2] == ["edge-readings"] and len(sys.argv) == 4:
        edge_readings(*sys.argv[2:])
    else:
        print(__doc__, file=sys.stderr)
        return 64
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
