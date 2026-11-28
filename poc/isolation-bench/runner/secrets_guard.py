#!/usr/bin/env python3
"""Population de secrets du banc et balayage des artefacts (BR-003, BR-010).

Population = secrets générés par le banc (.run/env), jetons reçus ou émis pendant la session (enregistrés par
l'exécuteur et l'adaptateur), pas une liste figée de motifs. Seuls des empreintes SHA-256 survivent au teardown
(.digests/) : elles ne sont pas des secrets et permettent de balayer un commit fait après la destruction des
secrets. Un jeton JWT est enregistré en entier et par segment (sauf l'en-tête, commun à tous).

Usage : secrets_guard.py scan [chemin ...]   (sans chemin : tous les fichiers suivis ou non ignorés du dépôt)
Code de sortie 1 si un secret de la population est trouvé : un balayage positif bloque le commit.
"""
import hashlib
import os
import re
import subprocess
import sys

BENCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIGESTS = os.path.join(BENCH, ".digests", "population.sha256")
RUN_ENV = os.path.join(BENCH, ".run", "env")
SEEN = os.path.join(BENCH, ".run", "seen_tokens")
CANDIDATE = re.compile(r"[A-Za-z0-9_\-.+/=]{8,}")
# BR-016: the private forge host is supplied by the workshop, never by public code.
# The registry subdomain is not the service host; require a hostname boundary.
PRIVATE_HOSTS = os.path.join(os.path.dirname(os.path.dirname(BENCH)),
                             ".cairn", "runs", "private", "bench-forbidden-hosts.txt")
FORBIDDEN_HOSTS = re.compile(r"(?<![\w.-])gitea\.com(?![\w-])", re.I)


def forbidden_hosts():
    if not os.path.exists(PRIVATE_HOSTS):
        print("BR-016 SKIP: .cairn/runs/private/bench-forbidden-hosts.txt absent; "
              "private forge-host check skipped, public pattern still enforced")
        return FORBIDDEN_HOSTS
    with open(PRIVATE_HOSTS, encoding="ascii") as source:
        hosts = [line.strip() for line in source if line.strip()]
    if not hosts or any(not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", h)
                        for h in hosts):
        raise ValueError("BR-016: invalid private forge-host input")
    return re.compile(r"(?<![\w.-])(?:gitea\.com|" +
                      "|".join(re.escape(h) for h in hosts) + r")(?![\w-])", re.I)


HOST_SCOPE = ("poc" + os.sep, os.path.join("docs", "trials", "poc-go.md"))
JWT_LIKE = re.compile(r"eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}(\.[A-Za-z0-9_-]*)?")


def _h(s):
    return hashlib.sha256(s.encode()).hexdigest()


def _forms(secret):
    out = {secret}
    if secret.count(".") == 2 and secret.startswith("eyJ"):
        out.update(p for p in secret.split(".")[1:] if len(p) >= 8)
    return out


class Registry:
    """Enregistre les secrets vus pendant la session : en clair dans .run (détruit au teardown), empreintes dans .digests."""

    def __init__(self):
        os.makedirs(os.path.dirname(DIGESTS), exist_ok=True)
        os.makedirs(os.path.dirname(SEEN), exist_ok=True)
        self.plain = set()
        self.add_env()

    def add_env(self):
        if os.path.exists(RUN_ENV):
            for line in open(RUN_ENV):
                if "=" in line:
                    self.add(line.strip().split("=", 1)[1], track=False)

    def add(self, secret, track=True):
        if not secret or len(secret) < 8 or secret in self.plain:
            return
        self.plain.add(secret)
        if track:
            with open(SEEN, "a") as f:
                f.write(secret + "\n")
        with open(DIGESTS, "a") as f:
            for form in _forms(secret):
                f.write(_h(form) + "\n")

    def redact(self, text):
        if not isinstance(text, str):
            text = str(text)
        for s in sorted(self.plain, key=len, reverse=True):
            text = text.replace(s, "<redacted>")
        return JWT_LIKE.sub("<redacted-jwt>", text)


def load_digests():
    if not os.path.exists(DIGESTS):
        return set()
    return {l.strip() for l in open(DIGESTS) if l.strip()}


def scan_text(text, digests):
    hits = []
    for m in CANDIDATE.finditer(text):
        tok = m.group(0)
        cands = {tok, tok.strip(".")} | {p for p in tok.split(".") if len(p) >= 8}
        if any(_h(c) in digests for c in cands):
            hits.append(m.start())
    for m in JWT_LIKE.finditer(text):
        hits.append(m.start())
    return hits


def repo_files():
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=BENCH, capture_output=True, text=True).stdout.strip()
    out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=top, capture_output=True, text=True).stdout.split("\n")
    return [os.path.join(top, f) for f in out if f]


def scan(paths=None):
    hosts = forbidden_hosts()
    digests = load_digests()
    # le registre courant (si la session est ouverte) complète les empreintes
    reg = Registry()
    digests = load_digests()
    bad = []
    for p in paths or repo_files():
        if not os.path.isfile(p):
            continue
        try:
            text = open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        hits = scan_text(text, digests)
        rel = os.path.relpath(p, os.path.dirname(os.path.dirname(BENCH)))
        if rel.startswith(HOST_SCOPE) and os.path.basename(p) != "secrets_guard.py":
            m = hosts.search(text)
            if m:
                hits.append(m.start())
        if hits:
            line = text.count("\n", 0, hits[0]) + 1
            bad.append(f"{os.path.relpath(p)}:{line}")
    return bad, len(digests)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "scan":
        bad, n = scan(sys.argv[2:] or None)
        if bad:
            print(f"BALAYAGE POSITIF ({n} empreintes) : secret trouvé dans " + ", ".join(bad), file=sys.stderr)
            sys.exit(1)
        print(f"balayage propre ({n} empreintes de la population)")
    else:
        print(__doc__)
        sys.exit(2)
