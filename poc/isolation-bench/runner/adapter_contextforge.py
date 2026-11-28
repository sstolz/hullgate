"""Adaptateur de passerelle : SEUL composant propre à ContextForge (BR-006).

Il porte le provisionnement, les actions d'administration qu'exigent certains tests (ajout de compte T10,
révocation T12, panne T13) et l'inventaire des fonctions du produit (preuve des lignes « non jouable », BR-009).
Pour le POC Go (HULL-5), on écrit un autre adaptateur ; l'exécuteur (recette.py) ne change pas.
"""
import base64
import hashlib
import hmac
import json
import re
import subprocess
import time

import httpx_lite as h

UPSTREAM_INTERNAL = "http://upstream:9100"


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def decode_claims(tok):
    p = tok.split(".")[1]
    p += "=" * (-len(p) % 4)
    return json.loads(base64.urlsafe_b64decode(p))


class ContextForgeAdapter:
    name = "ContextForge"

    def __init__(self, env, registry, cf_url, up_url, compose_dir):
        self.env, self.reg = env, registry
        self.cf, self.up = cf_url.rstrip("/"), up_url.rstrip("/")
        self.compose_dir = compose_dir
        self.params = {}
        self._n = 0

    # ---- utilitaires HTTP
    def _auth(self, tok):
        return {"Authorization": "Bearer " + tok}

    def _login(self, email, pw):
        r = h.request("POST", self.cf + "/auth/login", json_body={"email": email, "password": pw})
        if r.status != 200:
            raise RuntimeError(f"login {email}: HTTP {r.status}")
        tok = r.json()["access_token"]
        self.reg.add(tok)
        return tok

    def _must(self, r, what, ok=(200, 201)):
        if r.status not in ok:
            raise RuntimeError(f"{what}: HTTP {r.status} {self.reg.redact(r.text)[:200]}")
        return r.json()

    # ---- provisionnement (SCR-01)
    def provision(self):
        e = self.env
        adm = self._login("admin@example.test", e["CF_ADMIN_PASSWORD"])
        users = {"U1": ("u1@example.test", e["U1_PASSWORD"] + "Aa1!"), "U2": ("u2@example.test", e["U2_PASSWORD"] + "Aa1!")}
        for _, (mail, pw) in users.items():
            self._must(h.request("POST", self.cf + "/v1/auth/email/admin/users", self._auth(adm),
                                 json_body={"email": mail, "password": pw, "full_name": mail.split("@")[0], "is_admin": False}), "create user")
        role = self._must(h.request("POST", self.cf + "/v1/rbac/roles", self._auth(adm), json_body={
            "name": "bench-user", "description": "banc", "scope": "global",
            "permissions": ["servers.use", "servers.read", "tools.read", "tools.execute", "gateways.read", "tokens.create",
                            "tokens.read", "tokens.revoke", "tokens.update", "teams.read"]}), "create role")
        dev = next(r["id"] for r in h.request("GET", self.cf + "/v1/rbac/roles", self._auth(adm)).json() if r["name"] == "developer")
        sess, team = {}, {}
        for u, (mail, pw) in users.items():
            self._must(h.request("POST", self.cf + f"/v1/rbac/users/{mail}/roles", self._auth(adm), json_body={"role_id": role["id"], "scope": "global"}), "assign role")
            sess[u] = self._login(mail, pw)
            tl = h.request("GET", self.cf + "/v1/teams/", self._auth(sess[u])).json()
            team[u] = (tl.get("teams", tl) if isinstance(tl, dict) else tl)[0]["id"]
            self._must(h.request("POST", self.cf + f"/v1/rbac/users/{mail}/roles", self._auth(adm),
                                 json_body={"role_id": dev, "scope": "team", "scope_id": team[u]}), "assign team role")
            sess[u] = self._login(mail, pw)
        self.sess, self.team, self.adm, self.users = sess, team, adm, users
        gw = {}
        gw["conn-1"] = self._connect("U1", "conn-1", "1", 1)
        gw["conn-2"] = self._connect("U1", "conn-2", "2", 2)
        gw["conn-u2"] = self._connect("U2", "conn-u2", "u2", 2)
        profiles = {}
        for lab, conn in (("A", "conn-1"), ("B", "conn-2")):
            profiles[lab] = self._make_profile("U1", "profile-" + lab, conn)
        self.params = {
            "gateway_url": self.cf,
            "identities": {"U1": {"email": users["U1"][0]}, "U2": {"email": users["U2"][0]}},
            "profiles": profiles,
            "witness": {"A": "witness-1", "B": "witness-2"},
            "connection_ids": {"A": gw["conn-1"], "B": gw["conn-2"], "U2": gw["conn-u2"]},
            "foreign_tools": {"U2_whoami": "conn-u2-whoami"},
        }
        return self.params

    def _connect(self, user, name, suffix, client_n):
        e = self.env
        body = {"name": name, "url": f"{UPSTREAM_INTERNAL}/mcp/{suffix}", "transport": "STREAMABLEHTTP", "authType": "oauth",
                "visibility": "private", "teamId": self.team[user],
                "oauthConfig": {"grant_type": "authorization_code", "client_id": f"bench-client-{client_n}",
                                "client_secret": e[f"UP_CLIENT_SECRET_{client_n}"], "authorization_url": UPSTREAM_INTERNAL + "/authorize",
                                "token_url": UPSTREAM_INTERNAL + "/token", "redirect_uri": self.cf + "/oauth/callback", "scopes": ["mcp:read"]}}
        gid = self._must(h.request("POST", self.cf + "/v1/gateways", self._auth(self.sess[user]), json_body=body), "create gateway " + name)["id"]
        self._authorize(user, gid)
        self._must(h.request("POST", self.cf + f"/oauth/fetch-tools/{gid}", self._auth(self.sess[user])), "fetch-tools " + name)
        return gid

    def _authorize(self, user, gid):
        r = h.request("GET", self.cf + f"/oauth/authorize/{gid}", self._auth(self.sess[user]))
        loc = r.headers.get("location") or r.headers.get("Location")
        if not loc:
            raise RuntimeError("authorize: pas de redirection")
        r2 = h.request("GET", loc.replace(UPSTREAM_INTERNAL, self.up))
        cb = r2.headers.get("Location")
        r3 = h.request("GET", cb, self._auth(self.sess[user]))
        if r3.status != 200:
            raise RuntimeError(f"callback: HTTP {r3.status}")

    def reauthorize(self, label):
        """Rejoue l'autorisation OAuth de la connexion d'un profil (reprise après perte de session, T11)."""
        self._authorize("U1", self.params["connection_ids"][label])

    def _tools_of(self, user, slug):
        tl = h.request("GET", self.cf + "/v1/tools?limit=200", self._auth(self.sess[user])).json()
        tl = tl if isinstance(tl, list) else tl.get("tools", [])
        return [t for t in tl if t.get("gatewaySlug") == slug]

    def _make_profile(self, user, name, conn_slug):
        tools = self._tools_of(user, conn_slug)
        shown = [t for t in tools if "hidden" not in t["name"]]
        r = h.request("POST", self.cf + "/v1/servers", self._auth(self.sess[user]), json_body={
            "server": {"name": name, "associated_tools": [t["id"] for t in shown], "visibility": "team", "team_id": self.team[user]},
            "team_id": self.team[user], "visibility": "team"})
        sid = self._must(r, "create profile " + name)["id"]
        tok, ref = self.issue_token(user, sid)
        names = {t["name"].split("-", 2)[-1].replace("-", "_"): t["name"] for t in tools}
        return {"id": sid, "name": name, "resource_url": f"{self.cf}/servers/{sid}/mcp", "tools": names, "token": tok, "token_ref": ref, "owner": user}

    # ---- actions d'administration appelées par les tests
    def issue_token(self, user, profile_id):
        r = h.request("POST", self.cf + "/v1/tokens", self._auth(self.sess[user]), json_body={
            "name": f"t{self._n}", "expires_in_days": 1,
            "scope": {"server_id": profile_id, "permissions": ["servers.use", "tools.read", "tools.execute"]}})
        self._n += 1
        j = self._must(r, "issue token")
        tok = j["access_token"]
        self.reg.add(tok)
        ref = (j.get("token") or {}).get("id")
        return tok, ref

    def clone_profile(self, label, src="A"):
        """Profil jetable de même forme que A, avec son propre jeton (T12 le coupe sans abîmer la suite)."""
        conn = {"A": "conn-1", "B": "conn-2"}[src]
        return self._make_profile("U1", f"profile-{label}-{self._n}", conn)

    def revoke_token(self, ref):
        r = h.request("DELETE", self.cf + f"/v1/tokens/{ref}", self._auth(self.sess["U1"]))
        return r.status

    def delete_profile(self, profile_id):
        return h.request("DELETE", self.cf + f"/v1/servers/{profile_id}", self._auth(self.sess["U1"])).status

    def add_account(self):
        """T10 : U1 ajoute une connexion vers l'autre compte témoin après la création du profil A."""
        return self._connect("U1", "conn-1-extra", "extra", 2)

    def profile_tool_names(self, profile_id, token):
        r = h.request("POST", f"{self.cf}/servers/{profile_id}/mcp", {**self._auth(token), "Accept": "application/json, text/event-stream"},
                      json_body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        try:
            return sorted(t["name"] for t in r.json()["result"]["tools"])
        except Exception:
            return None

    def read_foreign_connection(self, user, connection_id):
        return h.request("GET", self.cf + f"/v1/gateways/{connection_id}", self._auth(self.sess[user])).status

    def store_down(self):
        subprocess.run(["docker", "compose", "stop", "postgres"], cwd=self.compose_dir, check=True, capture_output=True)

    def store_up(self):
        subprocess.run(["docker", "compose", "start", "postgres"], cwd=self.compose_dir, check=True, capture_output=True)
        for _ in range(60):
            if h.request("GET", self.cf + "/health").status == 200:
                r = h.request("GET", self.cf + "/health")
                if r.json() and r.json().get("status") == "healthy":
                    return
            time.sleep(1)

    def forge(self, base_token, overrides=None, remove=(), key="good"):
        """Jeton falsifié : claims du jeton de base modifiés, signé HS256 avec la clé du banc ('good') ou une autre ('bad')."""
        claims = decode_claims(base_token)
        claims.update(overrides or {})
        for k in remove:
            claims.pop(k, None)
        hdr = b64u(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        pay = b64u(json.dumps(claims).encode())
        secret = self.env["CF_JWT_SECRET"] if key == "good" else "not-the-secret-" + self.env["PG_PASSWORD"]
        sig = b64u(hmac.new(secret.encode(), f"{hdr}.{pay}".encode(), hashlib.sha256).digest())
        tok = f"{hdr}.{pay}.{sig}"
        self.reg.add(tok)
        return tok

    def claims(self, tok):
        return decode_claims(tok)

    # ---- inventaire des fonctions du produit (preuve de « non jouable », BR-009)
    def capability_evidence(self):
        r = h.request("GET", self.cf + "/openapi.json", self._auth(self.adm))
        paths = sorted(r.json().get("paths", {})) if r.status == 200 else []
        oauth_as = [p for p in paths if re.search(r"/(authorize|token|register|revoke|introspect|jwks)$", p) and not p.startswith(("/oauth/", "/v1/tokens", "/admin"))]
        client_auth_paths = [p for p in paths if p.startswith("/oauth/") or "auth" in p.split("/")[1:2]]
        probes = {}
        for p in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration", "/token", "/authorize", "/oauth/token", "/register", "/oauth/register"):
            rr = h.request("GET" if p != "/token" and "token" not in p and "register" not in p else "POST", self.cf + p)
            probes[p] = rr.status
        return {"openapi_path_count": len(paths), "authorization_server_paths_in_openapi": oauth_as,
                "oauth_paths_in_openapi": client_auth_paths, "probes": probes, "version": self._version()}

    def _version(self):
        r = h.request("GET", self.cf + "/version", self._auth(self.adm))
        return (r.json() or {}).get("app", {}).get("version") if r.status == 200 and isinstance(r.json(), dict) else None
