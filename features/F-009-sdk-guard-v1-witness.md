---
id: F-009
title: SDK guard witness — switch to the bench's v1 target
ticket: HULL-21
status: implemented
closed: 2026-10-31
capabilities:
  extends: [mcp SDK version guard]
  introduces: []
domain: [foundation, quality-gate]
br-introduced: []
br-touched: [BR-037]
decisions: []
drafted-by: [model:claude-sonnet-5-5]
validated-by: human:sstolz
---

# F-009 · SDK guard witness — switch to the bench's v1 target

## Metadata

| Field | Value |
|-------|-------|
| **Priority** | P2 |
| **Branch** | `feat/HULL-21-sdk-guard-v1` |
| **Last updated** | 2026-10-31 |

## 1. Context & Objective

### Current State

`scripts/sdk_guard.py` (step `sdk-guard` of `make gate`, BR-036 and BR-037, ADR-001 conditions 1 and 2) ties the `mcp` and `mcp-types` versions of the root `uv.lock` to a conclusive bench replay. Today that replay is the **Python POC's**: `poc/isolation-bench/evidence/results-py-gateway.json` (`bench_commit` `a8faea7`, target `py-gateway`), read together with the POC's own lock `poc/py-gateway/uv.lock` (SHA-256 fingerprint compared with `meta.config_sha256_start`). `make bench-sdk` replays `./bench.sh --target py-gateway`, and the README procedure (§"Upgrading the mcp SDK") asks that both `pyproject.toml` files and both locks be bumped.

That arrangement was a stopgap (F-004, BR-037 v1 clarification): the bench had no v1 target. Since F-005 it has one (`./bench.sh --target v1`, BR-071), and since F-008 lot B it passes in full. Facts read on `main` at `422bf0c`:

- `poc/isolation-bench/evidence/results-v1.json`: `meta.target` `v1`, `dev_mode` false, `partial_run` false, `bench_commit` `56cb4e3`, `module_versions` `mcp` and `mcp-types` `2.3.0` (root `uv.lock` resolves both to `2.3.0`), 16 tests `réussi`, 11 controls `réussi`.
- The evidence records the SHA-256 of the root lock (`meta.config_sha256_start["../../uv.lock"]` = `ff760224…`), equal to the current `uv.lock`.
- `56cb4e3` is **not** an ancestor of `main` (squash merge): any check that follows `bench_commit` through git history fails.
- The comment 10422 on the ticket (`aa5b163`, 5 `réussi` / 11 `non jouable`) is outdated: it described the evidence before F-008 lot B, which is why switching the witness had to wait (order settled by the operator on 2026-10-31).

The cost of the stopgap: every SDK bump must also touch and replay a disposable POC, and the guard certifies the POC's behaviour under the new SDK, not the gateway's.

### Objective

The guard certifies the gateway: an SDK version enters `main` only when the isolation bench's **v1 target** has replayed it. The Python POC stops being read by the guard. No change to the gateway's behaviour.

### Expected Impact

- A bump touches one `pyproject.toml`, one lock and one bench target (today: two of each plus a POC kept alive only for the guard).
- `make bench-sdk` replays the target that judges the shipped code; the N3 and nominal `/revoke` harnesses are part of that pass (BR-071), so the manual "replay and quote in the PR" step disappears.
- `poc/py-gateway/` stops being a hidden dependency of `make gate`.
- No bench run is needed by this feature: the committed `results-v1.json` already satisfies the new rule.

### Open Product Questions

All five are settled in this draft by the recommendation shown; the freeze is the operator's confirmation or correction. The orchestrator relays them with the same numbering.

1. **What the guard reads (and the lock fingerprint).** Options: (a) v1 evidence replaces the POC evidence, POC kept as unread history, **no lock fingerprint** — versions of `mcp` and `mcp-types` in `meta.module_versions` (read by the bench from the root lock at run time) must equal the root lock's; (b) same, plus the SHA-256 of the root `uv.lock` equal to `config_sha256_start["../../uv.lock"]`; (c) v1 primary and POC evidence still required as second witness. Cost of (b): the root lock holds every dependency, dev tools included; F-007 added 372 lines of QA packages to it (`1c7b4ea`), so under (b) any ruff, pytest or PyJWT bump would demand a full bench replay (≥10 GB free, BR-021), against the accepted position that those packages climb without replay (F-004 m11, H13) and against BR-037's subject (the SDK version only). Cost of (c): the POC keeps needing bumps. **Recommendation: (a).** The fingerprint exists today and matches, so (b) remains a one-line addition if the operator wants it.
2. **Verdicts or readings.** The guard needs no knowledge of the v1 readings of T06 to T10 and BR-015 (AC-29 amendments of F-008): they are bench-internal (`mapping.json`, `meta.lectures_v1`), judged by the bench under BR-071, and arrive at the guard as a verdict. What the guard demands of verdicts: (a) keep today's floor, T01..T16 present and the 11 isolation lines `réussi`; (b) all 16 tests `réussi` and every recorded control `réussi`, the N3 and nominal revocation harnesses named explicitly. ADR-001 condition 2 reads "16 tests and the out-of-recipe controls N3, nominal `/revoke`, T10". The v1 evidence satisfies both. **Recommendation: (b).** Cost: a future bench change that makes a non-isolation test legitimately `non jouable` blocks an SDK bump until BR-071 is settled; that block is the intended one.
3. **Fate of `poc/py-gateway/` and the bench's `py-gateway` target.** After this feature the guard no longer reads them, so BR-037's "no longer disposable" consequence lapses. Options: (a) keep both untouched as history (176 KB + the bench target; the only recurring cost is that bench changes must keep the POC adapters intact, a review finding of F-008 lot B); (b) delete them (git history keeps them) — they are cited by controls and implementation references of BR-004, BR-018, BR-020, BR-023 to BR-027, by `docs/trials/poc-python.md` and `report_check.py`: eight rules to touch, out of proportion here; (c) archive under another path — changes every cited path, same eight rules. **Recommendation: (a)**, with the deletion as a separate backlog candidate (§Out of Scope), decided if and when the bench maintenance cost shows.
4. **`make bench-sdk`.** It replays `./bench.sh --target v1` (full, never `--dev`) then runs the guard. Options on the flags: (a) bare `--target v1`; (b) `--target v1 --purge-images`, the form F-005's AC-08 used, which returns the shared VM disk to its starting point; (c) a free-disk pre-check in the Makefile — redundant, the bench refuses by itself under 10 GB free and fails above 4 GB growth (BR-021). **Recommendation: (b)**, no pre-check. The bench takes minutes (duration of the v1 pass not measured by this framing), needs `buildx`, which the session host lacks, so it is run in the toolbox with `/workspace/.git` mounted (inbox, F-005 lot B); the README says so.
5. **Evidence freshness.** When gateway code changes and the SDK does not, the guard does not care, and says so in BR-037 v2: it reads the SDK version and the verdicts, never `bench_commit`, `source_dirty`, `config_dirty`, a lock fingerprint or the gateway sources. Rationale: BR-037 binds the SDK version; the gateway is judged by BR-071 at each feature's closure; `bench_commit` cannot be followed through squash-merged history anyway. **Recommendation: no freshness check**, as stated; no operator decision expected beyond confirming it.

## 2. Scope

- `scripts/sdk_guard.py`, step "bench": read `poc/isolation-bench/evidence/results-v1.json`; require `meta.target` `v1`, `dev_mode` and `partial_run` false, `module_versions` `mcp` and `mcp-types` equal to the root `uv.lock`'s, T01..T16 present and `réussi`, a non-empty `controls` list all `réussi` with the N3 and nominal revocation harnesses present. Drop the POC lock, its fingerprint and the 11-line floor.
- `make bench-sdk` replays `./bench.sh --target v1 --purge-images`, then the guard.
- README §"Upgrading the mcp SDK" rewritten for the single-lock procedure and the toolbox; comment of `pyproject.toml` line 45 and the module docstring updated.
- `tests/unit/test_br_037_sdk_guard.py` rewritten around the v1 evidence, one case per condition, plus the cases that prove what the guard no longer reads.
- BR-037 v2 (written inline in §4, extracted at `close-feature`); capability-map row "Garde de version du SDK mcp" and CHANGELOG entry at closure.

## Out of Scope

- **Deleting or archiving `poc/py-gateway/` and the `py-gateway` bench target** (question 3): candidate backlog ticket, to be opened at `close-feature` Phase 6 if the operator wants it; touches eight rules of F-003.
- **Binding the other isolation-bearing packages** (PyJWT, Starlette, Authlib) to a replay: accepted since F-004 (m11, H13); unchanged.
- **A freshness or ancestry check of the evidence** (question 5): refused by design.
- **Recording an SDK-slice digest of the lock in the evidence**, a middle path between questions 1(a) and 1(b): needs a bench change and a replay; candidate for later if the operator wants a lock link without binding every package.
- **Any change to the bench, its recipe, its readings or `results-v1.json`**, and any bench run: this feature plays none.
- **Any change to the gateway.**

## 3. Functional Spec

### 3.1 The guard (SCR-01, CLI `python scripts/sdk_guard.py [--root <repo>]`)

Step "pin" (BR-036) is unchanged. Step "bench" reads `<root>/poc/isolation-bench/evidence/results-v1.json` and stops at the first failing condition with `SDK-GUARD bench KO <condition>` on stderr and exit 1; all hold: `SDK-GUARD bench OK mcp <v>, mcp-types <v> (results-v1.json)` on stdout, exit 0. Conditions, in order:

1. evidence file present;
2. `meta.target` is `v1`;
3. `meta.dev_mode` is `false`; `meta.partial_run` is `false`;
4. `meta.module_versions` has `mcp` and `mcp-types`, each equal to the root `uv.lock`'s;
5. `tests` carries T01..T16, each `réussi`;
6. `controls` is non-empty, every entry `réussi`, and the two revocation harnesses (`H-10/HULL-5-review-2-n3`, `H-10/HULL-5-review-3-revoke-nominal`) are among them.

A missing or malformed key is a refusal with a named line, never a traceback. Nothing else of the evidence or the repository is read by the step.

### 3.2 `make bench-sdk` (SCR-02)

Prints its prerequisites (version bumped in `pyproject.toml`, `uv lock`, both committed; ≥10 GB free; run where `buildx` exists), runs `cd poc/isolation-bench && ./bench.sh --target v1 --purge-images`, then `python scripts/sdk_guard.py`.

### 3.3 Upgrade procedure (README, SCR-03)

1. Change `mcp==X.Y.Z` in `pyproject.toml`; `uv lock`; check `mcp-types` followed. 2. Commit both (the bench refuses an uncommitted configuration outside `--dev`). 3. `make bench-sdk`, in the toolbox. 4. Commit `evidence/results-v1.json` (and the two v1 harness files the pass rewrites); `make gate` must end `GATE: OK`. Other packages bump without a replay.

## 4. Business Rules

### BR-037 · An mcp SDK version enters only when the bench's v1 target has replayed it (v2)

> **Extracted on 2026-10-31.** Current version: [`business-rules/BR-037.md`](../business-rules/BR-037.md).
> The content below reflects the state of this BR at the time of feature F-009 (v2).

*`behavioral` · `strict` · Candidate state: `verified` — `scripts/sdk_guard.py` §check_bench (step `sdk-guard` of `make gate`): `meta.target` `v1`, `dev_mode` and `partial_run` false, `mcp` and `mcp-types` of `module_versions` equal to the root `uv.lock`'s, T01..T16 all `réussi`, a non-empty `controls` list all `réussi` with the N3 and nominal harnesses present; one fabricated case per condition in `tests/unit/test_br_037_sdk_guard.py`.*

**Rule** — The gate refuses an `mcp` SDK version (packages `mcp` and `mcp-types`) that is not the one of a conclusive full replay of the isolation bench's `v1` target recorded in the repository (condition 2 of ADR-001): a replay is conclusive when its evidence is neither a development nor a partial run and every test of the recipe and every control it records has passed.

**Clarifications**
- The evidence read is `poc/isolation-bench/evidence/results-v1.json`. The guard reads verdicts only (`tests[].result`, `controls[].result`); the v1 readings of T06 to T10 and BR-015 (`meta.lectures_v1`, `mapping.json`, AC-29 of F-008) belong to the bench (BR-071) and are not known to the guard.
- Freshness is out of the rule: the guard reads the SDK version and the verdicts, not `bench_commit`, `source_dirty`, `config_dirty`, any lock fingerprint or the gateway sources. A gateway, Dockerfile or other-dependency change does not require a replay under this rule; the gateway is judged by BR-071 when a feature closes. `bench_commit` cannot be followed through squash-merged history in any case.
- Upgrade procedure (README and `make bench-sdk`): change `mcp==X.Y.Z`, `uv lock`, commit, `./bench.sh --target v1 --purge-images` (conclusive, never `--dev`) run in the toolbox, commit the evidence, PR. The N3 and nominal `/revoke` harnesses run inside that pass and are checked by the guard.
- Superseded procedure of v1: the Python POC (`poc/py-gateway/`, `results-py-gateway.json`) was the witness until this version. It is kept as history, no longer read, no longer follows SDK bumps and no longer has to exist for the gate to pass.
- State at v2: root lock and evidence both say `mcp` and `mcp-types` 2.3.0 (`bench_commit` `56cb4e3`); v2 is born conforming.

**Example**
- **Given** `pyproject.toml` pins `mcp==2.4.0`, the lock resolves it, and `results-v1.json` still records 2.3.0
- **When** `make gate` reaches `sdk-guard`
- **Then** it ends `GATE: KO sdk-guard` and names `mcp : uv.lock de la v1 2.4.0, banc 2.3.0`.

*Overlap scan (`br-identifiers.md` §Overlap)*: filter on SDK, mcp, banc, évidence, rejeu, garde, verrou, montée over the 129-row index: **10 lines retained** (BR-005, BR-018, BR-021, BR-025, BR-029, BR-035, BR-036, BR-037, BR-067, BR-071). Verdicts after opening: BR-005, BR-018, BR-021, BR-025, BR-029, BR-035, BR-067 off topic; BR-036 — neighbour (pin form), unchanged; BR-071 — neighbour (the bench judges the v1; BR-037 consumes its verdict). Overlap: **BR-037 — overlap — new version (C2)**: a repository whose only witness is the POC evidence satisfied v1 and no longer satisfies v2.

## 5. Acceptance Criteria

- [ ] **AC-01** (BR-037) — on the branch, `python3 scripts/sdk_guard.py` exits 0 and prints `SDK-GUARD pin OK mcp==2.3.0` then a `SDK-GUARD bench OK` line naming `results-v1.json`; and `make gate` ends `GATE: OK`. No bench run: `git diff --name-only main...HEAD -- poc | wc -l` prints `0`.
- [ ] **AC-02** (BR-037) — `pytest tests/unit/test_br_037_sdk_guard.py -q` is green, with one fabricated case per condition of §3.1: file absent; `target` other than `v1` (including the POC evidence); `dev_mode` true; `partial_run` true; `mcp` differs; `mcp-types` differs; `module_versions.<package>` absent; one test absent (T16); each of the 16 tests not `réussi` (parametrised); `controls` absent or empty; one control `échoué`; each of the two named harnesses missing. Each case asserts the stderr line names its condition.
- [ ] **AC-03** (BR-037) — the guard no longer needs the POC: `d=$(mktemp -d) && git archive HEAD | tar -x -C "$d" && rm -rf "$d/poc/py-gateway" "$d/poc/isolation-bench/evidence/results-py-gateway.json" && python3 scripts/sdk_guard.py --root "$d"` exits 0 (replay after the commit). Absence guard: `grep -nE "py-gateway|WITNESS|config_sha256_start" scripts/sdk_guard.py` returns no line; its presence twin `grep -c "results-v1.json" scripts/sdk_guard.py` returns at least 1.
- [ ] **AC-04** (BR-037) — readings and freshness are ignored: a unit case changes `meta.lectures_v1`, `bench_commit`, `source_dirty`, `config_dirty` and `config_sha256_start` of an otherwise conforming evidence and the guard still exits 0; `grep -nE "lectures_v1|bench_commit|source_dirty|config_dirty" scripts/sdk_guard.py` returns no line (twin: the unit case itself, red if the guard starts reading one).
- [ ] **AC-05** (BR-037) — the procedure follows the new target: `make -n bench-sdk | grep -c -- "--target v1 --purge-images"` prints `1`; `make -n bench-sdk | grep -c "py-gateway"` prints `0`; `grep -n "py-gateway" README.md` returns no line inside §"Upgrading the mcp SDK" other than at most one sentence marking the POC as history (read by hand); `grep -n "toolbox\|buildx" README.md` finds the line telling where the bench runs.
- [ ] **AC-06** (BR-036) — the pin cases of `test_br_036_*` remain green and unchanged.
- [ ] **AC-07** (BR-037) — at `close-feature`: `business-rules/BR-037.md` is `version: 2` with its `control` entry rewritten to the v1 evidence and its v1 text in Archives, `cairn render-index business-rules` leaves `business-rules/index.md` unchanged on a second run, and `cairn check` exits 0.

## 6. Diagrams

Selection matrix: **userflow** and **sequence** skipped (a single script reading one file, no layers exchanging messages); **state machine**, **ER**, **class** skipped (no state, schema or hierarchy). The decision order of §3.1 is the whole behaviour and is written there as a numbered list.

## 7. States & Captures

`package-status: absent` — no UI, no design assets.

## 9. Implementation Notes

### Dependencies (prerequisite features)

- F-008 — Relais (implemented 2026-10-31): lot B produced `results-v1.json` 16/16. Satisfied.
- F-005 — Socle (implemented): the bench's `v1` target (BR-071).
- F-004 — Fondation (implemented): BR-036, BR-037 v1, `sdk_guard.py`, `make bench-sdk`.
- Downstream: none known. HULL-10 (policy, per-tool masking) will tighten the T09 reading in the bench (BR-071); the guard is unaffected.

### Technical Constraints

- Python 3.13 standard library only in `sdk_guard.py`, as today; `mypy --strict` clean.
- The bench is read, not run, and not modified; `results-v1.json` is not rewritten by this feature.
- A bench run needs ≥10 GB free on the VM disk (BR-021) and runs in the toolbox.

### Architecture Decisions

| Question | Decision | Rationale |
|----------|----------|-----------|
| Witness | `results-v1.json` replaces the POC evidence; POC kept unread | the guard should certify the shipped gateway (question 1) |
| Lock fingerprint | none | the root lock holds every dependency; BR-037 binds the SDK version only (question 1) |
| What is read | verdicts of 16 tests and all controls, N3 and nominal named | ADR-001 condition 2; readings stay in the bench (question 2) |
| POC fate | untouched, deletion a separate ticket | eight rules cite it (question 3) |
| `make bench-sdk` | `--target v1 --purge-images` | the form of F-005 AC-08, disk-safe on the shared VM (question 4) |
| Freshness | not checked | squash merges break ancestry; the gateway is judged by BR-071 (question 5) |

No new `status` value is proposed (step 23a: nothing to apply).

### Data Model Impact

None.

### Components to Create / Modify

**Modified**
- `scripts/sdk_guard.py` — evidence path, conditions of §3.1, docstring, output line.
- `tests/unit/test_br_037_sdk_guard.py` — rewritten around the v1 evidence.
- `Makefile` — `bench-sdk`.
- `README.md` — §"Upgrading the mcp SDK".
- `pyproject.toml` — comment of line 45 (no longer says the POC is read by the guard).
- `CHANGELOG.md` — entry under `[Unreleased]`.
- At closure: `business-rules/BR-037.md`, `business-rules/index.md` (rendered), `capability-map.md` (row "Garde de version du SDK mcp": witness is the bench's v1 target), `features/index.md` (rendered).

**Deleted**: none.

### Pitfalls Discovered

None. The guard needed one extra edge: `controls` entries are kept as a list, not a dict, so a failed control cannot be masked by a later duplicate id that passed (unit case `test_br_037_failed_control_not_masked_by_duplicate`).

## 10. Phases

### Phase 1 — Guard, target, procedure (one PR)

- [ ] `sdk_guard.py`, tests, `Makefile`, README, comments
- [ ] `make gate` ends `GATE: OK`; AC-01 to AC-06 played
- [ ] Closure: BR-037 v2 extracted, capability map, CHANGELOG, AC-07

## 11. Tests

### unit (`tests/unit/test_br_037_sdk_guard.py`)

| Test | BR / Concern | File |
|------|-------------|------|
| `test_br_037_conforming_repository_passes` | BR-037 | `test_br_037_sdk_guard.py` |
| `test_br_037_target_must_be_v1` (incl. POC evidence) | BR-037 | same |
| `test_br_037_dev_mode_refused`, `test_br_037_partial_run_refused` | BR-037 | same |
| `test_br_037_mcp_version_differs_from_bench`, `..._mcp_types_...`, `..._module_version_absent` | BR-037 | same |
| `test_br_037_one_test_absent_refused`, `test_br_037_failed_line_refused[T01..T16]` | BR-037 | same |
| `test_br_037_controls_absent_empty_failed_or_harness_missing` | BR-037 | same |
| `test_br_037_readings_and_freshness_ignored`, `test_br_037_poc_not_needed` | BR-037 (AC-03, AC-04) | same |
| `test_br_036_*` | BR-036 | unchanged |

Negative control (mutation, by the implementation): point `EVIDENCE` at `results-py-gateway.json` and `test_br_037_target_must_be_v1` plus the conforming case turn red; drop the controls loop and the control cases turn red.

## 12. Risks & Watchpoints

- **Functional risk**: a future bench change renames a control id; the guard then refuses loudly (accepted, m2).
- **Dependency risk**: the next real SDK bump costs a full v1 pass; the first one will measure the duration, which this framing could not.
- **Technical risk**: the guard stays blind to non-SDK regressions by design (accepted, m3).
- **Process risk**: `results-v1.json` is rewritten by any bench pass of any feature; a pass with `--only` or `--dev` writes `dev-*.json` (git-ignored), so it cannot replace the witness by accident.

## Stress-test

**Passe du 2026-10-31** — model:claude-sonnet-5-5, six axes.
**10 findings — 0 critical, 4 major, 6 minor ; 9 retenus, 1 écartés.**

Critiques :
- none.

Majors :
- **M1** · axe A · §BR-037 Rule — « conclusive replay » left open what makes it conclusive (which tests, which controls, dev or partial). Sort : corrigé — the Rule defines it; §3.1 enumerates six conditions, each with an AC-02 case.
- **M2** · axe B · §3.1 — an evidence with no `meta.target`, no `controls` key or a missing `module_versions` entry could crash instead of refusing. Sort : corrigé — §3.1 requires a named refusal; AC-02 has a case per missing key.
- **M3** · axe D · §Out of Scope — a reader would assume the POC and the bench's `py-gateway` target are removed. Sort : différé — §Out of Scope carries the deletion as a candidate ticket, with the eight rules that cite them.
- **M4** · axe E · §1 — `bench_commit` `56cb4e3` is not an ancestor of `main` (checked with `git merge-base --is-ancestor`, exit 1): a freshness check through git would fail on a sound evidence. Sort : corrigé — BR-037 v2 Clarifications and question 5 state the guard never reads `bench_commit`; AC-04 guards the absence with its presence twin.

Minors :
- **m1** · axe A · §3.1 condition 6 — an empty `controls` list would pass an "all passed" loop (a guard reporting only zero). Sort : corrigé — non-empty required and the two named harnesses asserted present (AC-02).
- **m2** · axe C · §3.1 condition 6 — naming two control ids ties the guard to the bench's labels. Sort : accepté — ADR-001 condition 2 names these controls; a rename fails loudly; risk in §12.
- **m3** · axe D · §Out of Scope — PyJWT, Starlette, Authlib, dev tools still climb without a replay. Sort : accepté — unchanged since F-004 (m11, H13); risk in §12.
- **m4** · axe F · README — the §"Upgrading the mcp SDK" sentence "deleting them fails the gate" contradicts BR-037 v2. Sort : corrigé — AC-05 reads the section.
- **m5** · axe B · §3.2 — `make bench-sdk` fails in the session (no `buildx`) with an unhelpful error. Sort : corrigé — the target prints its prerequisites, the README names the toolbox (AC-05).
- **m6** · axe C · §1 question 2 — requiring all 16 tests and all controls goes beyond the 11 isolation lines of v1. Sort : rejeté — ADR-001 condition 2 names T10 and the harnesses and the evidence is 16/16; the choice stays visible to the operator as question 2.

## 13. Changelog

| Date | Author | Change |
|------|--------|--------|
| 2026-10-31 | model:claude-sonnet-5-5 | Spec creation (framing of HULL-21), up to the freeze, excluded |
| 2026-10-31 | human:sstolz (freeze) | Freeze: `ready-for-dev`. Capabilities: extends Garde de version du SDK mcp. Merge: AI; Split: no, 1 PR. Q1 a, Q2 b, Q3 a, Q4 b, Q5 confirmed (no freshness check). |
| 2026-10-31 | model:claude-sonnet-5-5 | Implementation and closure: BR-037 extracted to v2; AC-01 to AC-07 played; no bench run |
| 2026-11-16 | Codex (implementation-coder, model:gpt-6-astra) | F-022 slice 2, freeze `10d166e` (retouch 2 `7aac9e6`): §3.2 substitutions only; inline rule clarifications without version where applicable. |
| 2026-11-24 | Claude (implementation, model:claude-opus-5-5) | F-025 §3.8 rewrite pass, freeze `1427c98`: private-forge `#N` written `HULL-N`, citation form only. |
