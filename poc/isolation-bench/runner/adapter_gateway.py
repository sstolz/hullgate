"""Adaptateur du POC Go (F-002) : SEUL composant propre à la passerelle POC (BR-006, BR-012).

Il porte le provisionnement (rôles A et B, connexions, instance Gitea jetable), l'administration appelée par les tests
(placement d'une connexion, révocation, suppression d'un rôle, panne du coffre), l'OAuth entrant côté client (code + PKCE,
renouvellement) et la falsification de jetons RS256 avec la clé de test lue à l'administration (BR-010).
La correspondance recette -> rôle vit dans mapping.json (BR-012), pas dans les tests.
"""
import base64
import hashlib
import json
import os
import secrets
import subprocess
import time
import urllib.parse

import httpx_lite as h

REDIRECT = "http://localhost/callback"
CLIENT = "bench-gw-client"
PFX_SHA256 = bytes.fromhex("3031300d060960864801650304020105000420")


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def decode_claims(tok):
    return json.loads(b64d(tok.split(".")[1]))


class GatewayAdapter:
    name = "POC Go"

    def __init__(self, env, registry, gw_url, admin_url, up_url, compose_dir, mapping, front_ctl=None):
        self.env, self.reg = env, registry
        self.gw, self.admin, self.up = gw_url.rstrip("/"), admin_url.rstrip("/"), up_url.rstrip("/")
        self.compose_dir, self.map = compose_dir, mapping
        self.front_ctl = front_ctl
        self.params, self._n = {}, 0
        self.roles, self.grants = {}, {}
        self._key = None
        self._issued = []
        self.gitea_token = None

    # ---- administration
    def _adm(self, method, path, body=None, ok=(200,)):
        r = h.request(method, self.admin + path, {"X-Admin-Token": self.env["GW_ADMIN_TOKEN"]}, json_body=body)
        if r.status not in ok:
            raise RuntimeError(f"administration {method} {path}: HTTP {r.status} {self.reg.redact(r.text)[:200]}")
        return r.json()

    def _compose(self, *a, cap=True):
        env = dict(os.environ, COMPOSE_ENV_FILES=os.path.join(self.compose_dir, ".run", "env"))
        return subprocess.run(["docker", "compose", "-f", "gateway.compose.yaml", *a], cwd=self.compose_dir, capture_output=cap, text=True, env=env)

    # ---- OAuth entrant (client de banc : code + PKCE, resource = point d'accès du rôle)
    def authorize_grant(self, role_url):
        ver = secrets.token_urlsafe(48)
        chal = b64u(hashlib.sha256(ver.encode()).digest())
        q = urllib.parse.urlencode({"response_type": "code", "client_id": CLIENT, "redirect_uri": REDIRECT, "scope": "mcp offline_access",
                                    "state": "st-" + secrets.token_hex(8), "code_challenge": chal, "code_challenge_method": "S256", "resource": role_url})
        r = h.request("GET", self.gw + "/authorize?" + q)
        loc = r.headers.get("Location") or r.headers.get("location") or ""
        code = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query).get("code", [None])[0]
        if not code:
            raise RuntimeError(f"authorize: HTTP {r.status} {self.reg.redact(loc or r.text)[:200]}")
        self.reg.add(code)
        r = h.request("POST", self.gw + "/token", form={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                                                         "client_id": CLIENT, "code_verifier": ver, "resource": role_url})
        if r.status != 200:
            raise RuntimeError(f"token: HTTP {r.status} {self.reg.redact(r.text)[:160]}")
        j = r.json()
        self.reg.add(j["access_token"])
        self.reg.add(j["refresh_token"])
        self._issued += [j["access_token"], j["refresh_token"]]
        return j

    def issued_tokens(self):
        """Tous les jetons émis par la passerelle pendant la session (BR-022 : leurs empreintes ne doivent figurer dans aucun journal amont)."""
        return list(self._issued)

    def oauth_refresh(self, label, resources):
        """Renouvellement d'un jeton du rôle `label` (T03, T04) : retourne (statut HTTP, JSON). Une grant neuve par appel."""
        g = self.authorize_grant(self.params["profiles"][label]["resource_url"])
        form = {"grant_type": "refresh_token", "refresh_token": g["refresh_token"], "client_id": CLIENT}
        data = urllib.parse.urlencode([(k, v) for k, v in form.items()] + [("resource", r) for r in resources])
        r = _post_raw(self.gw + "/token", data)
        j = r.json() or {}
        for k in ("access_token", "refresh_token"):
            if j.get(k):
                self.reg.add(j[k])
                self._issued.append(j[k])
        return r.status, {k: (v if k not in ("access_token", "refresh_token") else "<jeton>") for k, v in j.items()}

    # ---- provisionnement (SCR-01)
    def provision(self):
        e, m = self.env, self.map
        tok = self._setup_gitea()
        ok = (200,)
        for lab in ("A", "B"):
            self._adm("POST", "/admin/roles", {"id": m["roles"][lab]["id"]})
        for lab in ("A", "B"):
            n = m["roles"][lab]["bench_client"]
            self._conn(f"w{n}", n, n)
        self._adm("POST", "/admin/connections", {"id": "gitea", "mcp_url": "http://front:8080/mcp", "token": tok})
        tools_per_role = {}
        for lab in ("A", "B"):
            r = m["roles"][lab]
            self._adm("POST", f"/admin/roles/{r['id']}/expose", {"conn": f"w{r['bench_client']}", "prefix": r["prefix"], "allow": ["*"], "hide": ["hidden_admin"]})
            self._adm("POST", f"/admin/roles/{r['id']}/expose", {"conn": "gitea", "prefix": "gitea", "allow": r["gitea_allow"], "hide": []})
        profiles = {}
        for lab in ("A", "B"):
            profiles[lab] = self._profile(m["roles"][lab]["id"], m["roles"][lab]["prefix"])
        self.params = {
            "gateway_url": self.gw,
            "identities": {"A": {"role": m["roles"]["A"]["id"]}, "B": {"role": m["roles"]["B"]["id"]}},
            "profiles": profiles,
            "witness": {"A": m["roles"]["A"]["witness"], "B": m["roles"]["B"]["witness"]},
            # T08 lu au rôle (spec F-002, table de transposition ; mapping.json) : « U2 » est la connexion placée dans le rôle B,
            # et son outil celui que le rôle B expose ; aucune connexion « placée nulle part » n'est provisionnée
            "connection_ids": {"A": f"w{m['roles']['A']['bench_client']}", "B": f"w{m['roles']['B']['bench_client']}",
                               "U2": f"w{m['roles']['B']['bench_client']}"},
            "foreign_tools": {"U2_whoami": f"{m['roles']['B']['prefix']}_whoami"},
            "mapping": m,
        }
        return self.params

    def _conn(self, cid, client_n, _):
        e = self.env
        return self._adm("POST", "/admin/connections", {
            "id": cid, "mcp_url": f"http://upstream:9100/mcp/{cid}", "auth_url": "http://upstream:9100/authorize",
            "token_url": "http://upstream:9100/token", "client_id": f"bench-client-{client_n}", "client_secret": e[f"UP_CLIENT_SECRET_{client_n}"]})

    def _profile(self, role_id, prefix):
        url = self.gw + f"/p/acme/app/prod/{role_id}/mcp"
        tok, ref = self.issue_token(None, role_id, url=url)
        names = {t: f"{prefix}_{t}" for t in ("whoami", "get_note", "write_note", "flaky", "hidden_admin")}
        names.update({t: f"gitea_{t}" for t in ("list_issues", "issue_read", "issue_write", "get_me")})
        return {"id": role_id, "name": role_id, "resource_url": url, "tools": names, "token": tok, "token_ref": ref, "owner": "operator"}

    def _setup_gitea(self):
        pw = self.env["GITEA_ADMIN_PASSWORD"] + "Aa1!"
        self.reg.add(pw)
        for _ in range(3):
            r = self._compose("exec", "-T", "gitea", "gitea", "admin", "user", "create", "--admin", "--username", "bench", "--password", pw,
                              "--email", "bench@bench.invalid", "--must-change-password=false")
            if r.returncode == 0:
                break
            time.sleep(3)
        else:
            raise RuntimeError("gitea : création de l'utilisateur : " + self.reg.redact(r.stderr)[-200:])
        r = self._compose("exec", "-T", "gitea", "gitea", "admin", "user", "generate-access-token", "--username", "bench", "--token-name", "bench", "--scopes", "all", "--raw")
        tok = r.stdout.strip().splitlines()[-1] if r.returncode == 0 and r.stdout.strip() else ""
        if not tok:
            raise RuntimeError("gitea : jeton non généré : " + self.reg.redact(r.stderr)[-200:])
        self.reg.add(tok)
        self.gitea_token = tok
        r = self._compose("--profile", "late", "up", "-d", "--wait", "--wait-timeout", "120", "gitea-mcp", "front")
        if r.returncode:
            raise RuntimeError("gitea-mcp/frontale : " + self.reg.redact(r.stderr)[-300:])
        for path, body in (("/api/v1/user/repos", {"name": "bench-repo", "auto_init": True}),
                           ("/api/v1/repos/bench/bench-repo/issues", {"title": "ticket témoin", "body": "corps témoin"})):
            rr = self._compose("exec", "-T", "-e", "TOK=" + tok, "gitea", "sh", "-c",
                               f"curl -fsS -X POST -H 'Content-Type: application/json' -H \"Authorization: token $TOK\" -d '{json.dumps(body)}' http://localhost:3000{path}")
            if rr.returncode:
                raise RuntimeError("gitea : données témoins : " + self.reg.redact(rr.stderr)[-200:])
        return tok

    # ---- actions d'administration appelées par les tests
    def issue_token(self, user, profile_id, url=None):
        url = url or self.gw + f"/p/acme/app/prod/{profile_id}/mcp"
        j = self.authorize_grant(url)
        ref = decode_claims(j["access_token"]).get("jti")
        self.grants[ref] = j["refresh_token"]
        return j["access_token"], ref

    def clone_profile(self, label, src="A"):
        """Rôle jetable de même forme que A, avec son propre jeton (T12 le coupe sans abîmer la suite)."""
        rid = f"t12-{self._n}"
        self._n += 1
        r = self.map["roles"][src]
        self._adm("POST", "/admin/roles", {"id": rid})
        self._adm("POST", f"/admin/roles/{rid}/expose", {"conn": f"w{r['bench_client']}", "prefix": r["prefix"], "allow": ["*"], "hide": ["hidden_admin"]})
        return self._profile(rid, r["prefix"])

    def revoke_token(self, ref):
        return h.request("POST", self.admin + "/admin/revoke", {"X-Admin-Token": self.env["GW_ADMIN_TOKEN"]}, json_body={"jti": ref}).status

    def delete_profile(self, profile_id):
        return h.request("DELETE", self.admin + f"/admin/roles/{profile_id}", {"X-Admin-Token": self.env["GW_ADMIN_TOKEN"]}).status

    def add_account(self):
        """T10 : placement d'une connexion (autre compte témoin) dans le rôle A, après sa création, sans l'exposer."""
        self._n += 1
        cid = f"w1x{self._n}"
        self._conn(cid, 2, 2)
        # placée dans le rôle A avec une liste fermée vide : aucun outil ouvert tant que l'administration ne l'ouvre pas outil par outil
        return self._adm("POST", f"/admin/roles/{self.map['roles']['A']['id']}/expose", {"conn": cid, "prefix": cid, "allow": [], "hide": []})

    def reauthorize(self, label):
        """Rejoue l'OAuth sortant de la connexion du rôle (jeton à courte vie, T11)."""
        n = self.map["roles"][label]["bench_client"]
        self._adm("POST", f"/admin/connections/w{n}/authorize")

    def profile_tool_names(self, profile_id, token):
        r = h.request("POST", f"{self.gw}/p/acme/app/prod/{profile_id}/mcp", {"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"},
                      json_body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        try:
            return sorted(t["name"] for t in r.json()["result"]["tools"])
        except Exception:
            return None

    def read_foreign_connection(self, user, connection_id):
        """Un porteur de jeton de rôle lit une connexion d'un autre rôle : aucun point d'accès ne l'expose."""
        return h.request("GET", self.gw + f"/connections/{connection_id}", {"Authorization": "Bearer " + self.params["profiles"]["A"]["token"]}).status

    def store_down(self):
        self._adm("POST", "/admin/store", {"down": True})

    def store_up(self):
        self._adm("POST", "/admin/store", {"down": False})

    # ---- falsification RS256 avec la clé de test (BR-010, BR-014)
    def _signing_key(self):
        if not self._key:
            k = self._adm("GET", "/admin/key")
            self._key = (int(k["n"], 16), int(k["d"], 16))
            self.reg.add(k["d"])
        return self._key

    def forge(self, base_token, overrides=None, remove=(), key="good", header=None, corrupt_signature=False):
        """Jeton falsifié : claims du jeton de base modifiés puis re-signés (RS256). key='bad' : signature altérée d'un octet."""
        n, d = self._signing_key()
        hdr = json.loads(b64d(base_token.split(".")[0]))
        hdr.update(header or {})
        claims = decode_claims(base_token)
        claims.update(overrides or {})
        for k in remove:
            claims.pop(k, None)
        si = b64u(json.dumps(hdr).encode()) + "." + b64u(json.dumps(claims).encode())
        k = (n.bit_length() + 7) // 8
        t = PFX_SHA256 + hashlib.sha256(si.encode()).digest()
        em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
        sig = bytearray(pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big"))
        if key == "bad" or corrupt_signature:
            sig[-1] ^= 0x01
        tok = si + "." + b64u(bytes(sig))
        self.reg.add(tok)
        return tok

    def claims(self, tok):
        return decode_claims(tok)

    def header(self, tok):
        return json.loads(b64d(tok.split(".")[0]))

    # ---- inventaire (conservé pour la forme de l'interface ; le POC a un serveur d'autorisation)
    def capability_evidence(self):
        r = h.request("GET", self.gw + "/.well-known/oauth-authorization-server")
        return {"probes": {"/.well-known/oauth-authorization-server": r.status}, "version": "poc-go"}


def _post_raw(url, data):
    import urllib.request, urllib.error
    req = urllib.request.Request(url, data=data.encode(), headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=30)
        return h.Resp(r.status, dict(r.headers), r.read())
    except urllib.error.HTTPError as e:
        return h.Resp(e.code, dict(e.headers), e.read())
