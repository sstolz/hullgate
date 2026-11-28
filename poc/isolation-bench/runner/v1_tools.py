"""Outils de la cible `v1` (F-005, BR-071), bibliothèque standard seulement (BR-005 du banc).

- ES256 : signature ECDSA P-256 avec la clé jetable du banc, pour falsifier des jetons (BR-010, BR-014) ;
- TOTP (RFC 6238) : codes de la barrière calculés à partir du secret jetable du banc, jamais deux fois le même ;
- parcours d'autorisation de la v1 : /authorize → barrière /consent → code → /token, sous /o/<id>/oauth/p/<chemin>.
Aucun jeton, code ni secret n'est imprimé ; l'appelant les inscrit dans la population (secrets_guard).
"""
import base64
import hashlib
import hmac
import json
import re
import secrets
import struct
import time
import urllib.parse

import httpx_lite as h

ACCEPT = "application/json, text/event-stream"
HANDSHAKE = "2025-06-18"   # 2026-07-28 n'a pas `ping` dans le SDK 2.3.0 (learning F-005 lot A)

# ---------------------------------------------------------------- ES256 (P-256)
P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
A = P - 3
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
G = (0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
     0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5)


def _add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    (x1, y1), (x2, y2) = p1, p2
    if x1 == x2 and (y1 + y2) % P == 0:
        return None
    if p1 == p2:
        lam = (3 * x1 * x1 + A) * pow(2 * y1, -1, P) % P
    else:
        lam = (y2 - y1) * pow(x2 - x1, -1, P) % P
    x3 = (lam * lam - x1 - x2) % P
    return x3, (lam * (x1 - x3) - y1) % P


def _mul(k, point):
    out = None
    while k:
        if k & 1:
            out = _add(out, point)
        point = _add(point, point)
        k >>= 1
    return out


def private_scalar(pem_text):
    """Scalaire privé d'une clé EC P-256 en PEM PKCS#8 (forme écrite par `python -m hullgate.keys`)."""
    body = "".join(l for l in pem_text.strip().splitlines() if not l.startswith("-----"))
    der = base64.b64decode(body)
    i = der.find(b"\x02\x01\x01\x04\x20")   # ECPrivateKey : version 1, OCTET STRING de 32 octets
    if i < 0:
        raise ValueError("clé EC P-256 PKCS#8 attendue")
    return int.from_bytes(der[i + 5:i + 37], "big")


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def es256_sign(d, signing_input):
    z = int.from_bytes(hashlib.sha256(signing_input).digest(), "big")
    while True:
        k = secrets.randbelow(N - 1) + 1
        r = _mul(k, G)[0] % N
        s = pow(k, -1, N) * (z + r * d) % N
        if r and s:
            return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def forge(d, base_token, overrides=None, remove=(), header=None, corrupt=False):
    hdr = json.loads(b64d(base_token.split(".")[0]))
    hdr.update(header or {})
    claims = json.loads(b64d(base_token.split(".")[1]))
    claims.update(overrides or {})
    for k in remove:
        claims.pop(k, None)
    si = b64u(json.dumps(hdr, separators=(",", ":")).encode()) + "." + b64u(json.dumps(claims, separators=(",", ":")).encode())
    sig = bytearray(es256_sign(d, si.encode()))
    if corrupt:
        sig[-1] ^= 0x01
    return si + "." + b64u(bytes(sig))


def claims(tok):
    return json.loads(b64d(tok.split(".")[1]))


def header(tok):
    return json.loads(b64d(tok.split(".")[0]))


# ---------------------------------------------------------------- TOTP
def totp(secret, counter):
    dg = hmac.new(secret, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = dg[-1] & 0x0F
    return str((struct.unpack(">I", dg[o:o + 4])[0] & 0x7FFFFFFF) % 1000000).zfill(6)


class Totp:
    """Codes de pas strictement croissants : pas courant ± 1, puis attente du pas suivant (BR-060 : un pas accepté
    ou antérieur est refusé)."""

    def __init__(self, secret_b32):
        clean = "".join(secret_b32.split()).upper().rstrip("=")
        self.secret = base64.b32decode(clean + "=" * (-len(clean) % 8))
        self.last = -1

    def code(self):
        while True:
            now = int(time.time() // 30)
            for c in (now - 1, now, now + 1):
                if c > self.last:
                    self.last = c
                    return totp(self.secret, c)
            time.sleep(30 - time.time() % 30 + 0.2)


# ---------------------------------------------------------------- parcours d'autorisation
# F-014: an access point is `/o/<id>/p/<path>/mcp`, its authorization server `/o/<id>/oauth/p/<path>`.
_ACCESS_POINT = re.compile(r"^/o/([a-z2-7]{12})/p/(.+)/mcp$")


def organisation_id(tree_json):
    """The organisation's public identifier from `tree show / --json` (key `public_id`, F-014 §3.3)."""
    return next(item["public_id"] for item in json.loads(tree_json) if item.get("level") == "org")


def resource_url(base, org_id, path):
    return f"{base}/o/{org_id}/p/{path}/mcp"


def _access_point(resource_url):
    found = _ACCESS_POINT.match(urllib.parse.urlparse(resource_url).path)
    if found is None:
        raise ValueError(f"not an access point URL: {resource_url}")
    return found.group(1), found.group(2)


def path_of(resource_url):
    return _access_point(resource_url)[1]


def org_of(resource_url):
    return _access_point(resource_url)[0]


def issuer(base, resource_url):
    org_id, path = _access_point(resource_url)
    return f"{base}/o/{org_id}/oauth/p/{path}"


def location(r):
    return r.headers.get("Location") or r.headers.get("location") or ""


def query(url):
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)


def authorize(base, resource_url, client_id, redirect, totp_codes, method="S256", challenge=True, scope="mcp offline_access"):
    """(statut de la dernière réponse, code ou None, erreur ou None, vérificateur). Barrière passée avec un code TOTP neuf."""
    ver = secrets.token_urlsafe(48)
    q = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect, "scope": scope,
         "state": "st-" + secrets.token_hex(8), "resource": resource_url}
    if challenge:
        q["code_challenge"] = b64u(hashlib.sha256(ver.encode()).digest()) if method == "S256" else ver
        q["code_challenge_method"] = method
    iss = issuer(base, resource_url)
    r = h.request("GET", iss + "/authorize?" + urllib.parse.urlencode(q))
    loc = location(r)
    if not loc.endswith("/consent"):
        qs = query(loc)
        return r.status, qs.get("code", [None])[0], qs.get("error", [None])[0], ver
    req = consent_ref(r)
    r = h.request("POST", iss + "/consent", consent_cookie(req), form={"req": req, "decision": "allow", "totp": totp_codes.code()})
    qs = query(location(r))
    return r.status, qs.get("code", [None])[0], qs.get("error", [None])[0], ver


def consent_ref(r):
    """Référence de la demande en attente : cookie posé par /authorize (la v1 ne la met jamais dans l'URL)."""
    raw = [v for k, v in r.headers.items() if k.lower() == "set-cookie"]
    for header in raw:
        for part in header.split(","):
            name, _, rest = part.strip().partition("=")
            if name == "hullgate_consent":
                return rest.split(";", 1)[0]
    return ""


def consent_cookie(req):
    """Double soumission (v1) : la barrière exige le champ `req` ET le cookie de la demande."""
    return {"Cookie": "hullgate_consent=" + req}


def token(base, resource_url, form):
    r = h.request("POST", issuer(base, resource_url) + "/token", form=form)
    return r.status, (r.json() or {})


def token_raw(base, resource_url, pairs):
    """Formulaire à paramètres répétés (resource deux fois, absente) : T04."""
    import urllib.request
    import urllib.error
    data = urllib.parse.urlencode(pairs).encode()
    req = urllib.request.Request(issuer(base, resource_url) + "/token", data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=30)
        return h.Resp(r.status, dict(r.headers), r.read())
    except urllib.error.HTTPError as e:
        return h.Resp(e.code, dict(e.headers), e.read())


def revoke(base, resource_url, form):
    return h.request("POST", issuer(base, resource_url) + "/revoke", form=form)


def ping(url, tok):
    """Sonde par la MÉTHODE MCP `ping` (BR-071 : la v1 n'a aucun outil en F-005), version à poignée de main."""
    hdr = {"Accept": ACCEPT, "MCP-Protocol-Version": HANDSHAKE}
    if tok:
        hdr["Authorization"] = "Bearer " + tok
    return h.request("POST", url, hdr, json_body={"jsonrpc": "2.0", "id": 1, "method": "ping"})


def ping_ok(r):
    j = r.json() if r.status == 200 else None
    return isinstance(j, dict) and "result" in j and "error" not in j


JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\-]{20,}")
