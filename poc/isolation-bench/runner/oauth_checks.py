"""Contrôle de l'OAuth entrant de la passerelle POC (F-002, ligne H-2 du tableau go / no-go), hors des 16 tests.

Joue, contre le serveur d'autorisation de la passerelle, ce que la recette ne couvre pas : PKCE S256 seul, code à usage
unique, ressource liée au code, rotation du jeton de renouvellement avec détection de réemploi, révocation (RFC 7009)
effective sur le jeton d'accès JWT, outil `ping`. Aucun jeton n'est écrit dans les preuves (seulement statuts et codes d'erreur).

F-005 (cible `v1`, BR-071) : un adaptateur peut fournir `endpoint(ressource, nom)` (serveur d'autorisation par point
d'accès), `as_metadata_url(ressource)`, `authorize_code(…)` (barrière opérateur) et `probe`/`probe_ok` (sonde par la
méthode MCP `ping`, la v1 n'ayant aucun outil). Sans ces crochets, le comportement des cibles des POC est inchangé.
"""
import base64
import hashlib
import secrets
import urllib.parse

import httpx_lite as h
from adapter_gateway import CLIENT, REDIRECT, _post_raw


def _b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _ep(adapter, resource, name):
    return adapter.endpoint(resource, name) if hasattr(adapter, "endpoint") else adapter.gw + "/" + name


def _code(gw, resource, method="S256", challenge=True, adapter=None):
    if adapter is not None and hasattr(adapter, "authorize_code"):
        return adapter.authorize_code(resource, method=method, challenge=challenge)
    ver = secrets.token_urlsafe(48)
    q = {"response_type": "code", "client_id": CLIENT, "redirect_uri": REDIRECT, "scope": "mcp offline_access",
         "state": "st-" + secrets.token_hex(8), "resource": resource}
    if challenge:
        q["code_challenge"] = _b64u(hashlib.sha256(ver.encode()).digest()) if method == "S256" else ver
        q["code_challenge_method"] = method
    r = h.request("GET", gw + "/authorize?" + urllib.parse.urlencode(q))
    loc = r.headers.get("Location") or r.headers.get("location") or ""
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
    return r.status, qs.get("code", [None])[0], qs.get("error", [None])[0], ver


def _token(gw, form, reg, adapter=None):
    url = _ep(adapter, form.get("resource", ""), "token") if adapter is not None and hasattr(adapter, "endpoint") else gw + "/token"
    r = _post_raw(url, urllib.parse.urlencode(form))
    j = r.json() or {}
    for k in ("access_token", "refresh_token"):
        if j.get(k):
            reg.add(j[k])
    return r.status, j


_ADAPTER = []


def _call(url, tok, method="tools/call", params=None):
    if _ADAPTER and hasattr(_ADAPTER[0], "probe"):
        return _ADAPTER[0].probe(url, tok)
    r = h.request("POST", url, {"Authorization": "Bearer " + tok, "Accept": "application/json, text/event-stream"},
                  json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    return r


def _pong(adapter, r):
    return adapter.probe_ok(r) if hasattr(adapter, "probe_ok") else "pong" in r.text


def run(adapter, reg):
    _ADAPTER[:] = [adapter]
    gw = adapter.gw
    A = adapter.params["profiles"]["A"]["resource_url"]
    B = adapter.params["profiles"]["B"]["resource_url"]
    obs, ok = {}, True

    def check(name, cond, detail):
        nonlocal ok
        obs[name] = {"tenu": bool(cond), **detail}
        ok &= bool(cond)

    md_url = adapter.as_metadata_url(A) if hasattr(adapter, "as_metadata_url") else gw + "/.well-known/oauth-authorization-server"
    md = h.request("GET", md_url).json()
    check("métadonnées_serveur_PKCE_S256_seul", md.get("code_challenge_methods_supported") == ["S256"],
          {"code_challenge_methods_supported": md.get("code_challenge_methods_supported")})
    st, code, err, _ = _code(gw, A, method="plain", adapter=adapter)
    check("PKCE_plain_refusé", code is None, {"http": st, "erreur": err})
    st, code, err, _ = _code(gw, A, challenge=False, adapter=adapter)
    check("PKCE_absent_refusé", code is None, {"http": st, "erreur": err})

    st, code, err, ver = _code(gw, A, adapter=adapter)
    reg.add(code or "")
    s1, j1 = _token(gw, {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "client_id": CLIENT,
                         "code_verifier": "x" * 64, "resource": A}, reg, adapter)
    check("vérificateur_PKCE_faux_refusé", s1 >= 400 and "access_token" not in j1, {"http": s1, "erreur": j1.get("error")})

    st, code, err, ver = _code(gw, A, adapter=adapter)
    reg.add(code or "")
    s1, j1 = _token(gw, {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "client_id": CLIENT,
                         "code_verifier": ver, "resource": B}, reg, adapter)
    check("code_lié_à_A_échangé_pour_B_refusé", s1 >= 400 and "access_token" not in j1, {"http": s1, "erreur": j1.get("error")})
    s2, j2 = _token(gw, {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "client_id": CLIENT,
                         "code_verifier": ver, "resource": A}, reg, adapter)
    # informatif : le code refusé pour sa ressource est brûlé (le gestionnaire PKCE de fosite supprime la session PKCE dès la
    # validation, avant le contrôle de ressource) ; plus strict que nécessaire, le client recommence l'autorisation
    obs["informatif_code_brûlé_après_refus_de_ressource"] = {"tenu": s2 >= 400, "http": s2, "erreur": j2.get("error")}
    st, code, err, ver = _code(gw, A, adapter=adapter)
    reg.add(code or "")
    s2, j2 = _token(gw, {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "client_id": CLIENT,
                         "code_verifier": ver, "resource": A}, reg, adapter)
    check("échange_nominal_code_PKCE_ressource", s2 == 200 and "access_token" in j2 and "refresh_token" in j2, {"http": s2})
    s3, j3 = _token(gw, {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "client_id": CLIENT,
                         "code_verifier": ver, "resource": A}, reg, adapter)
    check("code_à_usage_unique", s3 >= 400 and "access_token" not in j3, {"http": s3, "erreur": j3.get("error")})

    r = _call(A, j2.get("access_token", ""), params={"name": "ping", "arguments": {}})
    check("réemploi_du_code_révoque_les_jetons_émis", r.status == 401, {"http": r.status})   # RFC 6749 §4.1.2

    g = adapter.authorize_grant(A)
    at0, rt0 = g["access_token"], g["refresh_token"]
    r = _call(A, at0, params={"name": "ping", "arguments": {}})
    check("outil_ping", r.status == 200 and _pong(adapter, r), {"http": r.status})

    sb, jb = _token(gw, {"grant_type": "refresh_token", "refresh_token": rt0, "client_id": CLIENT, "resource": B}, reg, adapter)
    check("renouvellement_vers_B_refusé_sans_consommer_le_jeton", sb >= 400 and "access_token" not in jb, {"http": sb, "erreur": jb.get("error")})
    s4, j4 = _token(gw, {"grant_type": "refresh_token", "refresh_token": rt0, "client_id": CLIENT, "resource": A}, reg, adapter)
    rt1, at1 = j4.get("refresh_token", ""), j4.get("access_token", "")
    check("rotation_du_jeton_de_renouvellement", s4 == 200 and rt1 and rt1 != rt0, {"http": s4})
    if at1:
        c0, c1 = adapter.claims(at0), adapter.claims(at1)
        check("jeton_renouvelé_jti_et_iat_neufs_audience_unique", c1["jti"] != c0["jti"] and c1["iat"] >= c0["iat"] and c1["aud"] in ([A], A)
              and c1["exp"] - c1["iat"] == c0["exp"] - c0["iat"], {"durée_s": c1["exp"] - c1["iat"], "aud": c1["aud"]})
    r = _call(A, at0, params={"name": "ping", "arguments": {}})
    check("ancien_jeton_d_accès_invalidé_par_la_rotation", r.status == 401, {"http": r.status})
    s5, j5 = _token(gw, {"grant_type": "refresh_token", "refresh_token": rt0, "client_id": CLIENT, "resource": A}, reg, adapter)
    check("réemploi_de_l_ancien_renouvellement_refusé", s5 >= 400 and "access_token" not in j5, {"http": s5, "erreur": j5.get("error")})
    r = _call(A, at1, params={"name": "ping", "arguments": {}})
    # informatif (hors critère H-2, qui exige rotation et révocation) : OAuth 2.1 §4.3.1 recommande de révoquer la lignée au réemploi
    obs["informatif_réemploi_révoque_la_lignée"] = {"tenu": r.status == 401, "http": r.status}

    # révocation RFC 7009 d'un jeton de renouvellement : le jeton d'accès de la même autorisation tombe aussi
    j = adapter.authorize_grant(A)
    r = _call(A, j["access_token"], params={"name": "ping", "arguments": {}})
    before = r.status
    rv = _post_raw(_ep(adapter, A, "revoke"), urllib.parse.urlencode({"token": j["refresh_token"], "token_type_hint": "refresh_token", "client_id": CLIENT}))
    r = _call(A, j["access_token"], params={"name": "ping", "arguments": {}})
    s6, j6 = _token(gw, {"grant_type": "refresh_token", "refresh_token": j["refresh_token"], "client_id": CLIENT, "resource": A}, reg, adapter)
    check("révocation_RFC7009", before == 200 and rv.status == 200 and r.status == 401 and s6 >= 400,
          {"appel_avant": before, "revoke_http": rv.status, "appel_après": r.status, "renouvellement_après": s6})
    # révocation RFC 7009 d'un jeton d'accès JWT
    j = adapter.authorize_grant(A)
    rv = _post_raw(_ep(adapter, A, "revoke"), urllib.parse.urlencode({"token": j["access_token"], "token_type_hint": "access_token", "client_id": CLIENT}))
    r = _call(A, j["access_token"], params={"name": "ping", "arguments": {}})
    check("révocation_RFC7009_jeton_d_accès", rv.status == 200 and r.status == 401, {"revoke_http": rv.status, "appel_après": r.status})

    # le serveur d'autorisation refuse aussi : renouvellement d'un rôle supprimé, renouvellement pendant une panne du coffre
    P = adapter.clone_profile("A")
    rt = adapter.grants[P["token_ref"]]
    adapter.delete_profile(P["id"])
    s7, j7 = _token(gw, {"grant_type": "refresh_token", "refresh_token": rt, "client_id": CLIENT, "resource": P["resource_url"]}, reg, adapter)
    check("renouvellement_refusé_après_suppression_du_rôle", s7 >= 400 and "access_token" not in j7, {"http": s7, "erreur": j7.get("error")})
    g = adapter.authorize_grant(A)
    adapter.store_down()
    try:
        s8, j8 = _token(gw, {"grant_type": "refresh_token", "refresh_token": g["refresh_token"], "client_id": CLIENT, "resource": A}, reg, adapter)
    finally:
        adapter.store_up()
    check("renouvellement_refusé_coffre_en_panne", s8 >= 400 and "access_token" not in j8, {"http": s8, "erreur": j8.get("error")})

    return {"id": "H-2", "title": "OAuth entrant : PKCE S256, code unique, ressource, rotation, révocation, ping",
            "result": "réussi" if ok else "échoué", "observed": [obs],
            "reason": "" if ok else "au moins une propriété OAuth entrante non tenue (voir observed)"}
