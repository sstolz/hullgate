"""Exigences socle v1 relevées sur le POC Go (HULL-7), vérifiées par requête contre le POC Python (F-003, BR-026, ligne H-9),
et sous-contrôle `dcr` (point de comparaison de BR-024). Hors des 16 tests, hors des lignes d'isolation (H9 de la spec).

- N1 : le jeton d'accès porte `client_id` (RFC 9068 §2.2) ;
- N2 : un jeton sans le scope `mcp` est refusé en 403 `insufficient_scope` (RFC 6750 §3.1) ; jeton forgé avec la clé de test,
  `mcp` seul retiré de la claim `scope` (une propriété, discipline de BR-014) ; contrôle positif : jeton de référence re-signé accepté ;
- N4 : sur un rôle dédié (`clone_profile`), jetons émis, rôle supprimé puis recréé sous le même identifiant : 401 au point
  d'accès et `invalid_grant` au renouvellement ; contrôle positif : une autorisation neuve sur le rôle recréé fonctionne ;
- N13 : lue sur l'assertion `sans_token_400_invalid_request` du harnais nominal de `/revoke` (BR-025), par run_bench ;
- dcr : sur une instance jetable de l'image du banc où l'enregistrement est activé (DCR_ENABLED=1, secret d'autorisation
  jetable), un client s'enregistre (RFC 7591) puis bute sur la barrière d'autorisation ; sur l'instance de la recette,
  l'enregistrement est absent (désactivé).
Aucun jeton n'est écrit dans les preuves (statuts, codes d'erreur, noms de claims).

F-005 (cible `v1`, BR-071) : sonde par la méthode MCP `ping` (`adapter.probe`), routes OAuth du point d'accès
(`adapter.endpoint`), rôle recréé par la CLI (`adapter.recreate_profile`), `dcr` joué sur l'instance de la recette
(`adapter.dcr_check`) et contrôle `sensible_*` de BR-051 (`adapter.sensible_check`) ; cibles des POC inchangées.
"""
import base64
import hashlib
import secrets
import subprocess
import time
import urllib.parse

import httpx_lite as h
from adapter_gateway import CLIENT, _post_raw

ACCEPT = "application/json, text/event-stream"
DCR_PORT, DCR_ADMIN = 18310, 18311


_ADAPTER = []


def _ping(url, tok):
    if _ADAPTER and hasattr(_ADAPTER[0], "probe"):
        return _ADAPTER[0].probe(url, tok)
    return h.request("POST", url, {"Authorization": "Bearer " + tok, "Accept": ACCEPT},
                     json_body={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "ping", "arguments": {}}})


def _www(r):
    return next((v for k, v in r.headers.items() if k.lower() == "www-authenticate"), "")


def _pong(adapter, r):
    return adapter.probe_ok(r) if hasattr(adapter, "probe_ok") else "pong" in r.text


def run(adapter, reg, image):
    _ADAPTER[:] = [adapter]
    A = adapter.params["profiles"]["A"]
    obs, ok = {}, True

    def check(name, cond, detail):
        nonlocal ok
        obs[name] = {"tenu": bool(cond), **detail}
        ok &= bool(cond)

    # N1
    cl = adapter.claims(A["token"])
    check("N1_claim_client_id", cl.get("client_id") == CLIENT, {"claims": sorted(cl), "client_id_égal_au_client": cl.get("client_id") == CLIENT})

    # N2
    scope = str(cl.get("scope", ""))
    forged = adapter.forge(A["token"], {"scope": " ".join(s for s in scope.split() if s != "mcp")})
    diff = sorted(k for k in set(cl) | set(adapter.claims(forged)) if cl.get(k) != adapter.claims(forged).get(k))
    r = _ping(A["resource_url"], forged)
    check("N2_scope_mcp_absent_403_insufficient_scope", r.status == 403 and 'error="insufficient_scope"' in _www(r) and diff == ["scope"],
          {"http": r.status, "www-authenticate": reg.redact(_www(r))[:200], "propriétés_changées": diff, "scope_de_référence": scope})
    r = _ping(A["resource_url"], adapter.forge(A["token"]))
    check("N2_contrôle_positif_jeton_de_référence_re_signé", r.status == 200 and _pong(adapter, r), {"http": r.status})

    # N4
    P = adapter.clone_profile("A")
    rt = adapter.grants[P["token_ref"]]
    before = _ping(P["resource_url"], P["token"]).status
    adapter.delete_profile(P["id"])
    if hasattr(adapter, "recreate_profile"):
        adapter.recreate_profile(P["id"])                           # recréé au même chemin, par la CLI
    else:
        adapter._adm("POST", "/admin/roles", {"id": P["id"]})      # recréé sous le même identifiant
    r = _ping(P["resource_url"], P["token"])
    token_url = adapter.endpoint(P["resource_url"], "token") if hasattr(adapter, "endpoint") else adapter.gw + "/token"
    rr = _post_raw(token_url, urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": rt, "client_id": CLIENT,
                                                                   "resource": P["resource_url"]}))
    j = rr.json() or {}
    fresh = adapter.authorize_grant(P["resource_url"])
    pos = _ping(P["resource_url"], fresh["access_token"]).status
    check("N4_rôle_recréé_jeton_d_accès_401", before == 200 and r.status == 401, {"avant_suppression": before, "après_recréation": r.status})
    check("N4_rôle_recréé_renouvellement_invalid_grant", rr.status == 400 and j.get("error") == "invalid_grant" and "access_token" not in j,
          {"http": rr.status, "erreur": j.get("error")})
    check("N4_contrôle_positif_autorisation_neuve_sur_le_rôle_recréé", pos == 200, {"http": pos})

    if hasattr(adapter, "sensible_check"):   # BR-051 (F-005) : critère du ticket, contre la v1
        for name, res in adapter.sensible_check().items():
            check(name, res.pop("tenu"), res)
    if hasattr(adapter, "dcr_check"):        # v1 : désactivé pendant la recette, activé par la CLI, barrière TOTP
        obs["dcr"] = adapter.dcr_check()
        ok &= obs["dcr"]["tenu"]
        return {"id": "BR-026", "title": "Exigences socle N1, N2, N4, sensible (BR-051), N13 (harnais nominal) et sous-contrôle dcr",
                "result": "réussi" if ok else "échoué", "observed": [obs],
                "reason": "" if ok else "au moins une exigence socle ou le sous-contrôle dcr non tenu (voir observed)"}
    # DCR désactivé pendant la recette
    r = h.request("POST", adapter.gw + "/register", json_body={"redirect_uris": ["http://localhost/cb"], "token_endpoint_auth_method": "none"})
    md = h.request("GET", adapter.gw + "/.well-known/oauth-authorization-server").json() or {}
    check("dcr_désactivé_sur_l_instance_de_la_recette", r.status in (404, 405) and "registration_endpoint" not in md,
          {"http_register": r.status, "registration_endpoint_annoncé": "registration_endpoint" in md})
    obs["dcr"] = dcr(image, reg)
    ok &= obs["dcr"]["tenu"]
    return {"id": "BR-026", "title": "Exigences socle N1, N2, N4 (N13 : harnais nominal) et sous-contrôle dcr", "result": "réussi" if ok else "échoué",
            "observed": [obs], "reason": "" if ok else "au moins une exigence socle ou le sous-contrôle dcr non tenu (voir observed)"}


def dcr(image, reg):
    """Instance jetable, DCR activé ; secrets jetables détruits avec le conteneur."""
    name, admin, secret = "isolation-bench-py-dcr", secrets.token_hex(24), secrets.token_hex(16)
    reg.add(admin)
    reg.add(secret)
    gw, adm = f"http://127.0.0.1:{DCR_PORT}", f"http://127.0.0.1:{DCR_ADMIN}"
    out = {}
    subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True)
    r = subprocess.run(["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{DCR_PORT}:8080", "-p", f"127.0.0.1:{DCR_ADMIN}:8081",
                        "-e", "PUBLIC_BASE=" + gw, "-e", "ADMIN_TOKEN=" + admin, "-e", "AUTHORIZE_SECRET=" + secret, "-e", "DCR_ENABLED=1",
                        "-e", "ACCESS_TTL_SECONDS=300", image], capture_output=True, text=True)
    if r.returncode:
        return {"tenu": False, "raison": "docker run : " + reg.redact(r.stderr)[-200:]}
    try:
        for _ in range(60):
            if h.request("GET", gw + "/healthz").status == 200:
                break
            time.sleep(0.5)
        h.request("POST", adm + "/admin/roles", {"X-Admin-Token": admin}, json_body={"id": "dcr"})
        res = gw + "/p/acme/app/prod/dcr/mcp"
        md = h.request("GET", gw + "/.well-known/oauth-authorization-server").json() or {}
        out["registration_endpoint_annoncé"] = md.get("registration_endpoint", "").replace(gw, "<instance>")
        reg_r = h.request("POST", gw + "/register", json_body={"redirect_uris": ["http://localhost/cb"], "token_endpoint_auth_method": "none",
                                                               "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                                                               "scope": "mcp", "client_name": "banc-dcr"})
        j = reg_r.json() or {}
        cid = j.get("client_id")
        out["enregistrement"] = {"http": reg_r.status, "client_id_attribué": bool(cid), "secret_attribué": bool(j.get("client_secret"))}
        if j.get("client_secret"):
            reg.add(j["client_secret"])
        ver = secrets.token_urlsafe(48)
        chal = base64.urlsafe_b64encode(hashlib.sha256(ver.encode()).digest()).rstrip(b"=").decode()
        q = urllib.parse.urlencode({"response_type": "code", "client_id": cid or "", "redirect_uri": "http://localhost/cb", "scope": "mcp",
                                    "state": "st", "code_challenge": chal, "code_challenge_method": "S256", "resource": res})
        a = h.request("GET", gw + "/authorize?" + q)
        loc = a.headers.get("Location") or a.headers.get("location") or ""
        out["autorisation"] = {"http": a.status, "code_délivré": "code=" in loc, "renvoi_vers_la_barrière": "/consent?" in loc}
        bad = h.request("POST", gw + "/consent", form={"req": urllib.parse.parse_qs(urllib.parse.urlparse(loc).query).get("req", [""])[0],
                                                       "gw_secret": "faux-secret"})
        bl = bad.headers.get("Location") or bad.headers.get("location") or ""
        out["secret_faux"] = {"http": bad.status, "code_délivré": "code=" in bl}
        rid = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query).get("req", [""])[0]
        good = h.request("POST", gw + "/consent", form={"req": rid, "gw_secret": secret})
        gl = good.headers.get("Location") or good.headers.get("location") or ""
        out["contrôle_positif_secret_juste"] = {"http": good.status, "code_délivré": "code=" in gl, "vers_la_redirection_enregistrée": gl.startswith("http://localhost/cb?")}
        out["tenu"] = (reg_r.status == 201 and bool(cid) and a.status == 302 and "code=" not in loc and "/consent?" in loc
                       and "code=" not in bl and bool(out["registration_endpoint_annoncé"]) and good.status == 302 and "code=" in gl)
        return out
    finally:
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True)
