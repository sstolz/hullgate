#!/usr/bin/env python3
"""Contrôle de structure du rapport d'essai (BR-004 v2, BR-009, BR-007 ; cible gateway : BR-018, BR-023).

  report_check.py [--partial] [rapport.md]                     # docs/trials/contextforge.md (F-001)
  report_check.py --target gateway [--partial] [rapport.md]    # docs/trials/poc-go.md (F-002)

- exactement 16 lignes de test T01..T16, résultat parmi réussi / échoué / non jouable / non joué ;
- toute ligne échouée ou non jouable renvoie à un écart E-nn qui porte `Cause:`, `Fix:` (overlay | in-core) et `Cost: <n> d` ;
- toute ligne non jouable cite sa preuve (BR-009) ;
- rapport final : aucune ligne « non joué », un verdict go / no-go ; avec --partial, le rapport ne porte pas de verdict (BR-007) ;
- cohérence avec evidence/results.json quand il existe (le rapport ne peut pas contredire la recette).
"""
import json
import os
import re
import sys

BENCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = {"réussi", "échoué", "non jouable", "non joué"}


CLASSES = {"contextforge": r"(overlay|in-core)", "gateway": r"(library|in-house)"}   # BR-004 v2: classes by target (English reports since HULL-58)
RESULT_FILES = {"contextforge": "results.json", "gateway": "results-gateway.json"}
REPORTS = {"contextforge": "contextforge.md", "gateway": "poc-go.md"}


ISOLATION = ["T02", "T03", "T04", "T05", "T06", "T07", "T08", "T09", "T12", "T13", "T14"]           # BR-023
HYPOTHESES = [f"H-{i}" for i in range(1, 9)]                                                       # BR-018


def check_gateway(text, partial, top):
    errs = []
    hyp = {}
    for line in text.splitlines():
        m = re.match(r"\|\s*(H-\d)\s*\|(.*)\|\s*$", line)
        if m and m.group(1) not in hyp:   # première occurrence : le tableau go / no-go (d'autres tableaux peuvent citer H-n)
            hyp[m.group(1)] = [c.strip() for c in m.group(2).split("|")]
    if sorted(hyp) != HYPOTHESES:
        errs.append(f"BR-018 : le tableau go / no-go doit avoir les lignes H-1..H-8 (trouvé : {sorted(hyp)})")
    for hid, cells in hyp.items():
        if len(cells) < 4 or not cells[1] or not cells[2]:
            errs.append(f"BR-018 : {hid} sans critère ou sans preuve attendue")
    rf = os.path.join(BENCH, "evidence", "results-gateway.json")
    if not partial:
        meta = json.load(open(rf))["meta"] if os.path.exists(rf) else {}
        if not meta:
            errs.append("rapport final sans evidence/results-gateway.json")
        elif meta.get("dev_mode") or meta.get("partial_run") or meta.get("source_dirty") or not meta.get("config_unchanged_during_run"):
            errs.append("results-gateway.json n'est pas probant (mode --dev, --only, source ou configuration modifiée)")
        elif meta.get("table_commit_is_ancestor_of_bench_commit") is not True:
            errs.append("BR-018 : le commit du tableau go / no-go n'est pas un ancêtre du bench_commit des résultats")
        elif meta.get("table_first_commit", "x")[:7] not in text or meta.get("bench_commit", "x")[:7] not in text:
            errs.append("BR-018 : le rapport ne consigne pas le SHA du tableau et le bench_commit")
    if not partial:
        v = re.search(r"^##\s+Verdict\b(.*?)(?=^##\s|\Z)", text, re.S | re.M)
        body = v.group(1) if v else ""
        if "BR-023" not in body:
            errs.append("BR-023 : le verdict ne cite pas son critère")
        missing = [t for t in ISOLATION if t not in body]
        if missing:
            errs.append(f"BR-023 : le verdict ne cite pas l'état des lignes d'isolation {missing}")
        if not re.search(r"\bsum\b", body, re.I) or not re.search(r"5\s*d\b", body):
            errs.append("BR-023 : le verdict ne met pas la somme des écarts restants face au seuil de 5 j")
        if re.search(r"\*\*go\b", body) and "Claude.ai / iOS reservation open" not in body:
            errs.append("BR-023 : un « go » porte la mention « réserve Claude.ai / iOS ouverte »")
    return errs


def check(path, partial, target="contextforge"):
    errs = []
    text = open(path, encoding="utf-8").read()
    rows = {}
    for line in text.splitlines():
        m = re.match(r"\|\s*(T\d\d)\s*\|(.*)\|\s*$", line)
        if m:
            cells = [m.group(1)] + [c.strip() for c in m.group(2).split("|")]
            if m.group(1) in rows:
                errs.append(f"{m.group(1)} apparaît deux fois")
            rows[m.group(1)] = cells
    expected = [f"T{i:02d}" for i in range(1, 17)]
    if sorted(rows) != expected:
        errs.append(f"le tableau doit avoir exactement 16 lignes T01..T16 (trouvé : {sorted(rows)})")
    ecarts = {}
    for m in re.finditer(r"^###\s+(E-\d+)\b(.*?)(?=^###\s+E-\d+\b|^##\s|\Z)", text, re.S | re.M):
        body = m.group(2)
        ecarts[m.group(1)] = body
    for eid, body in ecarts.items():
        if not re.search(r"Cause\s*:\s*\S", body):
            errs.append(f"{eid} : cause absente")
        if not re.search(r"Fix\s*:\s*" + CLASSES[target] + r"\b", body, re.I):
            errs.append(f"{eid} : correctif non classé {CLASSES[target]} (BR-004 v2)")
        if not re.search(r"Cost[^:\n]*:\s*[\d.,]+\s*(d\b|day)", body, re.I):
            errs.append(f"{eid} : coût en jours absent")
    res_file = os.path.join(BENCH, "evidence", RESULT_FILES[target])
    machine = {t["id"]: t["result"] for t in json.load(open(res_file))["tests"]} if os.path.exists(res_file) else {}
    for tid, cells in rows.items():
        # cellules : Id | Test | Résultat | Écart | Preuve
        if len(cells) < 5:
            errs.append(f"{tid} : colonnes attendues Id | Test | Résultat | Écart | Preuve")
            continue
        result, ecart, preuve = cells[2], cells[3], cells[4]
        if result not in RESULTS:
            errs.append(f"{tid} : résultat « {result} » inconnu")
        if result == "non joué" and not partial:
            errs.append(f"{tid} : « non joué » dans un rapport final (BR-009, BR-007)")
        if result in ("échoué", "non jouable"):
            ids = re.findall(r"E-\d+", ecart)
            if not ids or any(i not in ecarts for i in ids):
                errs.append(f"{tid} : {result} sans écart documenté (BR-004)")
        if result == "non jouable" and len(preuve) < 15:
            errs.append(f"{tid} : non jouable sans preuve citée (BR-009)")
        if machine and tid in machine and machine[tid] != result:
            errs.append(f"{tid} : le rapport dit « {result} », evidence/results.json dit « {machine[tid]} »")
    if target == "gateway":
        errs += check_gateway(text, partial, os.path.dirname(os.path.dirname(BENCH)))
    has_verdict = re.search(r"^##\s+Verdict\b.*?\b(no-go|go)\b", text, re.S | re.M | re.I) is not None
    if partial and has_verdict:
        errs.append("un rapport partiel ne porte pas de verdict (BR-007)")
    if not partial:
        if not has_verdict:
            errs.append("verdict go / no-go absent")
        if target == "contextforge" and re.search(r"Verdict[^\n]*\n+[^\n]*\bcriterion\b", text, re.I) is None and "BR-007" not in text:
            errs.append("le verdict doit citer son critère (BR-007)")
    return errs


if __name__ == "__main__":
    argv = sys.argv[1:]
    target = "contextforge"
    if "--target" in argv:
        i = argv.index("--target")
        target = argv[i + 1]
        del argv[i:i + 2]
    args = [a for a in argv if not a.startswith("--")]
    default = REPORTS[target]
    path = args[0] if args else os.path.join(os.path.dirname(os.path.dirname(BENCH)), "docs", "trials", default)
    errs = check(path, "--partial" in sys.argv, target)
    if errs:
        print("rapport invalide :\n - " + "\n - ".join(errs), file=sys.stderr)
        sys.exit(1)
    print("rapport conforme (" + ("partiel" if "--partial" in sys.argv else "final") + ")")
