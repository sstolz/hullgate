#!/usr/bin/env python3
"""BR-008 : l'essai n'écrit ni `project.stack` ni un choix de pile (ni ADR) dans le dépôt.

Échoue (code 1) si le diff de la branche contre la base touche `.cairn/project-context.yaml`, ajoute un fichier d'ADR
(chemin contenant `adr/` ou nommé `ADR-*`), ou si `project.stack` n'est plus vide.
Usage : check_scope.py [base]   (base par défaut : origin/main)
"""
import re
import subprocess
import sys

base = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True).stdout.strip()
names = subprocess.run(["git", "diff", "--name-only", f"{base}...HEAD"], cwd=top, capture_output=True, text=True, check=True).stdout.split()
errs = []
if ".cairn/project-context.yaml" in names:
    errs.append("le diff touche .cairn/project-context.yaml")
errs += [f"fichier d'ADR dans le diff : {n}" for n in names if re.search(r"(^|/)adr/|(^|/)ADR-", n)]
ctx = open(f"{top}/.cairn/project-context.yaml", encoding="utf-8").read()
m = re.search(r"^\s*stack:\s*(.*)$", ctx, re.M)
if not m or m.group(1).strip() not in ('""', "''", ""):
    errs.append(f"project.stack n'est pas vide : {m.group(1) if m else 'key absent'}")
if errs:
    print("BR-008 violée : " + "; ".join(errs), file=sys.stderr)
    sys.exit(1)
print(f"BR-008 tenue : {len(names)} fichier(s) dans le diff, project.stack intact")
