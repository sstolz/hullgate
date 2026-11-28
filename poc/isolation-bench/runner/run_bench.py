#!/usr/bin/env python3
"""Commande unique du banc (BR-005) : construit le banc depuis zéro, joue la recette, détruit le banc (BR-010).

  ./bench.sh                         # cible ContextForge (F-001) : secrets, conteneurs, provisionnement, 16 tests, teardown
  ./bench.sh --target gateway        # cible POC Go (F-002) : même recette, lue au rôle (BR-012)
  ./bench.sh --target v1             # cible v1 (F-005, BR-071) : image du Dockerfile racine, adaptateur v1, contrôles, socle, harnais v1
  ./bench.sh --keep                  # ne détruit pas le banc à la fin (mise au point)
  ./bench.sh --dev                   # tolère une configuration non committée (résultats non probants, BR-011)
  ./bench.sh --only T01,T02          # sous-ensemble (mise au point, résultats non probants)
  ./bench.sh --purge-images          # supprime aussi les images tirées OU construites par ce lancement (par identifiant, par différence : BR-021)
  ./bench.sh --teardown              # détruit seulement

Prérequis : docker (avec compose), python3 >= 3.9, accès réseau aux registres. Aucune dépendance Python tierce.
Disque (BR-021) : refus de démarrer sous 10 Go libres ; échec si le lancement ajoute plus de 4 Go ; jamais `--rmi all`.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BENCH = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import gen_secrets  # noqa: E402
import httpx_lite as h  # noqa: E402
import recette  # noqa: E402
import secrets_guard  # noqa: E402

CF_PORT, UP_PORT, UP_CTL = 14444, 19100, 19101
GW_PORT, GW_ADMIN, FRONT_PORT, FRONT_CTL = 18100, 18102, 19200, 19201
FRONT_RO_PORT, FRONT_RO_CTL = 19202, 19203   # cible v1 (F-008) : frontale du serveur MCP Gitea en lecture seule
MIN_FREE_GB, MAX_GROWTH_GB = 10, 4
RUN = os.path.join(BENCH, ".run")
BASELINE = os.path.join(RUN, "baseline.json")
PROJECT_LABEL = "label=com.docker.compose.project=isolation-bench"
UNITS = {"B": 1, "kB": 1e3, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}
GW_TARGETS = ("gateway",)     # Go POC (F-002); the Python POC target (F-003) was removed (HULL-58)
SOURCE = {"gateway": "../go-gateway", "v1": "../../src"}
REPORT = {"contextforge": None, "gateway": "docs/trials/poc-go.md", "v1": None}
RESULTS = {"contextforge": "results.json", "gateway": "results-gateway.json", "v1": "results-v1.json"}
V1_IMAGE = "isolation-bench-v1:dev"
COMPOSE = {"gateway": "gateway.compose.yaml", "v1": "v1.compose.yaml"}
CONFIG_KEY = {"gateway": "gateway_config_files", "v1": "v1_config_files"}


def v1_secrets(bench):
    """BR-010 (F-005, F-008) : clé de signature ES256, secret TOTP et clé de connexions jetables, générés par l'image
    construite (son module `hullgate.keys`), dans .run/ ; montés en secrets dans la passerelle, lus par l'adaptateur ;
    autorité de certification jetable et certificat de l'amont du banc (`runner/tls_v1.py`, dans l'image : sa
    bibliothèque `cryptography`) dans .run/tls ; tout est détruit au teardown."""
    r = bench.compose("build", "gateway", check=False)
    if r.returncode:
        raise RuntimeError("construction de l'image v1 : " + r.stderr.strip()[-600:])
    for kind, name in (("signing-key", "signing_key"), ("totp-secret", "authorize_totp"), ("connections-key", "connections_key")):
        r = subprocess.run(["docker", "run", "--rm", "--user", "0", "-v", RUN + ":/out", "--entrypoint", "sh", V1_IMAGE, "-c",
                            f"python -m hullgate.keys {kind} /out/{name} >/dev/null && chmod 0444 /out/{name}"],
                           capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"secret jetable {name} : " + r.stderr.strip()[-300:])
    r = subprocess.run(["docker", "run", "--rm", "--user", "0", "-v", RUN + ":/out", "-v", HERE + ":/runner:ro",
                        "--entrypoint", "python", V1_IMAGE, "/runner/tls_v1.py", "/out/tls"], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError("autorité de certification jetable : " + r.stderr.strip()[-300:])


def run_v1_harnesses(reg):
    """Harnais N3 et nominal de la revue de la PR HULL-15, transposés à la v1 (runner/harness/v1-harness.py) : mêmes contrôles,
    pannes injectées de l'extérieur, barrière TOTP ; identifiants de contrôle repris des POC (AC-08 de F-005)."""
    hd, out = os.path.join(HERE, "harness"), []
    for mode, ident, dest, title in (("n3", "H-10/HULL-5-review-2-n3", "v1-n3-results.json", "Révocation et émission sur panne du coffre (N3)"),
                                     ("nominal", "H-10/HULL-5-review-3-revoke-nominal", "v1-revoke-nominal-results.json", "Chemin nominal de /revoke")):
        src = os.path.join(hd, f"v1-{mode}-results.json")
        r = subprocess.run([sys.executable, os.path.join(hd, "v1-harness.py"), mode, V1_IMAGE, src], cwd=hd, capture_output=True, text=True, timeout=900)
        rows = json.load(open(src)) if os.path.exists(src) else []
        for line in r.stdout.splitlines():
            if line.startswith("SECRET "):
                reg.add(line[7:])
        if os.path.exists(src):
            open(os.path.join(BENCH, "evidence", dest), "w").write(reg.redact(json.dumps(rows, ensure_ascii=False, indent=1)) + "\n")
            os.unlink(src)
        checks = [x for x in rows if "contrôle" in x]
        failed = [x["contrôle"] for x in checks if not x["tenu"]]
        tail = [l for l in r.stdout.strip().splitlines() if not l.startswith("SECRET ")]
        out.append({"id": ident, "title": title + " — v1 (runner/harness/v1-harness.py " + mode + ")",
                    "result": recette.OK if checks and not failed and r.returncode == 0 else recette.KO,
                    "observed": [{"contrôles": len(checks), "tenus": len(checks) - len(failed), "non_tenus": failed, "preuve": "evidence/" + dest,
                                  "code_retour": r.returncode, "fin_de_sortie": reg.redact(tail[-1] if tail else r.stderr[-200:])}]})
    return out


def sh(*a, check=True, env=None, cap=True):
    return subprocess.run(list(a), cwd=BENCH, check=check, capture_output=cap, text=True, env=env)


class Bench:
    def __init__(self, target):
        self.target = target
        self.file = COMPOSE.get(target, "compose.yaml")

    def compose(self, *a, check=True):
        env = dict(os.environ)
        env.pop("COMPOSE_ENV_FILES", None)
        if os.path.exists(os.path.join(RUN, "env")):   # absent (lancement interrompu, autre worktree) : compose sans fichier d'env
            env["COMPOSE_ENV_FILES"] = os.path.join(RUN, "env")
        return subprocess.run(["docker", "compose", "-f", self.file, *a], cwd=BENCH, check=check, capture_output=True, text=True, env=env)


def sha(path):
    return hashlib.sha256(open(os.path.join(BENCH, path), "rb").read()).hexdigest()


def git(*a, cwd=None):
    return subprocess.run(["git", *a], cwd=cwd or BENCH, capture_output=True, text=True)


def git_state(files):
    if git("rev-parse", "--show-toplevel").returncode:
        return {"git": False}
    dirty = git("status", "--porcelain", "--", *files).stdout.strip()
    head = git("rev-parse", "HEAD").stdout.strip()
    firsts = {f: git("log", "--diff-filter=A", "--format=%H %cI", "--", f).stdout.strip().split("\n")[-1] for f in files}
    return {"git": True, "head": head, "config_dirty": bool(dirty), "dirty_files": dirty, "first_commit_of_file": firsts}


# ---------------------------------------------------------------- BR-021 : empreinte disque
def size_bytes(s):
    m = re.match(r"([\d.]+)\s*([A-Za-z]+)", s.strip())
    return float(m.group(1)) * UNITS.get(m.group(2), 1) if m else 0.0


def docker_usage():
    out = sh("docker", "system", "df", "--format", "{{json .}}", check=False).stdout.strip().splitlines()
    return {j["Type"]: size_bytes(j["Size"]) for j in map(json.loads, out)}


def docker_inventory():
    # -a : `docker images` seul masque les images tirées par digest (sans étiquette) et les intermédiaires du constructeur ;
    # sans lui, la comparaison avant / après de BR-021 est aveugle à ce que le banc laisse
    imgs = sh("docker", "image", "ls", "-a", "--no-trunc", "--format", "{{.ID}} {{.Repository}}:{{.Tag}}", check=False).stdout.strip().splitlines()
    return {"images": sorted(imgs), "volumes": sorted(sh("docker", "volume", "ls", "-q", check=False).stdout.split()),
            "networks": sorted(sh("docker", "network", "ls", "--format", "{{.Name}}", check=False).stdout.split()),
            "containers": sorted(sh("docker", "ps", "-a", "--format", "{{.Names}} {{.Image}}", check=False).stdout.strip().splitlines())}


def free_gb():
    return shutil.disk_usage("/var/lib/docker" if os.path.isdir("/var/lib/docker") else "/").free / 1e9


def teardown(bench, purge):
    """Détruit conteneurs, volumes et réseaux du projet ; avec `purge`, supprime les images dont l'identifiant n'était pas dans
    l'inventaire d'avant le lancement (jamais `--rmi all`, jamais une purge par étiquette : BR-021)."""
    r = bench.compose("--profile", "late", "down", "-v", "--remove-orphans", check=False)
    left = []
    ps = sh("docker", "ps", "-a", "--filter", PROJECT_LABEL, "-q", check=False).stdout.split()
    vols = sh("docker", "volume", "ls", "--filter", PROJECT_LABEL, "-q", check=False).stdout.split()
    nets = sh("docker", "network", "ls", "--filter", PROJECT_LABEL, "-q", check=False).stdout.split()
    if r.returncode or ps or vols or nets:
        # `compose down` en échec ou incomplet (autre fichier compose, env absent) : démontage par le nom de projet compose,
        # restreint aux objets étiquetés com.docker.compose.project=isolation-bench (jamais au-delà)
        if r.returncode:
            print("[teardown] compose down en échec, démontage par étiquette de projet : " + r.stderr.strip()[-300:], file=sys.stderr)
        if ps:
            sh("docker", "rm", "-f", "-v", *ps, check=False)
        if vols:
            sh("docker", "volume", "rm", *vols, check=False)
        if nets:
            sh("docker", "network", "rm", *nets, check=False)
        ps = sh("docker", "ps", "-a", "--filter", PROJECT_LABEL, "-q", check=False).stdout.split()
        vols = sh("docker", "volume", "ls", "--filter", PROJECT_LABEL, "-q", check=False).stdout.split()
        nets = sh("docker", "network", "ls", "--filter", PROJECT_LABEL, "-q", check=False).stdout.split()
    if purge and os.path.exists(BASELINE):
        before = json.load(open(BASELINE))["images"]
        before_ids = {l.split()[0] for l in before}
        now = docker_inventory()["images"]
        todo = {line.split()[0] for line in now} - before_ids
        for _ in range(6):     # parents refusés tant que leurs enfants existent : passes successives, jamais -f ni étiquette
            for iid in sorted(todo):
                if sh("docker", "rmi", iid, check=False).returncode == 0:
                    todo.discard(iid)
            todo &= {line.split()[0] for line in docker_inventory()["images"]}
            if not todo:
                break
        sh("docker", "builder", "prune", "-f", check=False) if json.load(open(BASELINE)).get("build_cache_bytes", 1) == 0 else None
    if ps:
        left.append(f"{len(ps)} conteneur(s)")
    if vols:
        left.append(f"{len(vols)} volume(s)")
    if nets:
        left.append(f"{len(nets)} réseau(x)")
    hr = restore_hook()
    if hr is False:
        left.append("core.hooksPath non restauré")
    shutil.rmtree(RUN, ignore_errors=True)
    return left


def hook_prev_path():
    """Mémoire de core.hooksPath dans le dossier git commun (partagé par tous les worktrees, comme la config qu'elle
    restaure) : un --teardown lancé depuis un autre worktree la retrouve. None hors dépôt git."""
    r = git("rev-parse", "--path-format=absolute", "--git-common-dir")
    return os.path.join(r.stdout.strip(), "cairn-bench-hook-prev.json") if r.returncode == 0 and r.stdout.strip() else None


def install_hook():
    """BR-003 : le hook de pré-commit bloque un balayage positif ; il n'agit que si core.hooksPath le désigne.
    Absent ou déjà égal : posé et laissé en place (le blocage vaut pour chaque commit). Une autre valeur préexistante
    n'est pas écrasée en silence : remplacée pour le lancement, mémorisée dans <git-common-dir>/cairn-bench-hook-prev.json, restaurée au teardown
    (depuis n'importe quel worktree du dépôt)."""
    top = git("rev-parse", "--show-toplevel").stdout.strip()
    if not top:
        return {"installed": False, "reason": "hors dépôt git"}
    hooks = os.path.relpath(os.path.join(BENCH, "hooks"), top)
    cur = git("config", "--get", "core.hooksPath", cwd=top)
    prev = cur.stdout.strip() if cur.returncode == 0 else None
    same = prev is not None and os.path.realpath(os.path.join(top, prev)) == os.path.realpath(os.path.join(BENCH, "hooks"))
    out = {"previous": "absent" if prev is None else ("identique" if same else prev)}
    if not same:
        r = git("config", "core.hooksPath", hooks, cwd=top)
        if r.returncode:
            return dict(out, installed=False, reason="git config a échoué : " + r.stderr.strip()[-200:])
        if prev is not None:
            json.dump({"value": prev}, open(hook_prev_path(), "w"))
            out["restored_at_teardown"] = True
            print(f"[hook] core.hooksPath préexistant ({prev}) remplacé pour ce lancement ; restauré au teardown", file=sys.stderr)
    eff = git("config", "--get", "core.hooksPath", cwd=top).stdout.strip()
    return dict(out, installed=eff == hooks or same, **{"core.hooksPath": eff})


def restore_hook():
    """Remet la valeur de core.hooksPath mémorisée par install_hook ; None s'il n'y a rien à restaurer."""
    path = hook_prev_path()
    if not path or not os.path.exists(path):
        return None
    p = json.load(open(path))
    ok = git("config", "core.hooksPath", p["value"]).returncode == 0   # config partagée : écrite depuis ce worktree
    if ok:
        os.unlink(path)
    else:
        print(f"[hook] restauration de core.hooksPath ({p['value']}) en échec", file=sys.stderr)
    return ok


def hook_selftest(secret):
    """Essai du hook : un faux secret de la population fait échouer `git commit` sans être effacé du dépôt.
    Bloqué = le commit échoue, HEAD n'a pas bougé, et le hook configuré, rejoué seul (`git hook run`), refuse aussi."""
    top = git("rev-parse", "--show-toplevel").stdout.strip()
    rel = os.path.join("poc", "isolation-bench", "evidence", "hook-selftest.txt")
    path = os.path.join(top, rel)
    head0 = git("rev-parse", "HEAD", cwd=top).stdout.strip()
    open(path, "w").write("faux secret de la population : " + secret + "\n")
    try:
        git("add", "-f", rel, cwd=top)
        r = git("commit", "-m", "selftest du hook (ne doit pas aboutir)", "--", rel, cwd=top)
        head1 = git("rev-parse", "HEAD", cwd=top).stdout.strip()
        hk = git("hook", "run", "pre-commit", cwd=top)
        if head1 != head0:  # le commit a abouti : on revient exactement à HEAD d'avant (soft, jamais --hard, jamais HEAD~1)
            git("reset", "--soft", head0, cwd=top)
        blocked = r.returncode != 0 and head1 == head0 and hk.returncode != 0
        remains = os.path.exists(path)
        return {"blocked": blocked, "file_kept": remains, "git_returncode": r.returncode, "head_unchanged": head1 == head0,
                "hook_run_returncode": hk.returncode}
    finally:
        git("rm", "-q", "--cached", "--ignore-unmatch", rel, cwd=top)
        if os.path.exists(path):
            os.unlink(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["contextforge", "gateway", "v1"], default="contextforge")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--dev", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--purge-images", action="store_true")
    ap.add_argument("--teardown", action="store_true")
    a = ap.parse_args()
    bench = Bench(a.target)
    if a.teardown:
        left = teardown(bench, a.purge_images)
        print("teardown", "incomplet : " + ", ".join(left) if left else "ok")
        return 1 if left else 0

    policy = json.load(open(os.path.join(BENCH, "policy.json")))
    if not policy["cutoff_seconds"] < policy["access_token_lifetime_seconds"]:
        print("BR-011 : le délai de coupure doit être strictement inférieur à la durée de vie du jeton d'accès", file=sys.stderr)
        return 2
    cfg_files = ["policy.json"] + policy[CONFIG_KEY.get(a.target, "contextforge_config_files")]
    lifetime = policy["access_token_lifetime_seconds_v1"] if a.target == "v1" else policy["access_token_lifetime_seconds"]
    if not policy["cutoff_seconds"] < lifetime:
        print("BR-011 : le délai de coupure doit être strictement inférieur à la durée de vie du jeton d'accès", file=sys.stderr)
        return 2
    st = git_state(cfg_files)
    if not a.dev and st.get("git") and st["config_dirty"]:
        print("BR-011 : politique ou configuration non committée ; committez-les avant la recette (ou --dev)", file=sys.stderr)
        return 2
    hashes0 = {f: sha(f) for f in cfg_files}

    # --- BR-021 : seuil d'espace libre, inventaire d'avant, mesure d'avant
    free0 = free_gb()
    if free0 < MIN_FREE_GB:
        print(f"BR-021 : {free0:.1f} Go libres, moins de {MIN_FREE_GB} Go : le banc refuse de démarrer", file=sys.stderr)
        return 5
    inv0, use0 = docker_inventory(), docker_usage()

    # --- SCR-01 : banc
    print("[banc] secrets jetables et conteneurs…")
    teardown(bench, False)
    os.makedirs(RUN, exist_ok=True)
    hook = install_hook()     # après le démontage initial : la valeur mémorisée vit dans le dossier git commun jusqu'au teardown final
    print("[hook]", hook)
    json.dump({"images": inv0["images"], "volumes": inv0["volumes"], "build_cache_bytes": use0.get("Build Cache", 0)}, open(BASELINE, "w"))
    shutil.rmtree(os.path.join(BENCH, ".digests"), ignore_errors=True)
    gen_secrets.main(os.path.join(RUN, "env"))
    reg = secrets_guard.Registry()
    env = dict(l.strip().split("=", 1) for l in open(os.path.join(RUN, "env")))
    for k in ("U1_PASSWORD", "U2_PASSWORD"):
        reg.add(env[k] + "Aa1!")
    t0 = time.time()
    services = {"contextforge": []}.get(a.target, ["upstream", "gateway", "gitea"])
    if a.target == "v1":
        try:
            v1_secrets(bench)
        except RuntimeError as e:
            print("SCR-01.error : " + reg.redact(str(e)), file=sys.stderr)
            teardown(bench, a.purge_images)
            return 3
    r = bench.compose("up", "-d", "--build", "--wait", "--wait-timeout", "300", *services, check=False)
    if r.returncode:
        print("SCR-01.error : le banc n'a pas démarré\n" + reg.redact(r.stderr)[-1200:], file=sys.stderr)
        teardown(bench, a.purge_images)
        return 3
    controls = []
    up_url = f"http://localhost:{UP_PORT}"
    if a.target == "contextforge":
        from adapter_contextforge import ContextForgeAdapter
        adapter = ContextForgeAdapter(env, reg, f"http://localhost:{CF_PORT}", up_url, BENCH)
        extra = ()
    elif a.target == "v1":
        from adapter_v1 import V1Adapter
        mapping = json.load(open(os.path.join(BENCH, "mapping.json")))
        adapter = V1Adapter(env, reg, f"http://localhost:{GW_PORT}", up_url, BENCH, mapping)
        extra = [(f"http://localhost:{FRONT_CTL}", env["FRONT_CONTROL_TOKEN"]),
                 (f"http://localhost:{FRONT_RO_CTL}", env["FRONT_CONTROL_TOKEN"])]
    else:
        from adapter_gateway import GatewayAdapter
        mapping = json.load(open(os.path.join(BENCH, "mapping.json")))
        adapter = GatewayAdapter(env, reg, f"http://localhost:{GW_PORT}", f"http://localhost:{GW_ADMIN}", up_url, BENCH, mapping)
        extra = [(f"http://localhost:{FRONT_CTL}", env["FRONT_CONTROL_TOKEN"])]
    try:
        params = adapter.provision()
    except Exception as e:
        print("SCR-01.error : provisionnement incomplet : " + reg.redact(str(e)), file=sys.stderr)
        if not a.keep:
            teardown(bench, a.purge_images)
        return 3
    cl = adapter.claims(params["profiles"]["A"]["token"])
    if cl["exp"] - cl["iat"] != lifetime:
        print(f"BR-011 : durée de vie du jeton émis ({cl['exp'] - cl['iat']} s) différente de la politique", file=sys.stderr)
        if not a.keep:
            teardown(bench, a.purge_images)
        return 2
    print(f"[banc] SCR-01 prêt en {time.time() - t0:.0f} s ; profils {', '.join(params['profiles'])}")
    ctl = recette.Upstream(f"http://localhost:{UP_CTL}", env["UP_CONTROL_TOKEN"], extra)
    if a.target in GW_TARGETS:
        try:   # BR-013 : exhaustivité des deux journaux avant le premier test
            ex = recette.probe_exhaustiveness([("amont du banc", up_url, recette.Upstream(f"http://localhost:{UP_CTL}", env["UP_CONTROL_TOKEN"])),
                                               ("frontale Gitea", f"http://localhost:{FRONT_PORT}", recette.Upstream(f"http://localhost:{FRONT_CTL}", env["FRONT_CONTROL_TOKEN"]))])
        except recette.BenchHalt as e:
            print("SCR-01.error : " + str(e), file=sys.stderr)
            if not a.keep:
                teardown(bench, a.purge_images)
            return 3
        controls.append({"id": "BR-013", "title": "Exhaustivité des journaux amont", "result": recette.OK, "observed": ex})
        print("[BR-013] exhaustivité des deux journaux : ok")
    if a.target == "v1":
        try:   # BR-013 : les trois journaux de la v1 (amont du banc, frontales des deux serveurs MCP Gitea) tracent tout
            tok = env["FRONT_CONTROL_TOKEN"]
            ex = recette.probe_exhaustiveness([
                ("amont du banc", up_url, recette.Upstream(f"http://localhost:{UP_CTL}", env["UP_CONTROL_TOKEN"])),
                ("frontale Gitea", f"http://localhost:{FRONT_PORT}", recette.Upstream(f"http://localhost:{FRONT_CTL}", tok)),
                ("frontale Gitea lecture seule", f"http://localhost:{FRONT_RO_PORT}", recette.Upstream(f"http://localhost:{FRONT_RO_CTL}", tok))])
        except recette.BenchHalt as e:
            print("SCR-01.error : " + str(e), file=sys.stderr)
            if not a.keep:
                teardown(bench, a.purge_images)
            return 3
        controls.append({"id": "BR-013", "title": "Exhaustivité des journaux amont", "result": recette.OK, "observed": ex})
        print("[BR-013] exhaustivité des trois journaux : ok")

    # --- SCR-02 : recette
    started = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    results = recette.run(params, adapter, ctl, reg, policy, only=a.only.split(",") if a.only else None)
    if a.target == "v1" and not a.only:
        ctx = recette.Ctx(params, adapter, ctl, reg, policy)
        try:   # BR-015 (lecture v1, AC-29 amendé le 2026-10-31) et BR-022, contre gitea-mcp réel
            controls.append(recette.gitea_e2e_v1(ctx))
        except Exception as e:
            controls.append({"id": "BR-015/BR-022", "result": recette.NP, "reason": "difficulté du banc : " + reg.redact(f"{type(e).__name__}: {e}")[:300]})
        try:   # AC-2 of F-015 against real gitea-mcp: the reviewer of §3.3 on a sensitive role
            controls.append(recette.reviewer_ac2_v1(ctx))
        except Exception as e:
            controls.append({"id": "AC-2/F-015", "result": recette.KO, "reason": "control in error: " + reg.redact(f"{type(e).__name__}: {e}")[:300]})
        import oauth_checks   # H-2 : OAuth entrant de la v1, sondes par la méthode MCP ping
        try:
            controls.append(oauth_checks.run(adapter, reg))
        except Exception as e:
            controls.append({"id": "H-2", "result": recette.KO, "reason": "contrôle en erreur : " + reg.redact(f"{type(e).__name__}: {e}")[:300]})
        import socle_checks   # BR-026 : N1, N2, N4, sensible (BR-051), dcr
        os.makedirs(os.path.join(BENCH, "evidence"), exist_ok=True)
        try:
            controls.append(socle_checks.run(adapter, reg, V1_IMAGE))
        except Exception as e:
            controls.append({"id": "BR-026", "result": recette.KO, "reason": "contrôle en erreur : " + reg.redact(f"{type(e).__name__}: {e}")[:300]})
        controls += run_v1_harnesses(reg)
        nom = os.path.join(BENCH, "evidence", "v1-revoke-nominal-results.json")
        n13 = next((x for x in (json.load(open(nom)) if os.path.exists(nom) else []) if x.get("contrôle") == "sans_token_400_invalid_request"), None)
        controls.append({"id": "BR-026/N13", "title": "POST /revoke sans token : 400 invalid_request (assertion du harnais nominal v1)",
                         "result": recette.OK if n13 and n13["tenu"] else recette.KO, "observed": [n13 or {"absent": True}]})
        fps = {hashlib.sha256(t.encode()).hexdigest()[:12] for t in adapter.issued_tokens()}
        seen = [e for e in ctl.all_entries() if e.get("token_fp") in fps]
        controls.append({"id": "BR-022", "title": "Jeton entrant absent du journal amont", "result": recette.OK if not seen else recette.KO,
                         "observed": [{"empreintes_de_jetons_émis": len(fps), "entrées_portant_une_telle_empreinte": len(seen)}]})
        leak = recette.re.findall(r"eyJ[A-Za-z0-9_\-]{20,}", adapter.logs())
        controls.append({"id": "BR-066/journal", "title": "Journal de la passerelle v1 sans jeton (BR-053, BR-066)",
                         "result": recette.OK if not leak else recette.KO, "observed": [{"jwt_trouvés": len(leak)}]})
    if a.target in GW_TARGETS and not a.only:
        ctx = recette.Ctx(params, adapter, ctl, reg, policy)
        try:
            controls.append(recette.gitea_e2e(ctx, ctl))
        except Exception as e:
            controls.append({"id": "BR-015/BR-022", "result": recette.NP, "reason": "difficulté du banc : " + reg.redact(f"{type(e).__name__}: {e}")[:300]})
        import oauth_checks   # H-2 : OAuth entrant (PKCE, code, ressource, rotation, révocation, ping), hors des 16 tests
        try:
            controls.append(oauth_checks.run(adapter, reg))
        except Exception as e:
            controls.append({"id": "H-2", "result": recette.KO, "reason": "contrôle en erreur : " + reg.redact(f"{type(e).__name__}: {e}")[:300]})
        # BR-022 sur toute la durée : aucune empreinte d'un jeton émis par la passerelle dans un journal amont
        fps = {hashlib.sha256(t.encode()).hexdigest()[:12] for t in adapter.issued_tokens()}
        seen = [e for e in ctl.all_entries() if e.get("token_fp") in fps]
        controls.append({"id": "BR-022", "title": "Jeton entrant absent des journaux amont", "result": recette.OK if not seen else recette.KO,
                         "observed": [{"empreintes_de_jetons_émis": len(fps), "entrées_portant_une_telle_empreinte": len(seen)}]})
    hashes1 = {f: sha(f) for f in cfg_files}
    invalid = hashes0 != hashes1
    cfg = json.loads(bench.compose("config", "--format", "json", check=False).stdout or "{}")
    image_ids = {s: v.get("image") for s, v in cfg.get("services", {}).items()}
    net = {}
    if a.target in (*GW_TARGETS, "v1"):     # BR-016 : le réseau interne n'a pas de sortie ; qui y est attaché
        j = json.loads(sh("docker", "network", "inspect", "isolation-bench_internal", check=False).stdout or "[{}]")[0]
        net = {"internal": j.get("Internal"), "containers": sorted(c["Name"] for c in j.get("Containers", {}).values())}
        controls.append({"id": "BR-016", "title": "Réseau interne de Gitea, du serveur MCP Gitea et de la frontale", "observed": [net],
                         "result": recette.OK if net.get("internal") and {"gitea", "gitea-mcp", "front"} <= {re.sub(r"^isolation-bench-|-\d+$", "", n) for n in net["containers"]} else recette.KO})
    version = {}
    gm = open(os.path.join(BENCH, "..", "go-gateway", "go.mod")).read() if a.target == "gateway" else ""
    for mod in ("ory/fosite", "modelcontextprotocol/go-sdk", "golang.org/x/oauth2", "modernc.org/sqlite"):
        m = re.search(r"(\S*" + re.escape(mod) + r")\s+(v\S+)", gm)
        if m:
            version[m.group(1)] = m.group(2)
    if a.target == "v1":   # versions lues de uv.lock (BR-005)
        lock = open(os.path.join(BENCH, "..", "..", "uv.lock")).read()
        for mod in ("mcp", "mcp-types", "authlib", "pyjwt", "starlette", "uvicorn", "httpx", "httpx2", "cryptography", "pydantic"):
            m = re.search(r'name = "' + re.escape(mod) + r'"\nversion = "([^"]+)"', lock)
            if m:
                version[mod] = m.group(1)
    table_commit = [] if a.target == "v1" else git("log", "--diff-filter=A", "--format=%H", "--", REPORT.get(a.target) or "docs/trials/poc-go.md", cwd=os.path.dirname(os.path.dirname(BENCH))).stdout.strip().splitlines()
    table_commit = table_commit[-1] if table_commit else None
    anc = None
    if table_commit and st.get("head"):
        anc = git("merge-base", "--is-ancestor", table_commit, st["head"]).returncode == 0
    meta = {
        "date_utc": started, "target": a.target, "dev_mode": a.dev, "partial_run": bool(a.only), "images": image_ids, "module_versions": version,
        "bench_commit": st.get("head"), "config_dirty": st.get("config_dirty"), "config_sha256_start": hashes0,
        "config_unchanged_during_run": not invalid, "first_commit_of_config_files": st.get("first_commit_of_file"), "policy": policy,
        "table_first_commit": table_commit, "table_commit_is_ancestor_of_bench_commit": anc, "hook": hook,
        "source_dirty": bool(git("status", "--porcelain", "--", SOURCE[a.target]).stdout.strip()) if a.target in (*GW_TARGETS, "v1") else None,
    }
    if a.target == "v1":   # lectures propres à la v1 (AC-29 amendé, décision de l'opérateur du 2026-10-31), écrites dans mapping.json
        meta["lectures_v1"] = mapping.get("lectures_v1")
    if a.target == "contextforge":
        meta["contextforge_version"] = adapter.capability_evidence().get("version")
    if a.target in (*GW_TARGETS, "v1") and not a.only:
        controls.append({"id": "BR-003", "title": "Hook de pré-commit bloquant", "observed": [hook_selftest(env["UP_CONTROL_TOKEN"])], "result": recette.NP})
        controls[-1]["result"] = recette.OK if controls[-1]["observed"][0]["blocked"] and controls[-1]["observed"][0]["file_kept"] is True else recette.KO
    # claims du jeton d'accès émis, décodés (jamais le jeton) : critère « Jetons » de la spec
    if a.target in (*GW_TARGETS, "v1"):
        meta["access_token_claims"] = {k: v for k, v in adapter.claims(params["profiles"]["A"]["token"]).items() if k != "jti"}
        meta["access_token_header"] = adapter.header(params["profiles"]["A"]["token"])
    out = {"meta": meta, "tests": results, "controls": controls}
    # --- BR-021 : croissance mesurée avant teardown
    use1 = docker_usage()
    grow = sum(use1.values()) - sum(use0.values())
    meta["disk"] = {"free_gb_before": round(free0, 1), "docker_bytes_before": use0, "docker_bytes_after_run": use1, "growth_gb": round(grow / 1e9, 2), "ceiling_gb": MAX_GROWTH_GB}
    ev = os.path.join(BENCH, "evidence")
    os.makedirs(ev, exist_ok=True)
    fname = ("dev-" if (a.dev or a.only) else "") + RESULTS[a.target]
    # --- SCR-01.teardown
    left = []
    if not a.keep:
        left = teardown(bench, a.purge_images)
        inv1 = docker_inventory()
        meta["teardown"] = {"left": left, "images_equal_to_before": inv1["images"] == inv0["images"], "volumes_equal_to_before": inv1["volumes"] == inv0["volumes"],
                            "purged_images": a.purge_images, "free_gb_after": round(free_gb(), 1)}
        print("[banc] teardown", "incomplet : " + ", ".join(left) if left else "ok (secrets, volumes, conteneurs détruits)")
        if a.purge_images:
            print("[BR-021] listes d'images et de volumes identiques à celles d'avant :", meta["teardown"]["images_equal_to_before"] and meta["teardown"]["volumes_equal_to_before"])
    if grow > MAX_GROWTH_GB * 1e9:
        print(f"BR-021 : croissance de {grow / 1e9:.1f} Go au-delà de {MAX_GROWTH_GB} Go", file=sys.stderr)
    text = reg.redact(json.dumps(out, ensure_ascii=False, indent=2))
    open(os.path.join(ev, fname), "w").write(text + "\n")
    bad, n = secrets_guard.scan()
    print("[balayage]", "POSITIF : " + ", ".join(bad) if bad else f"propre ({n} empreintes)")
    counts = {k: sum(1 for t in results if t["result"] == k) for k in (recette.OK, recette.KO, recette.NJ, recette.NP)}
    print("[recette]", counts, "| configuration inchangée :", not invalid, "| contrôles :", {c["id"]: c["result"] for c in controls})
    return 0 if not (bad or left or invalid or grow > MAX_GROWTH_GB * 1e9) else 4


if __name__ == "__main__":
    sys.exit(main())
