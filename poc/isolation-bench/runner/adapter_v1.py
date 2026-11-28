"""Adaptateur de la v1 (F-005 BR-071, F-008 AC-29) : SEUL composant propre à la passerelle v1 (BR-006, BR-012).

- Provisionnement par la CLI dans le conteneur du banc (`docker compose exec gateway hullgate admin …`) : arbre
  acme/app/prod/{role-a, role-b} (mapping.json), client pré-enregistré
  `bench-gw-client`. Aucune route de test dans l'image (BR-068) : les pannes sont injectées de l'extérieur (fichier de base
  rendu non inscriptible), les jetons falsifiés avec la clé de signature jetable générée par le banc et montée en secret
  (BR-010).
- OAuth entrant : serveur d'autorisation par point d'accès (/o/<id>/oauth/p/<chemin>), barrière TOTP passée avec le secret TOTP
  jetable du banc (BR-071 : comme l'adaptateur des POC saisissait leur secret d'autorisation).
- Relais (F-008) : service `bench` = amont instrumenté du banc, en https (autorité jetable, BR-084) et hôte interne nommé ;
  applications `app1`, `app2` (clients `bench-client-1`, `bench-client-2`) ; connexions OAuth autorisées par l'adaptateur
  (l'URL d'autorisation est jouée contre l'amont, qui rend le code sans interaction ; l'URL de retour est passée à
  `connection authorize`) : `acme/w1` (compte témoin 1) sur le rôle A, `acme/w2` (compte témoin 2) sur le rôle B. Gitea :
  connexions à jeton fourni (jeton
  d'accès Gitea jetable, BR-085) `acme/ga` → `gitea-mcp` (rôle A), `acme/gb` → `gitea-mcp-ro` en lecture seule (rôle B),
  chacun derrière sa frontale ; leurs services prennent l'amont du banc pour la découverte OAuth exigée à la déclaration
  (BR-097), jamais appelée pour un jeton fourni.
- Action policy (F-015, BR-071 v3): the bench upstream publishes no annotation, so each of its tools would be of class
  `destructive` and hidden under the default mode `write` (BR-171, BR-177). Provisioning reads each catalogue (`connection
  tools`), classifies the bench tools (`whoami`, `get_note`, `hidden_admin` as `read`, `write_note`, `flaky` as `write`) and
  the Gitea service's `issue_write` as `write`, then puts `hidden_admin` in the `deny` list of the placements of roles A
  and B: the v1 reading of T09 judges that per-tool masking, next to `whoami`, the visible sibling on the same connection.
- AC-2 of F-015 on real `gitea-mcp`: a dedicated Gitea connection `acme/gr` is placed on its own sensitive reviewer role
  `acme/app/coffre/review` (never `acme/ga`: BR-186 would make role A's placement sensitive) and configured with the five
  commands of the spec's §3.3; a pull request is opened on the disposable Gitea for the review call; the role is reached
  with an operator token placed on it (BR-051).
- Les noms publics naissent à l'annonce (BR-110) : le provisionnement lit `tools/list` des rôles A et B, comme un client.
- Lectures propres à la v1 (mapping.json, `lectures_v1`, AC-29 amendé le 2026-10-31) passées dans les paramètres.
"""
import datetime
import json
import os
import subprocess
import time
import urllib.parse

import httpx_lite as h
import v1_tools as v1

REDIRECT = "http://localhost/callback"
CLIENT = "bench-gw-client"
TREE = ("acme", "app", "prod")
BENCH = "https://upstream:9102"
TOOLS = ("whoami", "get_note", "write_note", "flaky", "hidden_admin")
DENIED = "hidden_admin"   # T09, v1 reading: a read only `deny` hides (F-015)
REVIEWER_ENV = "acme/app/coffre"   # sensitive environment of the AC-2 reviewer (F-015)
REVIEWER_ROLE = "acme/app/coffre/review"
REVIEWER = "acme/gr"               # dedicated Gitea connection, placed on the reviewer role only


class V1Adapter:
    name = "v1"

    def __init__(self, env, registry, gw_url, up_url, compose_dir, mapping, compose_file="v1.compose.yaml"):
        self.env, self.reg = env, registry
        self.gw, self.up = gw_url.rstrip("/"), up_url.rstrip("/")
        self.compose_dir, self.map, self.compose_file = compose_dir, mapping, compose_file
        self.params, self._n = {}, 0
        self.grants = {}
        self._issued = []
        run = os.path.join(compose_dir, ".run")
        with open(os.path.join(run, "signing_key")) as f:
            self._d = v1.private_scalar(f.read())
        with open(os.path.join(run, "authorize_totp")) as f:
            secret = f.read().strip()
        self.reg.add(secret)
        with open(os.path.join(run, "connections_key")) as f:
            self.reg.add(f.read().strip())
        self.totp = v1.Totp(secret)
        self.gitea_token = None
        self.gitea_pull = None
        self._org = None

    # ---- CLI dans le conteneur (SCR-02)
    def _compose(self, *a, stdin=None):
        env = dict(os.environ, COMPOSE_ENV_FILES=os.path.join(self.compose_dir, ".run", "env"))
        return subprocess.run(["docker", "compose", "-f", self.compose_file, *a], cwd=self.compose_dir,
                              capture_output=True, text=True, env=env, input=stdin)

    def cli(self, *args, ok=(0,), stdin=None):
        r = self._compose("exec", "-T", "gateway", "hullgate", "admin", *args, stdin=stdin)
        if r.returncode not in ok:
            raise RuntimeError(f"hullgate {' '.join(args[:2])} : code {r.returncode} {self.reg.redact(r.stderr)[-300:]}")
        return r

    def path(self, role_id):
        return "/".join((*TREE, role_id))

    def org(self):
        """The organisation's public identifier, read once from `tree show / --json` (F-014 §3.3)."""
        if self._org is None:
            self._org = v1.organisation_id(self.cli("tree", "show", "/", "--json").stdout)
        return self._org

    def role_url(self, path):
        return v1.resource_url(self.gw, self.org(), path)

    def url(self, role_id):
        return self.role_url(self.path(role_id))

    # ---- OAuth entrant (client de banc : code + PKCE + barrière TOTP, resource = point d'accès du rôle)
    def authorize_code(self, resource_url, method="S256", challenge=True, scope="mcp offline_access", client=CLIENT, redirect=REDIRECT):
        st, code, err, ver = v1.authorize(self.gw, resource_url, client, redirect, self.totp, method, challenge, scope)
        if code:
            self.reg.add(code)
        self.reg.add(ver)
        return st, code, err, ver

    def authorize_grant(self, resource_url):
        st, code, err, ver = self.authorize_code(resource_url)
        if not code:
            raise RuntimeError(f"authorize: HTTP {st} erreur {err}")
        st, j = v1.token(self.gw, resource_url, {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                                                 "client_id": CLIENT, "code_verifier": ver, "resource": resource_url})
        if st != 200:
            raise RuntimeError(f"token: HTTP {st} {j.get('error')}")
        self._record(j)
        return j

    def _record(self, j):
        for k in ("access_token", "refresh_token"):
            if j.get(k):
                self.reg.add(j[k])
                self._issued.append(j[k])

    def issued_tokens(self):
        return list(self._issued)

    def oauth_refresh(self, label, resources):
        """T03, T04 : renouvellement d'un jeton du rôle `label` avec les ressources données ; une autorisation neuve par appel."""
        url = self.params["profiles"][label]["resource_url"]
        g = self.authorize_grant(url)
        pairs = [("grant_type", "refresh_token"), ("refresh_token", g["refresh_token"]), ("client_id", CLIENT)]
        r = v1.token_raw(self.gw, url, pairs + [("resource", x) for x in resources])
        j = r.json() or {}
        self._record(j)
        return r.status, {k: (v if k not in ("access_token", "refresh_token") else "<jeton>") for k, v in j.items()}

    # ---- hooks des contrôles oauth_checks / socle_checks
    def endpoint(self, resource_url, name):
        return v1.issuer(self.gw, resource_url) + "/" + name

    def as_metadata_url(self, resource_url):
        return f"{self.gw}/.well-known/oauth-authorization-server/o/{v1.org_of(resource_url)}/oauth/p/{v1.path_of(resource_url)}"

    def probe(self, url, tok):
        return v1.ping(url, tok)

    def probe_ok(self, r):
        return v1.ping_ok(r)

    # ---- OAuth sortant : connexion autorisée contre l'amont du banc (F-006, BR-082)
    def _authorize_connection(self, name, opened, confirm_account=False):
        """`opened` : sortie de `connection add` / `reauthorize` portant l'URL d'autorisation ; l'URL est jouée contre l'amont
        du banc (port en clair publié sur l'hôte, même serveur, même journal), qui rend le code par redirection."""
        line = next((l.strip() for l in opened.splitlines() if l.strip().startswith(BENCH + "/authorize?")), None)
        if line is None:
            raise RuntimeError(f"{name} : URL d'autorisation absente de la sortie")
        r = h.request("GET", self.up + line[len(BENCH):])
        back = next((v for k, v in r.headers.items() if k.lower() == "location"), "")
        for c in urllib.parse.parse_qs(urllib.parse.urlparse(back).query).get("code", []):
            self.reg.add(c)
        if r.status != 302 or "code=" not in back:
            raise RuntimeError(f"{name} : l'amont n'a pas rendu de code (HTTP {r.status})")
        extra = ("--confirm-account",) if confirm_account else ()   # modèle generic : compte « unknown » (BR-096)
        self.cli("connection", "authorize", name, "--callback", "-", *extra, stdin=back + "\n")

    def _oauth_connection(self, name, app, prefix):
        r = self.cli("connection", "add", name, "--service", "bench", "--application", app, "--prefix", prefix)
        self._authorize_connection(name, r.stdout)

    def _static_connection(self, name, service, prefix, value):
        self.cli("connection", "add", name, "--service", service, "--provided", "--file", "-", "--prefix", prefix,
                 stdin=value + "\n")

    # ---- provisionnement (SCR-01)
    def provision(self):
        m, e = self.map, self.env
        tok = self._setup_gitea()
        self.cli("tree", "add", "/", "client", "Acme")
        self.cli("tree", "add", "acme", "project", "App")
        self.cli("tree", "add", "acme/app", "environment", "Prod")
        for lab in ("A", "B"):
            rid = m["roles"][lab]["id"]
            self.cli("tree", "add", "acme/app/prod", "role", rid, "--slug", rid)
        self.cli("client", "add", CLIENT, "--path", "acme", "--redirect-uri", REDIRECT, "--name", "client du banc")
        # amont du banc : fournisseur OAuth et serveur MCP, hôte interne nommé (BR-084, BR-106)
        self.cli("service", "declare", "bench", "--server", BENCH, "--model", "generic", "--internal-host")
        self.cli("service", "upstream", "bench", "--type", "mcp-public", "--mcp-url", BENCH + "/mcp", "--mcp-internal-host")
        for n in (1, 2):
            self.cli("application", "declare", f"app{n}", "--service", "bench", "--mode", "declared",
                     "--client-id", f"bench-client-{n}", "--secret-file", "-", stdin=e[f"UP_CLIENT_SECRET_{n}"] + "\n")
        pa, pb = m["roles"]["A"]["prefix"], m["roles"]["B"]["prefix"]
        self._oauth_connection(f"acme/{pa}", "app1", pa)
        self._oauth_connection(f"acme/{pb}", "app2", pb)
        self.cli("connection", "place", f"acme/{pa}", self.path(m["roles"]["A"]["id"]))
        self.cli("connection", "place", f"acme/{pb}", self.path(m["roles"]["B"]["id"]))
        # Gitea : deux serveurs MCP réels derrière leurs frontales, connexions à jeton fourni
        for sid, url in (("gitea", "http://front:8080/mcp"), ("gitearo", "http://front-ro:8080/mcp")):
            self.cli("service", "declare", sid, "--server", BENCH, "--model", "generic", "--internal-host")
            self.cli("service", "upstream", sid, "--type", "mcp-public", "--mcp-url", url, "--mcp-internal-host")
        self._static_connection("acme/ga", "gitea", "ga", tok)
        self._static_connection("acme/gb", "gitearo", "gb", tok)
        self.cli("connection", "place", "acme/ga", self.path(m["roles"]["A"]["id"]))
        self.cli("connection", "place", "acme/gb", self.path(m["roles"]["B"]["id"]))
        self._policy(pa, pb)
        reviewer = self._reviewer(tok)
        profiles = {lab: self._profile(m["roles"][lab]["id"], m["roles"][lab]["prefix"]) for lab in ("A", "B")}
        self.params = {
            "gateway_url": self.gw,
            "identities": {"A": {"role": m["roles"]["A"]["id"]}, "B": {"role": m["roles"]["B"]["id"]}},
            "profiles": profiles,
            "witness": {"A": m["roles"]["A"]["witness"], "B": m["roles"]["B"]["witness"]},
            "connection_ids": {"A": f"acme/{pa}", "B": f"acme/{pb}", "U2": f"acme/{pb}"},
            "foreign_tools": {"U2_whoami": f"{pb}__whoami"},
            "readings": m.get("lectures_v1") or {},
            "denied_tool": DENIED,          # T09, v1 reading: hidden by `deny` (BR-174, BR-176)
            "visible_sibling": "whoami",    # same connection, exposed
            "reviewer": reviewer,           # AC-2 of F-015 on real gitea-mcp
            "gitea_prefixes": {"A": "ga", "B": "gb"},
            "mapping": m,
        }
        # les noms publics naissent à l'annonce (BR-110) : chaque rôle lit sa liste, comme un client
        for lab in ("A", "B"):
            listed = self.profile_tool_names(profiles[lab]["id"], profiles[lab]["token"])
            if not listed or f"{m['roles'][lab]['prefix']}__whoami" not in listed:
                raise RuntimeError(f"rôle {lab} : liste d'outils inattendue ({listed})")
        return self.params

    def _policy(self, pa, pb):
        """F-015: catalogue read by `connection tools` (BR-173), bench classes (BR-172), `hidden_admin` in `deny` (BR-176)."""
        a, b = self.path(self.map["roles"]["A"]["id"]), self.path(self.map["roles"]["B"]["id"])
        for conn, role in ((f"acme/{pa}", a), (f"acme/{pb}", b), ("acme/ga", a), ("acme/gb", b)):
            self.cli("connection", "tools", conn, "--role", role)
        # BR-171: the bench upstream publishes no annotation; gitea-mcp's `issue_write` carries `readOnlyHint: false` without
        # `destructiveHint`: `destructive` by default (F-015 §12), `write` here for the BR-015/BR-022 controls
        for classify in (("tool", "classify", "bench", "whoami", "read"), ("tool", "classify", "bench", "get_note", "read"),
                         ("tool", "classify", "bench", "hidden_admin", "read"),
                         ("tool", "classify", "bench", "write_note", "write"), ("tool", "classify", "bench", "flaky", "write"),
                         ("tool", "classify", "gitea", "issue_write", "write")):
            self.cli(*classify)
        for conn, role in ((f"acme/{pa}", a), (f"acme/{pb}", b)):
            self.cli("policy", "add", conn, "--role", role, "deny", DENIED)

    def _reviewer(self, tok):
        """AC-2 of F-015 on real gitea-mcp: the reviewer of the spec's §3.3 on a sensitive role, its own connection."""
        self.cli("tree", "add", "acme/app", "environment", "Coffre")
        self.cli("tree", "add", REVIEWER_ENV, "role", "Review")
        self.cli("tree", "sensitive", REVIEWER_ENV, "on")
        self._static_connection(REVIEWER, "gitea", "gr", tok)
        self.cli("connection", "place", REVIEWER, REVIEWER_ROLE)
        self.cli("connection", "tools", REVIEWER, "--role", REVIEWER_ROLE)
        for step in (("policy", "add", REVIEWER, "--role", REVIEWER_ROLE, "allowlist", "--class", "read"),
                     ("policy", "add", REVIEWER, "--role", REVIEWER_ROLE, "open", "pull_request_review_write"),
                     ("policy", "arg", REVIEWER, "--role", REVIEWER_ROLE, "pull_request_review_write", "method", "create", "submit"),
                     ("policy", "add", REVIEWER, "--role", REVIEWER_ROLE, "deny", "pull_request_write", "issue_write")):
            self.cli(*step)
        return {"path": REVIEWER_ROLE, "resource_url": self.role_url(REVIEWER_ROLE), "prefix": "gr",
                "token": self.operator_token(REVIEWER_ROLE, "bench-reviewer"), "pull_number": self.gitea_pull}

    def _profile(self, role_id, prefix):
        url = self.url(role_id)
        tok, ref = self.issue_token(None, role_id, url=url)
        names = {t: f"{prefix}__{t}" for t in TOOLS}
        return {"id": role_id, "name": role_id, "resource_url": url, "tools": names, "token": tok, "token_ref": ref, "owner": "operator"}

    def _setup_gitea(self):
        pw = self.env["GITEA_ADMIN_PASSWORD"] + "Aa1!"
        self.reg.add(pw)
        for _ in range(10):
            r = self._compose("exec", "-T", "gitea", "gitea", "admin", "user", "create", "--admin", "--username", "bench", "--password", pw,
                              "--email", "bench@bench.invalid", "--must-change-password=false")
            if r.returncode == 0:
                break
            time.sleep(3)
        else:
            raise RuntimeError("gitea : création de l'utilisateur : " + self.reg.redact(r.stderr)[-200:])
        r = self._compose("exec", "-T", "gitea", "gitea", "admin", "user", "generate-access-token", "--username", "bench", "--token-name", "bench",
                          "--scopes", "all", "--raw")
        tok = r.stdout.strip().splitlines()[-1] if r.returncode == 0 and r.stdout.strip() else ""
        if not tok:
            raise RuntimeError("gitea : jeton non généré : " + self.reg.redact(r.stderr)[-200:])
        self.reg.add(tok)
        self.gitea_token = tok
        r = self._compose("--profile", "late", "up", "-d", "--wait", "--wait-timeout", "120", "gitea-mcp", "gitea-mcp-ro", "front", "front-ro")
        if r.returncode:
            raise RuntimeError("gitea-mcp/frontales : " + self.reg.redact(r.stderr)[-300:])
        for path, body in (("/api/v1/user/repos", {"name": "bench-repo", "auto_init": True}),
                           ("/api/v1/repos/bench/bench-repo/issues", {"title": "ticket témoin", "body": "corps témoin"})):
            rr = self._compose("exec", "-T", "-e", "TOK=" + tok, "gitea", "sh", "-c",
                               f"curl -fsS -X POST -H 'Content-Type: application/json' -H \"Authorization: token $TOK\" -d '{json.dumps(body)}' http://localhost:3000{path}")
            if rr.returncode:
                raise RuntimeError("gitea : données témoins : " + self.reg.redact(rr.stderr)[-200:])
        # AC-2 of F-015: one open pull request for the reviewer's review call (a branch with one file, then the PR)
        for path, body in (("/api/v1/repos/bench/bench-repo/contents/review.txt",
                            {"content": "YmVuY2ggcmV2aWV3Cg==", "message": "bench review file", "new_branch": "bench-review"}),
                           ("/api/v1/repos/bench/bench-repo/pulls", {"head": "bench-review", "base": "main", "title": "bench pull request"})):
            rr = self._compose("exec", "-T", "-e", "TOK=" + tok, "gitea", "sh", "-c",
                               f"curl -fsS -X POST -H 'Content-Type: application/json' -H \"Authorization: token $TOK\" -d '{json.dumps(body)}' http://localhost:3000{path}")
            if rr.returncode:
                raise RuntimeError("gitea: reviewer pull request: " + self.reg.redact(rr.stderr)[-200:])
        self.gitea_pull = json.loads(rr.stdout)["number"]
        return tok

    def issue_token(self, user, profile_id, url=None):
        j = self.authorize_grant(url or self.url(profile_id))
        ref = v1.claims(j["access_token"]).get("jti")
        self.grants[ref] = (j["refresh_token"], url or self.url(profile_id))
        return j["access_token"], ref

    def clone_profile(self, label, src="A"):
        """Rôle jetable de même forme que A (T12) : la connexion du rôle A y est placée (même client, aucun partage)."""
        rid = f"t12-{self._n}"
        self._n += 1
        self.cli("tree", "add", "acme/app/prod", "role", rid, "--slug", rid)
        prefix = self.map["roles"][src]["prefix"]
        self.cli("connection", "place", f"acme/{prefix}", self.path(rid))
        return self._profile(rid, prefix)

    def revoke_token(self, ref):
        """Révocation RFC 7009 du renouvellement de l'autorisation : la lignée tombe, jetons d'accès compris."""
        rt, url = self.grants.get(ref, ("", self.params["profiles"]["A"]["resource_url"]))
        # au serveur d'autorisation de la ressource du jeton : ailleurs, RFC 7009 rend 200 sans effet
        r = v1.revoke(self.gw, url, {"token": rt, "client_id": CLIENT})
        return r.status

    def delete_profile(self, profile_id):
        p = self.path(profile_id)
        r = self.cli("tree", "delete", p, "--confirm", p, ok=(0, 2))
        return 200 if r.returncode == 0 else 404

    def recreate_profile(self, profile_id):
        self.cli("tree", "add", "acme/app/prod", "role", profile_id, "--slug", profile_id)

    def operator_token(self, path, label="banc"):
        r = self.cli("token", "create", "--path", path, "--label", label, "--json")
        value = json.loads(r.stdout)["value"]
        self.reg.add(value)
        return value

    def add_account(self):
        """T10 : une connexion d'un autre compte témoin placée dans le rôle A après sa création (lecture v1, BR-109 : ses
        outils arrivent sous son propre préfixe ; ceux de A et leur compte ne changent pas)."""
        self._n += 1
        name = f"acme/w9{self._n}"
        self._oauth_connection(name, "app2", f"w9{self._n}")
        self.cli("connection", "place", name, self.path(self.map["roles"]["A"]["id"]))

    def reauthorize(self, label):
        """T11 : rejoue l'OAuth sortant de la connexion du rôle (l'amont émet alors des jetons à courte vie), puis attend que le
        jeton n'ait plus que moins de 60 s : le prochain appel le renouvelle avant l'amont (BR-091), sous bail (BR-089)."""
        name = f"acme/{self.map['roles'][label]['prefix']}"
        r = self.cli("connection", "reauthorize", name)
        self._authorize_connection(name, r.stdout, confirm_account=True)
        st = json.loads(self.cli("connection", "status", name, "--json").stdout)
        st = st[0] if isinstance(st, list) else st
        if st.get("expires_at"):
            end = datetime.datetime.fromisoformat(st["expires_at"].replace("Z", "+00:00")).timestamp()
            wait = end - 55 - time.time()
            if 0 < wait < 600:
                time.sleep(wait)

    def profile_tool_names(self, profile_id, token):
        r = h.request("POST", self.url(profile_id), {"Authorization": "Bearer " + token, "Accept": v1.ACCEPT,
                                                     "MCP-Protocol-Version": v1.HANDSHAKE},
                      json_body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        try:
            return sorted(t["name"] for t in r.json()["result"]["tools"])
        except Exception:
            return None

    def read_foreign_connection(self, user, connection_id):
        return h.request("GET", self.gw + f"/connections/{urllib.parse.quote(connection_id, safe='')}",
                         {"Authorization": "Bearer " + self.params["profiles"]["A"]["token"]}).status

    # ---- panne du coffre injectée de l'extérieur : fichier de base non inscriptible pour l'UID 10001
    def _chmod(self, mode):
        r = self._compose("exec", "-T", "-u", "0", "gateway", "python", "-c",
                          f"import os; os.chmod('/data/hullgate.db', {mode})")
        if r.returncode:
            raise RuntimeError("panne du coffre : " + self.reg.redact(r.stderr)[-200:])

    def store_down(self):
        self._chmod(0o444)

    def store_up(self):
        self._chmod(0o644)
        time.sleep(0.2)

    # ---- falsification ES256 avec la clé jetable du banc (BR-010, BR-014)
    def forge(self, base_token, overrides=None, remove=(), key="good", header=None, corrupt_signature=False):
        tok = v1.forge(self._d, base_token, overrides, remove, header, corrupt=(key == "bad" or corrupt_signature))
        self.reg.add(tok)
        return tok

    def claims(self, tok):
        return v1.claims(tok)

    def header(self, tok):
        return v1.header(tok)

    def capability_evidence(self):
        url = self.params.get("profiles", {}).get("A", {}).get("resource_url") or self.url(self.map["roles"]["A"]["id"])
        r = h.request("GET", self.as_metadata_url(url))
        return {"probes": {"/.well-known/oauth-authorization-server/o/<id>/oauth/p/<rôle A>": r.status}, "version": "v1"}

    # ---- sous-contrôles propres à la v1 (socle_checks)
    def logs(self):
        r = self._compose("logs", "--no-color", "gateway")
        return r.stdout + r.stderr

    def dcr_check(self):
        """DCR inactif pendant la recette, puis activé par la CLI : enregistrement RFC 7591, barrière, refus d'un faux code."""
        A = self.params["profiles"]["A"]["resource_url"]
        out = {}
        r = h.request("POST", self.endpoint(A, "register"), json_body={"redirect_uris": ["http://localhost/cb"]})
        md = h.request("GET", self.as_metadata_url(A)).json() or {}
        out["désactivé"] = {"http_register": r.status, "registration_endpoint_annoncé": "registration_endpoint" in md}
        off_ok = r.status == 404 and "registration_endpoint" not in md
        self.cli("client", "dcr", "on")
        try:
            md = h.request("GET", self.as_metadata_url(A)).json() or {}
            out["registration_endpoint_annoncé"] = md.get("registration_endpoint", "").replace(self.gw, "<passerelle>")
            rr = h.request("POST", self.endpoint(A, "register"), json_body={
                "redirect_uris": ["http://localhost/cb"], "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"], "scope": "mcp", "client_name": "banc-dcr"})
            j = rr.json() or {}
            cid = j.get("client_id")
            out["enregistrement"] = {"http": rr.status, "client_id_attribué": bool(cid)}
            ver = "v" * 64
            q = urllib.parse.urlencode({"response_type": "code", "client_id": cid or "", "redirect_uri": "http://localhost/cb",
                                        "scope": "mcp", "state": "st", "resource": A, "code_challenge_method": "S256",
                                        "code_challenge": v1.b64u(__import__("hashlib").sha256(ver.encode()).digest())})
            a = h.request("GET", self.endpoint(A, "authorize") + "?" + q)
            loc = v1.location(a)
            out["autorisation"] = {"http": a.status, "code_délivré": "code=" in loc, "renvoi_vers_la_barrière": loc.endswith("/consent")}
            req = v1.consent_ref(a)
            bad = h.request("POST", self.endpoint(A, "consent"), v1.consent_cookie(req), form={"req": req, "decision": "allow", "totp": "000000"})
            out["code_TOTP_faux"] = {"http": bad.status, "code_délivré": "code=" in v1.location(bad)}
            good = h.request("POST", self.endpoint(A, "consent"), v1.consent_cookie(req), form={"req": req, "decision": "allow", "totp": self.totp.code()})
            gl = v1.location(good)
            out["contrôle_positif_code_TOTP_juste"] = {"http": good.status, "code_délivré": "code=" in gl,
                                                       "vers_la_redirection_enregistrée": gl.startswith("http://localhost/cb?")}
            for c in v1.query(gl).get("code", []):
                self.reg.add(c)
            out["tenu"] = (off_ok and rr.status == 201 and bool(cid) and a.status == 302 and "code=" not in loc and loc.endswith("/consent")
                           and "code=" not in v1.location(bad) and good.status == 302 and "code=" in gl
                           and bool(out["registration_endpoint_annoncé"]))
        finally:
            self.cli("client", "dcr", "off")
        return out

    def sensible_check(self):
        """BR-051 (critère du ticket) : jeton posé sur acme/app refusé sur un rôle d'un environnement sensible."""
        self.cli("tree", "add", "acme/app", "environment", "Sensible")
        self.cli("tree", "add", "acme/app/sensible", "role", "Dev")
        self.cli("tree", "sensitive", "acme/app/sensible", "on")
        project = self.operator_token("acme/app", "banc-projet")
        role = self.operator_token("acme/app/sensible/dev", "banc-rôle")
        s_url = self.role_url("acme/app/sensible/dev")
        a_url = self.params["profiles"]["A"]["resource_url"]
        r1 = v1.ping(s_url, project)
        r2 = v1.ping(a_url, project)
        r3 = v1.ping(s_url, role)
        return {
            "sensible_jeton_de_projet_refusé": {"tenu": r1.status == 401 and 'error="invalid_token"' in _www(r1), "http": r1.status},
            "sensible_contrôle_positif_même_jeton_sur_un_rôle_non_sensible": {"tenu": r2.status == 200 and v1.ping_ok(r2), "http": r2.status},
            "sensible_jeton_posé_sur_le_rôle_accepté": {"tenu": r3.status == 200 and v1.ping_ok(r3), "http": r3.status},
        }


def _www(r):
    return next((v for k, v in r.headers.items() if k.lower() == "www-authenticate"), "")
