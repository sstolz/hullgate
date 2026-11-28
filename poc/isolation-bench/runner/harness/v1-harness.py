"""Harnais de révocation de la revue de la PR #15 (N3 et chemin nominal de /revoke), transposés à la v1 (F-005, BR-071).

Mêmes contrôles que HULL-5-review-2-n3-test.py et HULL-5-review-3-revoke-nominal-test.py, contre l'image v1 construite
par le banc, lancée seule dans un conteneur jetable ; transpositions :
- panne simulée (`/admin/store` des POC) -> fichier de base rendu non inscriptible de l'extérieur du processus
  (`docker exec -u 0 … chmod`, BR-064, BR-068 : aucune route de test dans l'image) ; révocation par l'administration ->
  `hullgate admin token revoke` (CLI, code 1 en panne) ;
- verrou d'écriture tenu de l'extérieur du processus (`BEGIN IMMEDIATE` dans un autre processus du conteneur, sur le
  même fichier) : la v1 est fermée par défaut aussi pour la lecture (prédicat de /readyz, BR-064), le contrôle « les
  lectures passent » des POC devient « le point d'accès répond 503 » ;
- base sur un volume nommé jetable : sous un moteur à identités décalées (Sysbox), un `chmod` fait depuis l'hôte sur un
  montage lié ne prive pas le processus du conteneur de l'écriture ; l'injection se fait donc dans le conteneur.
- sonde par la méthode MCP `ping` ; autorisation par le serveur d'autorisation du point d'accès et la barrière TOTP.
Usage : v1-harness.py n3|nominal <image> <fichier de résultats>. Aucun jeton n'est affiché ; les valeurs émises sont
transmises au banc pour la population de secrets (lignes « SECRET »), jamais écrites dans les résultats.
"""
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import httpx_lite as h  # noqa: E402
import v1_tools as v1  # noqa: E402

PORT = 18300
GW = f"http://127.0.0.1:{PORT}"
CLIENT, REDIRECT = "bench-gw-client", "http://localhost/callback"
PATH = "acme/app/prod/n3"
RES = ""  # the access point of PATH, `/o/<id>/p/…/mcp` (F-014): set by `Instance.start` from the instance's identifier
results = []


def secret(v):
    if v:
        print("SECRET " + v, flush=True)


def check(name, cond, detail):
    results.append({"contrôle": name, "tenu": bool(cond), **detail})
    print(("OK   " if cond else "KO   "), name, json.dumps(detail, ensure_ascii=False), flush=True)


class Instance:
    def __init__(self, image, mode):
        self.image, self.name = image, f"isolation-bench-v1-{mode}"
        self.dir = os.path.join(HERE, f"v1-{mode}-run")
        self.sec = os.path.join(self.dir, "secrets")
        self.volume = self.name + "-data"
        self.totp = None

    def start(self):
        subprocess.run(["docker", "rm", "-f", "-v", self.name], capture_output=True)
        subprocess.run(["docker", "volume", "rm", "-f", self.volume], capture_output=True)
        shutil.rmtree(self.dir, ignore_errors=True)
        os.makedirs(self.sec)
        uid = f"{os.getuid()}:{os.getgid()}"
        for kind, f in (("signing-key", "signing_key"), ("totp-secret", "authorize_totp")):
            r = subprocess.run(["docker", "run", "--rm", "--user", uid, "-v", self.sec + ":/out", "--entrypoint", "sh", self.image,
                                "-c", f"python -m hullgate.keys {kind} /out/{f} >/dev/null && chmod 0444 /out/{f}"],
                               capture_output=True, text=True)
            if r.returncode:
                raise RuntimeError(f"secret jetable {f} : {r.stderr[-200:]}")
        totp_secret = open(os.path.join(self.sec, "authorize_totp")).read().strip()
        secret(totp_secret)
        self.totp = v1.Totp(totp_secret)
        r = subprocess.run(["docker", "run", "-d", "--name", self.name, "--read-only", "-p", f"127.0.0.1:{PORT}:4790",
                            "-v", self.volume + ":/data", "-v", os.path.join(self.sec, "signing_key") + ":/run/secrets/signing_key:ro",
                            "-v", os.path.join(self.sec, "authorize_totp") + ":/run/secrets/authorize_totp:ro",
                            "-e", "PUBLIC_URL=" + GW, "-e", "ADMIN_URL=http://localhost:18301", self.image], capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError("docker run : " + r.stderr[-300:])
        for _ in range(120):
            if h.request("GET", GW + "/readyz").status == 200:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("passerelle v1 non prête")
        for args in (("tree", "add", "/", "client", "Acme"), ("tree", "add", "acme", "project", "App"),
                     ("tree", "add", "acme/app", "environment", "Prod"), ("tree", "add", "acme/app/prod", "role", "n3"),
                     ("client", "add", CLIENT, "--path", "acme", "--redirect-uri", REDIRECT)):
            assert self.cli(*args).returncode == 0, args
        global RES
        RES = v1.resource_url(GW, v1.organisation_id(self.cli("tree", "show", "/", "--json").stdout), PATH)

    def cli(self, *args):
        return subprocess.run(["docker", "exec", self.name, "hullgate", "admin", *args], capture_output=True, text=True)

    def lineage(self):
        out = json.loads(self.cli("token", "list", "--json").stdout or "[]")
        live = [t["id"] for t in out if t["type"] == "oauth" and t["state"] == "active"]
        return live[-1] if live else ""

    def chmod(self, mode):
        r = subprocess.run(["docker", "exec", "-u", "0", self.name, "python", "-c",
                            f"import os; os.chmod('/data/hullgate.db', {mode})"], capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError("chmod : " + r.stderr[-200:])

    def hold_lock(self):
        """Verrou d'écriture tenu par un autre processus du conteneur jusqu'au retrait du fichier témoin."""
        code = ("import os, sqlite3, time\n"
                "c = sqlite3.connect('/data/hullgate.db', isolation_level=None, timeout=5)\n"
                "c.execute('BEGIN IMMEDIATE')\n"
                "open('/data/.lock-held', 'w').close()\n"
                "t = time.time()\n"
                "while os.path.exists('/data/.lock-held') and time.time() - t < 120:\n"
                "    time.sleep(0.1)\n"
                "c.execute('ROLLBACK')\n"
                "open('/data/.lock-released', 'w').close()\n")
        subprocess.run(["docker", "exec", "-d", self.name, "python", "-c", code], check=True)
        for _ in range(100):
            if subprocess.run(["docker", "exec", self.name, "test", "-e", "/data/.lock-held"]).returncode == 0:
                return
            time.sleep(0.1)
        raise RuntimeError("verrou non pris")

    def release_lock(self):
        subprocess.run(["docker", "exec", self.name, "rm", "-f", "/data/.lock-held"])
        for _ in range(100):
            if subprocess.run(["docker", "exec", self.name, "test", "-e", "/data/.lock-released"]).returncode == 0:
                subprocess.run(["docker", "exec", self.name, "rm", "-f", "/data/.lock-released"])
                return
            time.sleep(0.1)
        raise RuntimeError("verrou non relâché")

    def logs(self):
        r = subprocess.run(["docker", "logs", self.name], capture_output=True, text=True)
        return r.stdout + r.stderr

    def stop(self):
        subprocess.run(["docker", "rm", "-f", "-v", self.name], capture_output=True)
        subprocess.run(["docker", "volume", "rm", "-f", self.volume], capture_output=True)
        shutil.rmtree(self.dir, ignore_errors=True)


def grant(inst):
    st, code, err, ver = v1.authorize(GW, RES, CLIENT, REDIRECT, inst.totp)
    secret(code)
    if not code:
        return st, None, None
    st, j = v1.token(GW, RES, {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                               "client_id": CLIENT, "code_verifier": ver, "resource": RES})
    secret(j.get("access_token"))
    secret(j.get("refresh_token"))
    return st, j.get("access_token"), j.get("refresh_token")


def refresh(rt):
    st, j = v1.token(GW, RES, {"grant_type": "refresh_token", "refresh_token": rt, "client_id": CLIENT, "resource": RES})
    secret(j.get("access_token"))
    secret(j.get("refresh_token"))
    return st, j.get("error"), j.get("access_token"), j.get("refresh_token")


def revoke(tok, hint=None, client=CLIENT):
    form = {"client_id": client}
    if tok is not None:
        form["token"] = tok
    if hint:
        form["token_type_hint"] = hint
    r = v1.revoke(GW, RES, form)
    return r.status, (r.json() or {}).get("error") if r.body else None


def ping(tok):
    r = v1.ping(RES, tok)
    return r.status, v1.ping_ok(r)


def n3(inst):
    st, a1, r1 = grant(inst)
    check("nominal_grant_ping", st == 200 and a1 and r1 and ping(a1) == (200, True), {"token_http": st, "ping": ping(a1)[0]})
    lin = inst.lineage()
    # --- S1 : coffre en panne, fichier de base non inscriptible (injecté hors du processus)
    inst.chmod(0o444)
    try:
        st, err = revoke(r1, "refresh_token")
        check("S1_revoke_refresh_coffre_en_panne_503", st == 503, {"http": st, "erreur": err})
        st, err = revoke(a1, "access_token")
        check("S1_revoke_access_coffre_en_panne_503", st == 503, {"http": st, "erreur": err})
        c = inst.cli("token", "revoke", lin)
        check("S1_cli_revoke_coffre_en_panne_code_1", c.returncode == 1, {"code": c.returncode})
        st, err, at, rt = refresh(r1)
        check("S1_refresh_coffre_en_panne_aucun_jeton", st >= 400 and not at and not rt, {"http": st, "erreur": err})
        st, _ = ping(a1)
        check("S1_point_d_acces_coffre_en_panne_503", st == 503, {"http": st})
    finally:
        inst.chmod(0o644)
    st, ok = ping(a1)
    check("S1_apres_retour_jeton_intact_non_revoque_par_un_503", st == 200 and ok, {"http": st})
    st, err, a2, r2 = refresh(r1)
    check("S1_apres_retour_refresh_non_consomme", st == 200 and a2 and r2, {"http": st, "erreur": err})
    check("rotation_ancien_jeton_401", ping(a1)[0] == 401 and ping(a2) == (200, True), {"ancien": ping(a1)[0], "nouveau": ping(a2)[0]})
    # --- S2 : verrou d'écriture tenu de l'extérieur (BEGIN IMMEDIATE)
    inst.hold_lock()
    try:
        st, _ = ping(a2)
        check("S2_point_d_acces_verrou_tenu_503_ferme_par_defaut", st == 503, {"http": st})
        t0 = time.time(); st, err = revoke(r2, "refresh_token"); dt = round(time.time() - t0, 1)
        check("S2_revoke_refresh_ecriture_en_echec_503", st == 503, {"http": st, "erreur": err, "delai_s": dt})
        t0 = time.time(); st, err = revoke(a2, "access_token"); dt = round(time.time() - t0, 1)
        check("S2_revoke_access_ecriture_en_echec_503", st == 503, {"http": st, "erreur": err, "delai_s": dt})
        c = inst.cli("token", "revoke", lin)
        check("S2_cli_revoke_ecriture_en_echec_code_1", c.returncode == 1, {"code": c.returncode})
        t0 = time.time(); st, err, at, rt = refresh(r2); dt = round(time.time() - t0, 1)
        check("S2_refresh_ecriture_en_echec_aucun_jeton", st >= 400 and not at and not rt, {"http": st, "erreur": err, "delai_s": dt})
    finally:
        inst.release_lock()
    st, _ = ping(a2)
    results.append({"info": "S2_apres_verrou_ancien_jeton_d_acces", "http": st})
    st, err, a3, r3 = refresh(r2)
    results.append({"info": "S2_apres_verrou_refresh_r2", "http": st, "erreur": err, "jeton_delivre": bool(a3)})
    if not a3:
        st, a3, r3 = grant(inst)
    st, err = revoke(a3, "access_token")
    check("nominal_revoke_200_puis_401", st == 200 and ping(a3)[0] == 401, {"revoke_http": st, "ping": ping(a3)[0]})


def nominal(inst):
    st, a1, r1 = grant(inst)
    check("grant_ping", st == 200 and a1 and r1 and ping(a1) == (200, True), {"token_http": st})
    st, err = revoke(None)
    check("sans_token_400_invalid_request", st == 400 and err == "invalid_request", {"http": st, "erreur": err})
    st, err = revoke(a1, "access_token", client="client-inconnu-" + secrets.token_hex(4))
    check("client_inconnu_invalid_client", st in (400, 401) and err == "invalid_client", {"http": st, "erreur": err})
    check("jeton_toujours_valide_apres_invalid_client", ping(a1) == (200, True), {"ping": ping(a1)[0]})
    st, err = revoke(secrets.token_urlsafe(48), "access_token")
    check("jeton_inconnu_200", st == 200, {"http": st, "erreur": err})
    st, err = revoke("pas.un.jwt")
    check("jeton_malforme_200", st == 200, {"http": st, "erreur": err})
    st, err = revoke(a1, "access_token")
    check("revoke_access_200", st == 200, {"http": st, "erreur": err})
    check("access_revoque_401", ping(a1)[0] == 401, {"ping": ping(a1)[0]})
    st2, err2, at, rt = refresh(r1)
    results.append({"info": "refresh_apres_revoke_access_meme_lignee", "http": st2, "erreur": err2, "jeton_delivre": bool(at)})
    st, a2, r2 = grant(inst)
    st, err = revoke(r2, "refresh_token")
    check("revoke_refresh_200", st == 200, {"http": st, "erreur": err})
    st2, err2, at, rt = refresh(r2)
    check("refresh_revoque_refuse", st2 >= 400 and not at and not rt, {"http": st2, "erreur": err2})
    check("access_lie_au_refresh_revoque_401", ping(a2)[0] == 401, {"ping": ping(a2)[0]})
    st, err = revoke(a2, "access_token")
    check("revoke_deja_revoque_200", st == 200, {"http": st, "erreur": err})
    st, a3, r3 = grant(inst)
    st, err = revoke(a3, "refresh_token")
    check("hint_faux_revoque_quand_meme", st == 200 and ping(a3)[0] == 401, {"http": st, "ping": ping(a3)[0]})


def main():
    mode, image, out = sys.argv[1], sys.argv[2], sys.argv[3]
    inst = Instance(image, mode)
    code = 0
    try:
        inst.start()
        (n3 if mode == "n3" else nominal)(inst)
        leak = v1.JWT_RE.findall(inst.logs())
        check("journal_sans_jeton", not leak, {"lignes": len(inst.logs().splitlines()), "jwt_trouves": len(leak)})
    except Exception as e:   # difficulté du harnais : consignée, jamais « tenu »
        check("harnais_sans_erreur", False, {"erreur": f"{type(e).__name__}: {str(e)[:200]}"})
        code = 2
    finally:
        inst.stop()
    json.dump(results, open(out, "w"), ensure_ascii=False, indent=1)
    ok = all(r.get("tenu", True) for r in results)
    print(f"RÉSULTAT {mode} v1 :", "tous tenus" if ok else "au moins un échec", f"({sum(1 for r in results if 'tenu' in r)} contrôles)")
    return code or (0 if ok else 1)


if __name__ == "__main__":
    sys.exit(main())
