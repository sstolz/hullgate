"""Exécuteur de recette d'isolation : T01..T16 de la revue technique du 26 octobre 2026, section 6 (F-001).

BR-006 : ce module ne connaît la passerelle que par HTTP/OAuth. Il n'importe rien de ContextForge. Tout ce qui
est propre au produit (provisionnement, révocation, panne, falsification de jetons, inventaire des fonctions)
passe par l'objet `adapter` ; les identités et les profils sont des paramètres (`params`).
BR-001 : 16 tests, sans transposition ; le décompte est vérifié au chargement.
BR-002 : un refus ne réussit que si le journal amont est vide dans la fenêtre du test.
"""
import http.client
import json
import threading
import re
import time
import urllib.parse

import httpx_lite as h

ACCEPT = "application/json, text/event-stream"
OK, KO, NJ, NP = "réussi", "échoué", "non jouable", "non joué"


class BenchHalt(Exception):
    """SCR-01.error : le banc ne permet pas de poursuivre (journal muet, composant indisponible)."""


class Upstream:
    """Lecture du journal amont (serveur MCP et serveur d'autorisation) : instrument du banc, pas la passerelle.

    F-002 (BR-002, BR-013) : avec `extra` = [(url, jeton de contrôle)…], le journal amont d'une fenêtre est l'UNION des journaux
    (amont du banc + frontale du serveur MCP Gitea) ; `seq()` retourne alors une liste de positions, une par journal."""

    def __init__(self, url, token, extra=()):
        self.url, self.hdr = url.rstrip("/"), {"X-Control-Token": token}
        self.extra = [(u.rstrip("/"), {"X-Control-Token": t}) for u, t in extra]

    def _sources(self):
        return [(self.url, self.hdr)] + self.extra

    def seq(self):
        return [h.request("GET", u + "/now", hd).json()["seq"] for u, hd in self._sources()]

    def since(self, seq):
        time.sleep(1.0)   # laisse arriver les appels tardifs de la passerelle
        seq = seq if isinstance(seq, list) else [seq]
        out = []
        for (u, hd), s in zip(self._sources(), seq):
            out += h.request("GET", u + f"/journal?since_seq={s}", hd).json()["entries"]
        return out

    def all_entries(self):
        return self.since([0] * len(self._sources()))

    def writes(self):
        return h.request("GET", self.url + "/writes", self.hdr).json()["writes"]

    def post(self, path, body):
        return h.request("POST", self.url + path, self.hdr, json_body=body)


def summarize(entries):
    by = {}
    for e in entries:
        k = f"{e['component']}:{e['account']}"
        by[k] = by.get(k, 0) + 1
    return {"count": len(entries), "by_component_account": by,
            "calls": [f"{e['component']} {e['account']} {e['endpoint']} {e['tool']} {e['outcome']}" for e in entries][:12]}


class Ctx:
    def __init__(self, params, adapter, up, reg, policy):
        self.p, self.a, self.up, self.reg, self.policy = params, adapter, up, reg, policy
        self.gw = params["gateway_url"].rstrip("/")

    # --- appels MCP (HTTP) ---
    def mcp(self, url, token, method, params=None, extra=None, rid=1, protocol=None):
        hdr = {"Accept": ACCEPT}
        if token:
            hdr["Authorization"] = "Bearer " + token
        if protocol:
            hdr["MCP-Protocol-Version"] = protocol
        hdr.update(extra or {})
        body = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            body["params"] = params
        return h.request("POST", url, hdr, json_body=body)

    def call(self, prof, tool, args=None, token=None, url=None, extra=None):
        t = prof["tools"].get(tool, tool)
        return self.mcp(url or prof["resource_url"], token or prof["token"], "tools/call", {"name": t, "arguments": args or {}}, extra, rid=3)

    def verdict(self, r):
        """('ok'|'refus', détail) d'après la réponse HTTP / JSON-RPC."""
        if r.status != 200:
            return "refus", f"HTTP {r.status}"
        j = r.json()
        if j is None:
            return "refus", "corps non JSON"
        if "error" in j:
            return "refus", f"JSON-RPC error {j['error'].get('code')} {str(j['error'].get('message'))[:60]}"
        res = j.get("result", {})
        if res.get("isError"):
            return "refus", "isError: " + str(res.get("content", [{}])[0].get("text", ""))[:60]
        return "ok", "HTTP 200"

    def account_of(self, r):
        try:
            return json.loads(r.json()["result"]["content"][0]["text"]).get("account")
        except Exception:
            return None

    def san(self, s):
        return self.reg.redact(s)

    def challenge(self, r):
        return {"status": r.status, "www-authenticate": self.san(next((v for k, v in r.headers.items() if k.lower() == "www-authenticate"), "")),
                "body": self.san(r.text)[:160]}


class Window:
    def __init__(self, ctx):
        self.ctx, self.start = ctx, ctx.up.seq()

    def entries(self):
        return self.ctx.up.since(self.start)


def reading(c, tid):
    """Lecture propre à la cible (mapping.json, `lectures_v1`, AC-29 de F-008 amendé), passée par l'adaptateur ; None
    pour les cibles POC, qui gardent le jugement d'origine."""
    return (c.p.get("readings") or {}).get(tid)


def served_for_a_only(c, win, observed, refused_variant, served_ok, text):
    """Jugement v1 de T07/T08 (BR-115) : la variante « outil de B » est refusée, les autres sont servies pour le compte
    de A, et l'amont de B ne reçoit rien dans la fenêtre (aucune entrée d'un autre compte que celui de A)."""
    ents = win.entries()
    w = c.p["witness"]["A"]
    foreign = [e for e in ents if e["account"] not in (None, w)]
    ok = refused_variant and served_ok and not foreign
    return {"result": OK if ok else KO, "observed": observed, "journal": summarize(ents), "lecture": text,
            "entrées_d_un_autre_compte": len(foreign),
            "reason": "" if ok else "variante refusée servie, appel non servi pour A, ou entrée au journal de l'amont de B"}


def refusal_result(ctx, win, observed, refused_all, note=""):
    ents = win.entries()
    clean = len(ents) == 0
    return {"result": OK if (refused_all and clean) else KO, "observed": observed, "journal": summarize(ents),
            "reason": ("" if refused_all and clean else
                       ("; ".join(x for x in (None if refused_all else "au moins une demande n'a pas été refusée",
                                              None if clean else f"{len(ents)} entrée(s) dans le journal amont après le refus (BR-002)") if x))) + note}


# ---------------------------------------------------------------- les 16 tests
def t01(c):
    A, w = c.p["profiles"]["A"], c.p["witness"]["A"]
    win = Window(c)
    r = c.call(A, "whoami")
    ents = win.entries()
    v, d = c.verdict(r)
    if not [e for e in ents if e["component"] == "mcp" and e["account"] == w]:
        raise BenchHalt("T01 : aucune entrée du journal amont pour le compte témoin attendu : journal muet, aucun refus ne serait prouvable (BR-002)")
    other = [e for e in ents if e["account"] not in (None, w) and e["component"] == "mcp"]
    ok = v == "ok" and c.account_of(r) == w and not other
    return {"result": OK if ok else KO, "observed": [{"http": r.status, "compte": c.account_of(r), "verdict": d}],
            "journal": summarize(ents), "reason": "" if ok else "appel autorisé non abouti ou mauvais compte fournisseur"}


def t02(c):
    A, B = c.p["profiles"]["A"], c.p["profiles"]["B"]
    win, obs = Window(c), []
    r = c.call(B, "whoami", token=A["token"])                     # jeton A sur la ressource B
    ch = c.challenge(r)
    strict = r.status == 401 and "invalid_token" in (ch["www-authenticate"] + ch["body"]).lower()
    obs.append({"variante": "jeton A sur profil B", **ch, "attendu_401_invalid_token": strict})
    refused = c.verdict(r)[0] == "refus"
    r2 = c.mcp(B["resource_url"], A["token"], "tools/list")
    obs.append({"variante": "jeton A, tools/list sur B", **c.challenge(r2)})
    refused &= c.verdict(r2)[0] == "refus"
    r3 = h.request("POST", c.gw + "/rpc", {"Authorization": "Bearer " + A["token"]},
                   json_body={"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": B["tools"]["whoami"], "arguments": {}}})
    obs.append({"variante": "jeton A, point d'accès /rpc, outil de B", **c.challenge(r3)})
    refused &= c.verdict(r3)[0] == "refus"
    res = refusal_result(c, win, obs, refused)
    if res["result"] == OK and not strict:
        res["result"] = KO
        res["reason"] = "refus et journal vide, mais la réponse n'est pas 401 invalid_token comme exigé (HTTP %d)" % r.status
    return res


def refresh_refusals(c, variants, what):
    """F-002 (BR-012) : branche « serveur d'autorisation présent » de T03 et T04. Chaque variante est une demande de
    renouvellement du jeton de A ; elle doit être refusée par le serveur d'autorisation, sans jeton émis ni appel amont.
    Contrôle positif : le même renouvellement avec la ressource A seule aboutit (le refus n'est pas une panne du chemin)."""
    A, B = c.p["profiles"]["A"], c.p["profiles"]["B"]
    win, obs, refused = Window(c), [], True
    for label, res in variants(A["resource_url"], B["resource_url"]):
        st, j = c.a.oauth_refresh("A", res)
        got = "access_token" in j
        obs.append({"variante": label, "http": st, "erreur": j.get("error"), "jeton_émis": got})
        refused &= st >= 400 and not got
    st, j = c.a.oauth_refresh("A", [A["resource_url"]])
    obs.append({"contrôle_positif": "renouvellement avec la ressource A", "http": st, "jeton_émis": "access_token" in j})
    res = refusal_result(c, win, obs, refused)
    if res["result"] == OK and not (st == 200 and "access_token" in j):
        res["result"], res["reason"] = KO, f"contrôle positif en échec (HTTP {st}) : le chemin de renouvellement ne fonctionne pas, le refus ne prouve rien"
    return res


def t03(c):
    if hasattr(c.a, "oauth_refresh"):
        return refresh_refusals(c, lambda a, b: [("renouvellement du jeton de A avec resource = B", [b])], "T03")
    return needs_authorization_server(c, "refresh token de A avec ressource B")


def t04(c):
    if hasattr(c.a, "oauth_refresh"):
        return refresh_refusals(c, lambda a, b: [("renouvellement sans resource", []), ("renouvellement avec resource = A et B", [a, b])], "T04")
    return needs_authorization_server(c, "refresh token de A sans resource ou avec A+B")


def needs_authorization_server(c, what):
    ev = c.a.capability_evidence()
    probes = ev["probes"]
    present = any(v != 404 for v in probes.values())
    if present:
        return {"result": NP, "reason": "un serveur d'autorisation est annoncé : le scénario OAuth côté client n'est pas encore écrit dans l'exécuteur",
                "proof": ev}
    return {"result": NJ, "reason": f"« {what} » exige un serveur d'autorisation pour les clients MCP ; le produit n'en offre pas",
            "proof": ev}


def t05(c):
    A, B = c.p["profiles"]["A"], c.p["profiles"]["B"]
    base_aud = c.a.claims(A["token"])["aud"]
    win, obs, refused = Window(c), [], True
    base_aud = base_aud if isinstance(base_aud, str) else base_aud[0]   # F-002 : l'audience émise peut être une liste à un élément
    variants = [("aud = [audience du produit, ressource B]", [base_aud, B["resource_url"]]),
                ("aud = [ressource A, ressource B]", [A["resource_url"], B["resource_url"]])]
    for label, aud in variants:
        tok = c.a.forge(A["token"], {"aud": aud})
        r = c.call(A, "whoami", token=tok)
        obs.append({"variante": label, "claims_aud": aud, **c.challenge(r)})
        refused &= c.verdict(r)[0] == "refus"
    return refusal_result(c, win, obs, refused)


def t06(c):
    A = c.p["profiles"]["A"]
    now = int(time.time())
    cases = [("signature invalide", dict(key="bad")), ("émetteur invalide", dict(overrides={"iss": "https://evil.invalid"})),
             ("jeton expiré", dict(overrides={"exp": now - 3600}))]
    if getattr(c.a, "name", "") == "ContextForge":     # variante de F-001, rejouable à l'identique contre ContextForge
        cases.append(("ID token à la place d'un access token", dict(overrides={"token_use": "id", "scopes": {}})))
    else:
        # BR-014 (F-002) : chaque variante diffère du jeton de référence par UNE propriété (la variante « ID token » de F-001, qui en
        # changeait deux, est réécrite). Le jeton d'un autre type portant son propre jti est le seul à en nommer deux, déclarées.
        if reading(c, "T06"):   # lecture v1 (BR-054) : le type d'un jeton d'accès est l'en-tête `typ`, pas une claim
            cases += [("type = renouvellement (seule propriété : en-tête typ rt+jwt)", dict(header={"typ": "rt+jwt"})),
                      ("type = identité (seule propriété : en-tête typ JWT)", dict(header={"typ": "JWT"})),
                      ("jeton de renouvellement portant son propre jti (en-tête typ + jti)",
                       dict(header={"typ": "rt+jwt"}, overrides={"jti": "rt-own-jti-0001"}))]
        else:
            cases += [("type = renouvellement (seule propriété token_use)", dict(overrides={"token_use": "refresh"})),
                      ("type = identité (seule propriété token_use)", dict(overrides={"token_use": "id"})),
                      ("jeton de renouvellement portant son propre jti (token_use + jti)", dict(overrides={"token_use": "refresh", "jti": "rt-own-jti-0001"}))]
        cases.append(("jti inconnu (seule propriété jti)", dict(overrides={"jti": "unknown-jti-0001"})))
    win, obs, refused = Window(c), [], True
    bad_shape = []
    ref_claims = c.a.claims(A["token"])
    # relevé des en-têtes (lecture v1 de BR-014 sur `typ`) : seulement pour un adaptateur qui sait lire
    # l'en-tête d'un jeton ; ContextForge n'en a pas et garde son jugement d'origine (BR-006, revue 462 M1)
    header = getattr(c.a, "header", None)
    ref_header = header(A["token"]) if header is not None else {}
    for label, kw in cases:
        tok = c.a.forge(A["token"], **kw)
        r = c.call(A, "whoami", token=tok)
        diff = sorted(k for k in set(ref_claims) | set(c.a.claims(tok)) if ref_claims.get(k) != c.a.claims(tok).get(k))
        if kw.get("key") == "bad":
            diff.append("signature")
        if header is not None:
            diff += sorted(f"en-tête {k}" for k, v in header(tok).items() if ref_header.get(k) != v)
        obs.append({"variante": label, "propriétés_changées": diff, **c.challenge(r)})
        refused &= c.verdict(r)[0] == "refus" and r.status in (401, 403)
        # BR-014 : une seule propriété par variante, sauf le jeton d'un autre type portant son propre jti (deux, déclarées)
        if hasattr(c.a, "oauth_refresh") and len(diff) != (2 if "propre jti" in label else 1):
            bad_shape.append(f"{label} : {diff}")
    res = refusal_result(c, win, obs, refused)
    if reading(c, "T06"):
        res["lecture"] = reading(c, "T06")
    if bad_shape:
        res["result"], res["reason"] = KO, "variante falsifiée hors forme (BR-014) : " + "; ".join(bad_shape)
    if hasattr(c.a, "oauth_refresh"):   # contrôle de référence : le même jeton re-signé sans modification est accepté (les refus ne viennent pas de la re-signature)
        rr = c.call(A, "whoami", token=c.a.forge(A["token"]))
        res["observed"].append({"contrôle_de_référence": "jeton de référence re-signé sans modification", "verdict": c.verdict(rr)[0], "compte": c.account_of(rr)})
        if c.verdict(rr)[0] != "ok":
            res["result"], res["reason"] = KO, "le jeton de référence re-signé est refusé : les refus des variantes ne prouvent rien (BR-014)"
    # contrôle hors verdict (N1) : la version précédente de ce test retirait `jti` en même temps que le type ; on isole ici la seule absence de `jti`
    tok = c.a.forge(A["token"], remove=("jti",))
    r = c.call(A, "whoami", token=tok)
    res["observed"].append({"contrôle_hors_verdict": "jeton sans jti seul (type inchangé)", **c.challenge(r)})
    return res


def t07(c):
    A, B = c.p["profiles"]["A"], c.p["profiles"]["B"]
    cid = c.p["connection_ids"]["B"]
    win, obs, refused = Window(c), [], True
    tests = [("outil de la connexion B appelé depuis A", lambda: c.mcp(A["resource_url"], A["token"], "tools/call", {"name": B["tools"]["whoami"], "arguments": {}}, rid=3)),
             ("argument connection_id = identifiant de B", lambda: c.call(A, "whoami", {"connection_id": cid})),
             ("en-tête X-Connection-Id = identifiant de B", lambda: c.call(A, "whoami", {}, extra={"X-Connection-Id": cid})),
             ("paramètre de requête connection_id = identifiant de B", lambda: c.call(A, "whoami", {}, url=A["resource_url"] + "?connection_id=" + urllib.parse.quote(cid)))]
    text = reading(c, "T07")
    served_ok = True
    for i, (label, fn) in enumerate(tests):
        r = fn()
        v, d = c.verdict(r)
        obs.append({"variante": label, "verdict": v, "detail": d, "compte": c.account_of(r)})
        if text and i > 0:   # lecture v1 : servie pour le compte de A, rien chez B
            served_ok &= v == "ok" and c.account_of(r) == c.p["witness"]["A"]
        else:
            refused &= v == "refus"
    if text:
        return served_for_a_only(c, win, obs, refused, served_ok, text)
    return refusal_result(c, win, obs, refused)


def t08(c):
    A = c.p["profiles"]["A"]
    cid = c.p["connection_ids"]["U2"]
    win, obs, refused = Window(c), [], True
    text = reading(c, "T08")
    r = c.call(A, "whoami", {"connection_id": cid})
    v, d = c.verdict(r)
    obs.append({"variante": "U1 fournit l'identifiant de connexion de U2 (argument)", "verdict": v, "detail": d, "compte": c.account_of(r)})
    served_ok = v == "ok" and c.account_of(r) == c.p["witness"]["A"]
    if not text:
        refused &= v == "refus"
    r = c.mcp(A["resource_url"], A["token"], "tools/call", {"name": c.p["foreign_tools"]["U2_whoami"], "arguments": {}}, rid=3)
    v, d = c.verdict(r)
    obs.append({"variante": "outil de la connexion de U2 appelé par U1", "verdict": v, "detail": d})
    refused &= v == "refus"
    st = c.a.read_foreign_connection("U1", cid)
    obs.append({"variante": "U1 lit la connexion de U2 par l'API d'administration", "http": st})
    refused &= st in (401, 403, 404)
    if text:
        return served_for_a_only(c, win, obs, refused, served_ok, text)
    return refusal_result(c, win, obs, refused)


def t09(c):
    A = c.p["profiles"]["A"]
    hidden = A["tools"]["hidden_admin"]
    listed = c.a.profile_tool_names(A["id"], A["token"]) or []
    generic = [n for n in listed if any(k in n.lower() for k in ("invoke", "call", "exec", "proxy", "run"))]
    win, obs, refused = Window(c), [], True
    text = reading(c, "T09")
    if text:   # v1 reading (BR-174, BR-176): the tool in `deny` is absent, its sibling on the same connection is listed
        sibling = A["tools"][c.p["visible_sibling"]]
        obs.append({"variante": "outil masqué par deny présent dans tools/list", "present": hidden in listed,
                    "outil_frère_de_la_même_connexion_listé": sibling in listed, "outils_génériques_listés": generic,
                    "lecture": text})
        refused &= sibling in listed
    else:
        obs.append({"variante": "outil masqué présent dans tools/list", "present": hidden in listed, "outils_génériques_listés": generic})
    refused &= hidden not in listed
    r = c.mcp(A["resource_url"], A["token"], "tools/call", {"name": hidden, "arguments": {}}, rid=3)
    v, d = c.verdict(r)
    obs.append({"variante": "appel direct de l'outil masqué", "verdict": v, "detail": d})
    refused &= v == "refus"
    r = h.request("POST", c.gw + "/rpc", {"Authorization": "Bearer " + A["token"]},
                  json_body={"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": hidden, "arguments": {}}})
    v, d = c.verdict(r)
    obs.append({"variante": "appel direct par /rpc", "verdict": v, "detail": d})
    refused &= v == "refus"
    for g in generic:   # contournement par un outil d'appel générique
        r = c.mcp(A["resource_url"], A["token"], "tools/call", {"name": g, "arguments": {"name": hidden, "tool": hidden}}, rid=6)
        v, d = c.verdict(r)
        obs.append({"variante": f"contournement par {g}", "verdict": v, "detail": d})
        refused &= v == "refus"
    if text:   # v1 reading (BR-174, BR-176): the upstream of the masked connection (component `mcp`) receives no tool
        # call and no entry names the masked tool; the other entries are recorded outside the verdict
        ents = win.entries()
        called = [e for e in ents if e.get("tool") == c.p["denied_tool"]
                  or (e["component"] == "mcp" and e.get("tool") not in (None, "initialize", "tools/list")
                      and not str(e.get("tool")).startswith("notifications/"))]
        ok = refused and not called
        return {"result": OK if ok else KO, "observed": obs, "journal": summarize(ents), "lecture": text,
                "appels_d_outil_à_l_amont": len(called),
                "autres_entrées_relevées_hors_verdict": summarize([e for e in ents if e not in called]),
                "reason": "" if ok else "outil en deny annoncé ou servi, outil frère absent, ou appel d'outil à l'amont"}
    return refusal_result(c, win, obs, refused)


def t10(c):
    A, w = c.p["profiles"]["A"], c.p["witness"]["A"]
    before = c.a.profile_tool_names(A["id"], A["token"])
    c.a.add_account()
    after = c.a.profile_tool_names(A["id"], A["token"])
    win = Window(c)
    r = c.call(A, "whoami")
    ents = win.entries()
    other = [e for e in ents if e["component"] == "mcp" and e["account"] != w]
    text = reading(c, "T10")
    if text:   # lecture v1 (BR-109) : les outils déjà listés restent, servis par le même compte
        same = before is not None and after is not None and set(before) <= set(after)
    else:
        same = before == after
    ok = same and c.account_of(r) == w and not other
    out = {"result": OK if ok else KO, "observed": [{"outils_avant": before, "outils_apres": after, "compte_apres": c.account_of(r)}],
           "journal": summarize(ents), "reason": "" if ok else "le profil a changé de portée ou de compte après l'ajout d'un compte"}
    if text:
        out["lecture"] = text
    return out


def t11(c):
    A, w = c.p["profiles"]["A"], c.p["witness"]["A"]
    # Jetons fournisseur à courte vie : le prochain appel déclenche un renouvellement ; deux appels simultanés le déclenchent deux fois
    c.up.post("/ttl", {"seconds": 120})
    c.a.reauthorize("A")            # le jeton stocké par la passerelle est désormais à courte vie
    win = Window(c)
    res = [None, None]

    def go(i):
        res[i] = c.call(A, "whoami")
    ts = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    third = c.call(A, "whoami")
    ents = win.entries()
    c.up.post("/ttl", {"seconds": 3600})
    refresh = [e for e in ents if e["component"] == "as" and e["tool"] == "refresh_token"]
    bad = [e for e in refresh if e["outcome"] != "issued"]
    ok = all(c.verdict(x)[0] == "ok" for x in res) and c.verdict(third)[0] == "ok" and not bad
    recovered = None
    if not ok:      # l'état est perdu : l'administrateur doit rejouer l'autorisation (constaté), sinon la suite de la recette est faussée
        c.a.reauthorize("A")
        recovered = c.verdict(c.call(A, "whoami"))[0]
    else:
        c.call(A, "whoami")
    return {"result": OK if ok else KO,
            "observed": [{"appels_simultanes": [c.verdict(x) for x in res], "appel_suivant": c.verdict(third),
                          "renouvellements_émis": len(refresh) - len(bad), "renouvellements_refusés_invalid_grant": len(bad),
                          "après_nouvelle_autorisation_par_l_administrateur": recovered}],
            "journal": summarize(ents), "reason": "" if ok else "renouvellements concurrents : perte ou refus (invalid_grant) du jeton renouvelé"}


def persistent_call(conn, path, token, tool):
    body = json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": tool, "arguments": {}}})
    conn.request("POST", path, body, {"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": ACCEPT})
    r = conn.getresponse()
    raw = r.read()
    return h.Resp(r.status, dict(r.getheaders()), raw)


def t12(c):
    cut = c.policy["cutoff_seconds"]
    obs, all_ok, ents_all = [], True, []
    for label, how in (("révocation du jeton", "revoke"), ("suppression du profil", "delete")):
        P = c.a.clone_profile("t12")
        u = urllib.parse.urlparse(P["resource_url"])
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=30)
        tool = P["tools"]["whoami"]
        pre = persistent_call(conn, u.path, P["token"], tool)
        pre_ok = c.verdict(pre)[0] == "ok"
        pre_detail = c.verdict(pre)[1]
        t0 = time.time()
        st = c.a.revoke_token(P["token_ref"]) if how == "revoke" else c.a.delete_profile(P["id"])
        delay, first = None, None
        while time.time() - t0 < cut + 30:
            r = persistent_call(conn, u.path, P["token"], tool)     # même connexion persistante
            if c.verdict(r)[0] == "refus":
                delay, first = time.time() - t0, r
                break
            time.sleep(1)
        win = Window(c)
        post = [c.verdict(persistent_call(conn, u.path, P["token"], tool))[0] for _ in range(3)]
        post.append(c.verdict(c.call(P, "whoami", token=P["token"]))[0])
        ents = win.entries()
        ents_all += ents
        ok = pre_ok and delay is not None and delay <= cut and all(x == "refus" for x in post) and not ents
        all_ok &= ok
        obs.append({"variante": label, "action_http": st, "appel_avant": pre_ok, "appel_avant_détail": pre_detail, "délai_de_coupure_s": None if delay is None else round(delay, 1),
                    "borne_s": cut, "refus_ensuite": post, "entrées_amont_après_coupure": len(ents)})
        conn.close()
    return {"result": OK if all_ok else KO, "observed": obs, "journal": summarize(ents_all),
            "reason": "" if all_ok else "coupure plus lente que la borne, accès maintenu, ou appel amont après coupure"}


def t13(c):
    A = c.p["profiles"]["A"]
    win, obs, refused = Window(c), [], True
    c.a.store_down()
    try:
        for wait in (0, 5, 15):
            time.sleep(wait)
            r = c.call(A, "whoami")
            v, d = c.verdict(r)
            obs.append({"après_s": wait, "verdict": v, "detail": d, "compte": c.account_of(r)})
            refused &= v == "refus"
    finally:
        c.a.store_up()
    res = refusal_result(c, win, obs, refused)
    back, t0 = "refus", time.time()
    while time.time() - t0 < 120 and back != "ok":
        back = c.verdict(c.call(A, "whoami"))[0]
        if back != "ok":
            time.sleep(3)
    res["observed"].append({"après_rétablissement": back, "reprise_après_s": round(time.time() - t0, 1)})
    if back != "ok":
        res["result"] = NP if res["result"] == OK else res["result"]
        res["reason"] = (res["reason"] + "; " if res["reason"] else "") + "le service n'a pas repris après rétablissement du coffre : suite de la recette compromise"
    return res


def t14(c):
    obs, ok = [], True
    win = Window(c)
    for lab in ("A", "B"):
        P = c.p["profiles"][lab]
        r = c.mcp(P["resource_url"], None, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "bench", "version": "1"}})
        chal = c.challenge(r)
        path = urllib.parse.urlparse(P["resource_url"]).path
        cand = [c.gw + "/.well-known/oauth-protected-resource" + path, c.gw + "/.well-known/oauth-protected-resource"]
        m = urllib.parse.parse_qs(urllib.parse.urlparse(chal["www-authenticate"].replace(" ", "&")).query)
        entry = {"profil": lab, "défi_sans_jeton": chal, "métadonnées": []}
        specific = False
        for u in cand:
            rr = h.request("GET", u)
            j = rr.json() if rr.status == 200 else None
            entry["métadonnées"].append({"url": u.replace(c.gw, "<passerelle>"), "http": rr.status, "resource": j.get("resource") if isinstance(j, dict) else None,
                                         "authorization_servers": j.get("authorization_servers") if isinstance(j, dict) else None})
            if isinstance(j, dict) and j.get("resource", "").rstrip("/").endswith(path.rstrip("/")) and u.endswith(path):
                specific = True
        entry["métadonnées_propres_à_la_ressource"] = specific
        if hasattr(c.a, "oauth_refresh"):
            # F-002 : avec un serveur d'autorisation, le défi 401 désigne les métadonnées de CETTE ressource (RFC 9728 §5.1) et la
            # racine ne sert aucune ressource globale de repli (spec F-002 §3.2, T14 « sans repli sur une audience globale »)
            pointer = re.search(r'resource_metadata="([^"]+)"', chal["www-authenticate"])
            root = entry["métadonnées"][1]
            entry["défi_désigne_les_métadonnées_de_la_ressource"] = bool(pointer) and pointer.group(1).endswith(path)
            entry["aucune_ressource_globale_à_la_racine"] = root["resource"] is None
            specific = specific and entry["défi_désigne_les_métadonnées_de_la_ressource"] and entry["aucune_ressource_globale_à_la_racine"]
        ok &= specific
        obs.append(entry)
    ents = win.entries()
    return {"result": OK if ok else KO, "observed": obs, "journal": summarize(ents),
            "reason": "" if ok else "pas de métadonnées propres à chaque ressource (profil A et/ou B)"}


MODERN = "2026-07-28"   # frontière entre générations : retire les sessions de protocole (revue §4 « Limites »)
LEGACY = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")


def t15(c):
    """Deux générations de clients MCP (B1) : générations avec sessions (versions antérieures à 2026-07-28) ET 2026-07-28 sans session."""
    A, w = c.p["profiles"]["A"], c.p["witness"]["A"]
    url, tok = A["resource_url"], A["token"]
    obs, ok = [], True
    # 1. génération avec sessions de protocole : poignée de main initialize, quatre versions
    for ver in LEGACY:
        un = c.mcp(url, None, "initialize", {"protocolVersion": ver, "capabilities": {}, "clientInfo": {"name": "bench", "version": ver}}, protocol=ver)
        ini = c.mcp(url, tok, "initialize", {"protocolVersion": ver, "capabilities": {}, "clientInfo": {"name": "bench", "version": ver}}, protocol=ver)
        neg = ((ini.json() or {}).get("result") or {}).get("protocolVersion")
        lst = c.mcp(url, tok, "tools/list", protocol=ver)
        cl = c.call(A, "whoami", extra={"MCP-Protocol-Version": ver})
        good = ini.status == 200 and bool(neg) and c.verdict(lst)[0] == "ok" and c.verdict(cl)[0] == "ok" and c.account_of(cl) == w and un.status == 401
        obs.append({"génération": "avec sessions", "version_demandée": ver, "version_négociée": neg, "sans_jeton": un.status, "initialize": ini.status,
                    "tools_list": c.verdict(lst)[0], "appel": c.verdict(cl)[0], "compte": c.account_of(cl),
                    "en_tête_de_session_émis": bool(ini.headers.get("Mcp-Session-Id") or ini.headers.get("mcp-session-id"))})
        ok &= good
    # 2. versions annoncées par la passerelle : la réponse à une version inconnue énumère les versions acceptées
    bad = c.mcp(url, tok, "tools/list", protocol="2099-01-01")
    supported = (((bad.json() or {}).get("error") or {}).get("data") or {}).get("supported")
    obs.append({"preuve": "versions acceptées, d'après la réponse à une version inconnue", "http": bad.status, "supported": supported})
    # 3. génération sans session : 2026-07-28, enveloppe par requête (_meta), sans initialize
    meta = {"io.modelcontextprotocol/protocolVersion": MODERN, "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": "bench", "version": MODERN}}

    def modern(token, method, params=None, name=None):
        p = dict(params or {}, _meta=meta)
        ex = {"Mcp-Method": method}
        if name:
            ex["Mcp-Name"] = name
        return c.mcp(url, token, method, p, protocol=MODERN, extra=ex)

    disc = modern(tok, "server/discover")
    dres = (disc.json() or {}).get("result") or {}
    tname = A["tools"]["whoami"]
    lst = modern(tok, "tools/list")
    cl = modern(tok, "tools/call", {"name": tname, "arguments": {}}, name=tname)
    un = modern(None, "tools/list")
    sess = [bool(r.headers.get("Mcp-Session-Id") or r.headers.get("mcp-session-id")) for r in (disc, lst, cl)]
    announced = MODERN in (supported or []) and MODERN in (dres.get("supportedVersions") or [])
    good = (announced and disc.status == 200 and c.verdict(lst)[0] == "ok" and c.verdict(cl)[0] == "ok" and c.account_of(cl) == w
            and un.status == 401 and not any(sess))
    obs.append({"génération": "sans session", "version": MODERN, "server_discover": disc.status, "supportedVersions_annoncées": dres.get("supportedVersions"),
                "serveur": (dres.get("_meta") or {}).get("io.modelcontextprotocol/serverInfo"), "tools_list": c.verdict(lst)[0],
                "appel": c.verdict(cl)[0], "compte": c.account_of(cl), "sans_jeton": un.status, "en_tête_de_session_émis": any(sess)})
    ok &= good
    if not announced and disc.status != 200:
        # la version n'est ni annoncée ni acceptée : la fonction est absente (BR-009), preuve citée
        return {"result": NJ, "observed": obs, "journal": {"count": None},
                "reason": f"la passerelle n'accepte pas {MODERN} (versions acceptées : {supported}) ; server/discover : HTTP {disc.status}"}
    return {"result": OK if ok else KO, "observed": obs, "journal": {"count": None},
            "reason": "" if ok else "négociation ou autorisation en échec pour une version ou une génération"}


def t16(c):
    A, w, B = c.p["profiles"]["A"], c.p["witness"]["A"], c.p["witness"]["B"]
    marker = "t16-" + str(int(time.time()))
    c.up.post("/fault", {"flaky_remaining": 1, "mode": "write_then_error"})
    win = Window(c)
    r = c.call(A, "flaky", {"text": marker})
    v, d = c.verdict(r)
    time.sleep(3)   # laisse passer une éventuelle reprise automatique
    ents = win.entries()
    c.up.post("/fault", {"flaky_remaining": 0})
    flaky_calls = [e for e in ents if e["component"] == "mcp" and e["tool"] == "flaky"]
    other = [e for e in ents if e["account"] not in (None, w)]
    writes = [x for x in c.up.writes() if x["text"] == marker]
    ok = v == "refus" and len(flaky_calls) == 1 and len(writes) == 1 and not other
    return {"result": OK if ok else KO,
            "observed": [{"réponse_au_client": v, "detail": d, "appels_amont_flaky": len(flaky_calls), "écritures_effectives": len(writes),
                          "entrées_autre_compte": len(other)}],
            "journal": summarize(ents), "reason": "" if ok else "reprise automatique (double écriture), repli vers un autre compte, ou erreur masquée au client"}


TESTS = [("T01", "Access token A sur A, action autorisée", t01), ("T02", "Access token A sur B", t02),
         ("T03", "Refresh token A avec ressource B", t03), ("T04", "Refresh A sans resource ou avec A+B", t04),
         ("T05", "Jeton à audiences A et B", t05), ("T06", "Signature, émetteur ou expiration invalides", t06),
         ("T07", "Appel depuis A avec connection_id de B", t07), ("T08", "U1 fournit un identifiant de connexion U2", t08),
         ("T09", "Outil masqué appelé directement", t09), ("T10", "Ajout d'un compte après création du profil", t10),
         ("T11", "Deux renouvellements simultanés", t11), ("T12", "Révocation ou suppression du profil", t12),
         ("T13", "Coffre / contrôle de révocation indisponible", t13), ("T14", "Découverte OAuth sur A et B", t14),
         ("T15", "Deux générations de clients MCP", t15), ("T16", "Reprise après erreur fournisseur", t16)]

EXPECTED_TESTS = 16
if len(TESTS) != EXPECTED_TESTS:     # BR-001 : le décompte est vérifié au chargement
    raise SystemExit(f"recette invalide : {len(TESTS)} tests déclarés, 16 attendus (BR-001)")


def check_mapping(mapping):
    """BR-012 : chaque test T01..T16 a une ligne de correspondance dans le fichier lu ; sinon le démarrage s'arrête."""
    missing = [tid for tid, _, _ in TESTS if tid not in (mapping or {}).get("tests", {})]
    if missing:
        raise SystemExit(f"recette mal transposée (BR-012) : pas de ligne de correspondance pour {', '.join(missing)}")


def probe_exhaustiveness(sources, log=print):
    """BR-013 : avant le premier test, une requête GET, une DELETE et une sur chemin inconnu (et un POST inconnu) produisent chacune
    une entrée sur chaque journal ; une requête non tracée arrête le banc. sources : [(nom, url publique, Upstream à un journal)]."""
    out = []
    for name, pub, ctl in sources:
        before = ctl.seq()[0]
        sent = [("GET", "/mcp"), ("DELETE", "/mcp"), ("GET", "/chemin-inconnu-br013"), ("POST", "/chemin-inconnu-br013")]
        for m, p in sent:
            h.request(m, pub.rstrip("/") + p, {"Accept": "application/json"}, json_body={} if m == "POST" else None)
        ents = ctl.since([before])
        got = sorted({(e.get("method"), e.get("path")) for e in ents})
        miss = [x for x in sent if x not in got]
        out.append({"journal": name, "requêtes_envoyées": [f"{m} {p}" for m, p in sent], "entrées": len(ents), "entrées_lues": [f"{a} {b}" for a, b in got], "manquantes": [f"{m} {p}" for m, p in miss]})
        if miss:
            raise BenchHalt(f"BR-013 : le journal « {name} » n'a pas tracé {miss} ; une absence d'entrée ne prouverait pas l'absence d'appel")
    return out


def gitea_e2e(c, front_ctl):
    """BR-015 (relais de bout en bout) et BR-022 (jeton entrant jamais transmis), contre l'instance Gitea jetable."""
    import hashlib
    A, B = c.p["profiles"]["A"], c.p["profiles"]["B"]
    fp = lambda t: hashlib.sha256(t.encode()).hexdigest()[:12]
    la = c.a.profile_tool_names(A["id"], A["token"]) or []
    lb = c.a.profile_tool_names(B["id"], B["token"]) or []
    obs = {"outils_A": la, "outils_B": lb}
    win = Window(c)
    rd = c.call(A, "list_issues", {"owner": "bench", "repo": "bench-repo"})
    wr = c.call(A, "issue_write", {"method": "create", "owner": "bench", "repo": "bench-repo", "title": "ticket écrit par le banc via la passerelle"})
    ents = win.entries()
    front = [e for e in ents if e["component"] == "gitea-mcp"]
    conn_fp = fp(c.a.gitea_token)
    incoming = {fp(A["token"]), fp(B["token"])}
    obs.update({"lecture": c.verdict(rd), "écriture": c.verdict(wr), "lecture_contient_ticket_témoin": "ticket témoin" in rd.text,
                "entrées_frontale": len(front), "empreinte_du_jeton_de_connexion_reçue": conn_fp in {e["token_fp"] for e in front},
                "empreinte_d_un_jeton_entrant_reçue": bool(incoming & {e["token_fp"] for e in front})})
    win2 = Window(c)
    denied = c.call(B, "issue_write", {"method": "create", "owner": "bench", "repo": "bench-repo", "title": "ne doit pas exister"}, token=B["token"])
    ents2 = win2.entries()
    obs.update({"B_possède_issue_write": "gitea_issue_write" in lb, "B_appel_refusé": c.verdict(denied), "entrées_frontale_après_refus": len([e for e in ents2 if e["component"] == "gitea-mcp"])})
    ok = (c.verdict(rd)[0] == "ok" and c.verdict(wr)[0] == "ok" and obs["lecture_contient_ticket_témoin"] and "gitea_issue_write" in la
          and "gitea_issue_write" not in lb and c.verdict(denied)[0] == "refus" and not ents2 and obs["empreinte_du_jeton_de_connexion_reçue"]
          and not obs["empreinte_d_un_jeton_entrant_reçue"] and "gitea_list_issues" in la and "ping" in la)
    return {"id": "BR-015/BR-022", "title": "Relais Gitea de bout en bout, jeton entrant non transmis", "result": OK if ok else KO, "observed": [obs],
            "reason": "" if ok else "relais Gitea ou masquage ou transmission du jeton entrant en défaut (voir observed)"}


def gitea_e2e_v1(c):
    """BR-015 et BR-022 contre la v1 (F-008, AC-29 ; lecture v1 du 2026-10-31) : lecture et écriture du rôle A servies par
    `gitea-mcp` réel sur l'instance Gitea jetable ; le rôle B passe par un second `gitea-mcp` en lecture seule (`-r`) : son
    outil d'écriture est absent de sa liste et un appel direct est refusé sans entrée aux journaux des frontales ; aucun
    jeton entrant n'atteint une frontale, le jeton de la connexion oui."""
    import hashlib
    A, B = c.p["profiles"]["A"], c.p["profiles"]["B"]
    ga, gb = c.p["gitea_prefixes"]["A"], c.p["gitea_prefixes"]["B"]
    fp = lambda t: hashlib.sha256(t.encode()).hexdigest()[:12]
    la = c.a.profile_tool_names(A["id"], A["token"]) or []
    lb = c.a.profile_tool_names(B["id"], B["token"]) or []
    obs = {"outils_gitea_A": [n for n in la if n.startswith(ga + "__")], "outils_gitea_B": [n for n in lb if n.startswith(gb + "__")]}
    win = Window(c)
    rd = c.call(A, f"{ga}__list_issues", {"owner": "bench", "repo": "bench-repo"})
    wr = c.call(A, f"{ga}__issue_write", {"method": "create", "owner": "bench", "repo": "bench-repo",
                                         "title": "ticket écrit par le banc via la passerelle v1"})
    rb = c.call(B, f"{gb}__list_issues", {"owner": "bench", "repo": "bench-repo"}, token=B["token"])
    ents = win.entries()
    front = [e for e in ents if e["component"] == "gitea-mcp"]
    incoming = {fp(t) for t in c.a.issued_tokens()}
    conn_fp = fp(c.a.gitea_token)
    obs.update({"lecture_A": c.verdict(rd), "écriture_A": c.verdict(wr), "lecture_B": c.verdict(rb),
                "lecture_contient_ticket_témoin": "ticket témoin" in rd.text, "entrées_frontales": len(front),
                "empreinte_du_jeton_de_connexion_reçue": conn_fp in {e["token_fp"] for e in front},
                "empreinte_d_un_jeton_entrant_reçue": bool(incoming & {e["token_fp"] for e in front})})
    win2 = Window(c)
    denied = c.call(B, f"{gb}__issue_write", {"method": "create", "owner": "bench", "repo": "bench-repo", "title": "ne doit pas exister"},
                    token=B["token"])
    ents2 = win2.entries()
    obs.update({"B_possède_l_écriture": f"{gb}__issue_write" in lb, "B_appel_refusé": c.verdict(denied),
                "entrées_frontales_après_refus": len([e for e in ents2 if e["component"] == "gitea-mcp"]),
                "lecture": reading(c, "BR-015")})
    ok = (c.verdict(rd)[0] == "ok" and c.verdict(wr)[0] == "ok" and c.verdict(rb)[0] == "ok" and obs["lecture_contient_ticket_témoin"]
          and f"{ga}__issue_write" in la and f"{ga}__list_issues" in la and f"{gb}__list_issues" in lb
          and f"{gb}__issue_write" not in lb and c.verdict(denied)[0] == "refus" and not ents2
          and obs["empreinte_du_jeton_de_connexion_reçue"] and not obs["empreinte_d_un_jeton_entrant_reçue"])
    return {"id": "BR-015/BR-022", "title": "Relais Gitea de bout en bout (gitea-mcp réel), jeton entrant non transmis", "result": OK if ok else KO,
            "observed": [obs], "reason": "" if ok else "relais Gitea, lecture seule de B ou transmission du jeton en défaut (voir observed)"}


def reviewer_ac2_v1(c):
    """AC-2 of F-015 against real `gitea-mcp`: on a sensitive reviewer role configured as in the spec's §3.3,
    `pull_request_write` is absent from `tools/list`, a direct call answers "unknown tool", the frontal journal holds no
    call for it, and `pull_request_review_write` works (a COMMENT review on the bench pull request); its announced
    `method` enum holds the allowed values only (BR-179)."""
    rv = c.p["reviewer"]
    p = rv["prefix"]
    listed = c.mcp(rv["resource_url"], rv["token"], "tools/list")
    tools = (listed.json() or {}).get("result", {}).get("tools", []) if listed.status == 200 else []
    names = sorted(t["name"] for t in tools)
    review = next((t for t in tools if t["name"] == f"{p}__pull_request_review_write"), None)
    enum = ((review or {}).get("inputSchema", {}).get("properties", {}).get("method", {}) or {}).get("enum")
    win = Window(c)
    merge = c.mcp(rv["resource_url"], rv["token"], "tools/call",
                  {"name": f"{p}__pull_request_write",
                   "arguments": {"method": "merge", "owner": "bench", "repo": "bench-repo", "pull_number": rv["pull_number"]}}, rid=3)
    merge_v = c.verdict(merge)
    merge_err = ((merge.json() or {}).get("error") or {}).get("code") if merge.status == 200 else None
    served = c.mcp(rv["resource_url"], rv["token"], "tools/call",
                   {"name": f"{p}__pull_request_review_write",
                    "arguments": {"method": "create", "owner": "bench", "repo": "bench-repo", "pull_number": rv["pull_number"],
                                  "state": "COMMENT", "body": "bench review through the gateway"}}, rid=4)
    served_v = c.verdict(served)
    front = [e for e in win.entries() if e["component"] == "gitea-mcp"]
    merges = [e for e in front if e.get("tool") == "pull_request_write"]
    reviews = [e for e in front if e.get("tool") == "pull_request_review_write"]
    obs = {"tools_listed": len(names), "pull_request_write_listed": f"{p}__pull_request_write" in names,
           "pull_request_review_write_listed": review is not None, "announced_method_enum": enum,
           "direct_merge_call": merge_v, "direct_merge_jsonrpc_error": merge_err,
           "frontal_entries_for_pull_request_write": len(merges), "review_call": served_v,
           "frontal_entries_for_pull_request_review_write": len(reviews),
           "other_frontal_entries": summarize([e for e in front if e not in merges and e not in reviews])}
    ok = (listed.status == 200 and not obs["pull_request_write_listed"] and review is not None
          and enum == ["create", "submit"] and merge_v[0] == "refus" and merge_err == -32602 and not merges
          and served_v[0] == "ok" and len(reviews) == 1)
    return {"id": "AC-2/F-015", "title": "The reviewer does not see the merge tool (real gitea-mcp, sensitive role, spec §3.3)",
            "result": OK if ok else KO, "observed": [obs],
            "reason": "" if ok else "merge tool listed or reached, or review call not served (see observed)"}


def run(params, adapter, up, reg, policy, only=None, log=print, not_playable=None):
    """`not_playable` (F-005, cible v1) : {id: {"reason", "proof"}} fourni par l'adaptateur, pour les tests dont un pas a
    besoin d'une fonction absente (BR-009 : preuve citée) ; ils ne sont pas joués et ne comptent jamais comme réussis."""
    if params.get("mapping") is not None:
        check_mapping(params["mapping"])
    ctx = Ctx(params, adapter, up, reg, policy)
    results, halted = [], None
    for tid, title, fn in TESTS:
        if only and tid not in only:
            continue
        if not_playable and tid in not_playable:
            res = dict(not_playable[tid], result=NJ, id=tid, title=title, seconds=0.0)
            results.append(res)
            log(f"{tid} {NJ:<12} {res['reason'][:110]}")
            continue
        if halted:
            results.append({"id": tid, "title": title, "result": NP, "reason": "recette arrêtée : " + halted})
            continue
        t0 = time.time()
        try:
            res = fn(ctx)
        except BenchHalt as e:
            halted = str(e)
            res = {"result": NP, "reason": "recette arrêtée (SCR-01.error) : " + halted}
        except Exception as e:   # difficulté du banc : « non joué », jamais « réussi » ni « non jouable » (BR-009)
            res = {"result": NP, "reason": "difficulté du banc : " + reg.redact(f"{type(e).__name__}: {e}")[:300]}
        res.update({"id": tid, "title": title, "seconds": round(time.time() - t0, 1)})
        results.append(res)
        log(f"{tid} {res['result']:<12} {res.get('reason', '')[:110]}")
    return results
