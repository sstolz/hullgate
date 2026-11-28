---
id: F-024
title: OAuth routes read their body under BR-316's deadline
ticket: HULL-85
status: implemented
closed: 2026-11-18
domain: [oauth, relay]
br-introduced: []
br-touched: [BR-316]
decisions: []
drafted-by: ["model:claude-opus-5-5"]
validated-by: "human:sstolz"
capabilities:
  extends: ["Per-access-point authorization server"]
  introduces: []
---

# F-024 · OAuth body deadline

## Metadata

| Field | Value |
|-------|-------|
| **Priority** | — (milestone 1.0.0; HULL-85 stays open until this spec ships, operator option (a) of its acceptance raise 1) |
| **Branch** | `fix/HULL-85-oauth-body-deadline` |
| **Last updated** | 2026-11-18 |

## 1. Context & Objective

### Current State

Read on `main` at `50bd49e`:

- **HULL-85.** BR-316 (`strict`) says that on both listeners a request body not received in full within 30 seconds gets 408 `request_timeout` and the connection is closed. PR #96 (`fd418ba`) put the shared reader `read_body` (`src/hullgate/body.py`) on the admin API and the MCP access point; the OAuth routes of the public listener (`authorize`, `consent`, `token`, `revoke`, `register`, POST) still read their body with `request.stream()`, bounded in size (64 KiB, `BODY_MAX`) but not in time (`src/hullgate/oauth.py:368-376`). The acceptance probe (`.cairn/runs/acts/HULL-85-acceptance-1-probe.py`, deadline patched to 1 s) got no answer in 6 s on a stalled `POST …/oauth/p/<role>/token`, while the access point of the same listener answered 408. The acceptance stopped on that gap (raise 1); the operator chose option (a): keep BR-316 as written and bring the OAuth routes under it.

Sources: `.cairn/runs/acts/HULL-85-acceptance-1.md` (raise 1) and its probe; BR-316; F-016 §3.15.

### Objective

No route of the public listener keeps a connection open while its client stalls the body.

### Expected Impact

- BR-316 holds to its letter on both listeners, so HULL-85 can be accepted; an unauthenticated OAuth route no longer parks a connection as long as a client stalls.

### Open Product Questions

None. The scoping choices are in §9 Architecture Decisions.

## 2. Scope

**Included**

- The OAuth routes' body deadline (§3.3): their answer to a stalled body, its audit entry, one stalled-body test per route.

## Out of Scope

- **HULL-85's other review items** (N4 cost of the double access decision, N5 allow-list checked before the body, N7 purge answering 503): its acceptance raise 2, not this spec.
- **The OAuth routes' 413 on `authorize` and `consent`**, answered today as a JSON body where those routes' other refusals are HTML pages: unchanged, the 413 keeps its current form.
- **What Traefik bounds in front of the public listener** (unmeasured, HULL-85's note). The gateway's deadline does not depend on it.

## 3. Functional Spec

### 3.3 The OAuth routes' body deadline (public listener)

The five OAuth routes that take a POST body — `authorize`, `consent`, `token`, `revoke`, `register` — read it with the shared reader under BR-316's deadline (30 s, one monotonic deadline for the whole body) and BR-311's bound (64 KiB for these routes, `BODY_MAX` of `oauth.py`, unchanged). What precedes the body is unchanged: the method check (405), the role and instance (404, 503) answer before any read.

| Outcome of the read | Answer | Audit |
|---|---|---|
| complete within the deadline | the route, as today | as today |
| not complete at the deadline, `token`, `revoke`, `register` | `408`, `Connection: close`, JSON `{"error": "invalid_request", "error_description": "request_timeout"}` | one entry, below |
| not complete at the deadline, `authorize`, `consent` | `408`, `Connection: close`, the routes' HTML error page saying the request did not arrive in time | one entry, below |
| over 64 KiB (announced or read) | `413` as today | as today (none) |
| client disconnected before the end | nothing sent | none |

- **The JSON shape** is RFC 6749 §5.2's, which RFC 7009 §2.2.1 (`revoke`) and RFC 7591 §3.2.2 (`register`) reuse: `error` takes a registered value, `invalid_request`, as the 413 of these routes already does; `request_timeout`, BR-316's code, is the `error_description`. A client reads the status and the registered code; an operator reads BR-316's code.
- **The browser routes** answer the resource owner directly: their body, which carries the client and its redirect URI, was not received, so no redirection to a client is possible (RFC 6749 §4.1.2.1).
- **The audit entry**: action of the route (`oauth.authorize`, `oauth.consent`, `oauth.token`, `oauth.revoke`, `oauth.register`), `result: refused`, `reason: request_timeout`, actor `anonymous` (the body that would identify a client was not read; a `token` request's `Authorization: Basic` header names a client but is not authenticated before the body), the role's path, detail `{"timeout_seconds": 30}` with the client address (BR-313). Written as the routes' other refusals are: the 408 is served even if the entry fails (BR-066).
- **Budgets.** A 408 is not a failed credential (BR-308); what F-020 counts at a request's entry is unchanged.
- **GET** on `authorize` and `consent` carries no body and is unchanged.
- **The re-check** of BR-316's second clause has nothing to re-check here: no OAuth route decides an authorisation before its body.

#### Visual States

No new state: the HTML error page is the routes' existing one.

## 4. Business Rules

No rule introduced.

### BR-316 · A request body is read within 30 seconds, then the caller is checked again *(clarification, rule unchanged)*

*`behavioral` · `strict` · Candidate state: `verified` — `tests/integration/test_hull_85_body_deadline.py` (the five OAuth routes added).*

**Rule** — unchanged (v1): *On both listeners, a request body not received in full within 30 seconds of its first byte being awaited gets 408 `request_timeout` and the connection is closed; an authorisation decided before the body is checked again before the request acts.*

**Clarification added**

- Public listener, OAuth routes (`authorize`, `consent`, `token`, `revoke`, `register`, POST): the 408 carries `request_timeout` as the `error_description` of an RFC 6749 `invalid_request` on `token`, `revoke` and `register`, and as the HTML error page on `authorize` and `consent`; it is audited under the route's action, reason `request_timeout`, actor `anonymous`. No authorisation is decided before their body, so none is checked again.

### Rules touched

- **BR-316**, clarification without version (above): the population of "both listeners" named for the OAuth routes; rule text, kind, enforcement unchanged.
- Applied, unchanged: **BR-311** (the OAuth routes' 64 KiB bound and their 413), **BR-313** (a refusal's client address), **BR-066** (the refusal is served even if its audit fails).

**Overlap scan** (`business-rules/index.md`, 220 lines; filter: body, deadline, timeout, 408, request, oauth) — BR-316 — the rule this spec completes on its letter: clarification. BR-311 — size bound, applied unchanged. BR-131 — closed vocabularies: `request_timeout` is already a reason (access point), `invalid_request` an RFC value. BR-313, BR-066 — refusal audit form, applied. No new rule.

## 5. Acceptance Criteria

- [x] **AC-11** (BR-316) A stalled POST body (deadline patched to 1 s, `Content-Length: 100`, one byte sent) on `token`, `revoke` and `register` gets `408`, `Connection: close`, a JSON body equal to `{"error": "invalid_request", "error_description": "request_timeout"}`, and the connection closed by the server (the next `recv` on the socket returns EOF within 2 s) — one case per route. Instrument: `tests/integration/test_hull_85_body_deadline.py`.
- [x] **AC-12** (BR-316) The same stalled body on `authorize` and `consent` gets `408`, `Connection: close`, a `text/html` body, and the connection closed (EOF as in AC-11) — one case per route. Same instrument.
- [x] **AC-13** (BR-316) Each of the five cases of AC-11 and AC-12 writes exactly one audit entry: action `oauth.<route>`, `result` `refused`, `reason` `request_timeout`, actor `anonymous`, detail `timeout_seconds` and the client address. Same instrument.
- [x] **AC-14** (BR-311, BR-316) On the OAuth routes, a body complete within the deadline is served as before (the existing OAuth suites pass unchanged), a body over 64 KiB still gets `413` `invalid_request`, and a client disconnecting mid-body gets no answer and writes no entry. Same instrument; the existing OAuth suites (`tests/integration/test_br_057_059_oauth_flow.py`, `test_br_097_endpoints.py`, `test_br_095_revoke.py`, `test_br_061_clients.py`) pass unchanged under `make gate`.

## 6. Diagrams

| Type | Included | Reason |
|---|---|---|
| Userflow | no | no user-facing flow changes; §3.3's table gives the outcomes |
| Sequence | no | the read is one layer (the dispatcher and its audit), its outcomes in §3.3's table |
| State machine | no | no new state |
| ER / Class | no | no schema change |

## 7. States & Captures

- **Package-status**: absent — no design package.

## 8. Design & Prototypes

No design assets.

## 9. Implementation Notes

### Dependencies (prerequisite features)

- HULL-85 — body deadline, PR #96 merged at `fd418ba` (ticket open, acceptance stopped on this gap): `read_body` and the access point's 408 are this spec's base.

### Technical Constraints

- BR-316's 30 seconds are a constant; tests patch `hullgate.body.BODY_READ_TIMEOUT`, as the acceptance probe does.

### Architecture Decisions

| Question | Decision | Rationale |
|---|---|---|
| D8 · Carrier of the OAuth deadline | F-024, a spec of its own on HULL-85, the ticket kept open until it ships | The operator's option (a) of HULL-85's acceptance raise 1; first drafted as F-023 slice 2, moved to F-024 by the orchestrator on 2026-11-18 (F-023 drafting report, raise 7, option (b)). |
| D9 · 408 body on `token`, `revoke`, `register` | RFC 6749 shape, `invalid_request` with `request_timeout` as description | A registered `error` value for OAuth clients (as the routes' 413 already does), BR-316's code still in the body. Discarded: `{"error": "request_timeout"}` (the access point's form), an unregistered value an RFC client cannot map. |
| D10 · 408 on `authorize`, `consent` | the routes' HTML error page | Browser routes answer the resource owner; no redirect URI is known without the body (RFC 6749 §4.1.2.1). |
| D11 · Audit action | the route's own action, reason `request_timeout` | The routes' refusals are already audited per action; a new `oauth.body_refused` action would split one route's refusals over two actions. Discarded: mirroring `access.body_refused`. |
| D12 · BR-316 version or clarification | Clarification, rule text unchanged | The rule already covers both listeners; only the population and the OAuth answer's form needed naming (operator's option (a)). |

### Data Model Impact

- Migration: no.

### Components to Create / Modify

**Modified**
- `src/hullgate/oauth.py` — the dispatcher reads POST bodies with `read_body`.
- `tests/integration/test_hull_85_body_deadline.py` — five OAuth cases.
- `business-rules/BR-316.md` — clarification at close-feature.

### Pitfalls Discovered

- The dispatcher's 408 goes through `_refused` off the event loop (`to_thread`), like the routes' other refusals, so a failing audit write still serves the 408 (BR-066); `test_oauth_timeout_survives_failed_audit` pins it.
- A body announced over 64 KiB is refused before any read (`read_body` checks `Content-Length` first); a chunked one stops at the first chunk past the bound. Both keep the routes' existing 413 and write no entry.

## 10. Phases

Feature delivered in one phase.

### Phase 1 — OAuth body deadline

- [x] §3.3 delivered; AC-11 to AC-14 met.
- [x] Tests pass (`make gate`). HULL-85's acceptance is replayed after this spec merges.

## 11. Tests

### integration

| Test | BR / Concern | File |
|---|---|---|
| stalled body per OAuth route, `Content-Length` and chunked: status, body, connection closed, audit (`test_oauth_stalled_body_gets_408_eof_and_one_audit`) | BR-316 | `tests/integration/test_hull_85_body_deadline.py` |
| OAuth body at the 64 KiB bound reaches the route, 413 over it, disconnect sends and audits nothing, pre-body refusals read nothing | BR-311, BR-316 | same, plus the existing OAuth suites named in AC-14 |
| the 408 is served when its audit write fails | BR-066, BR-316 | same (`test_oauth_timeout_survives_failed_audit`) |

## 12. Risks & Watchpoints

- **OAuth clients on a slow link.** A 64 KiB form body has 30 s; a legitimate client stalled that long gets 408 where it waited forever before. Accepted: the access point already behaves so.
- **Packaging findings settled by the split.** C2 and E2 of the pass below were accepted while this text was F-023's slice 2; the move to a spec of its own on HULL-85 alone answers both.

## Stress-test

**Passe du 2026-11-18** — model:claude-opus-5-5 (the drafting executant, on the orchestrator's instruction), six axes, on the delta of slice 2 (§1, §2, §3.3, BR-316, AC-11 to AC-14, §9 D8 to D12, §10, §12).
**6 findings — 0 critical, 2 major, 4 minor ; 6 retenus, 0 écartés.**

Majors :
- **A4** · axe A · AC-11, AC-12 — "the connection closed by the server" had no instrument. Sort : corrigé — the next `recv` returns EOF within 2 s. Clause question re-asked: status, header, body, EOF and audit are each read by the test.
- **E2** · axe E · frontmatter `ticket` — the template's key holds one identifier; `HULL-109, HULL-85` renders and passes `cairn check`, but a tool reading one identifier reads the string whole. Sort : accepté — kept as the orchestrator asked; §12; §10 names each slice's ticket.

Minors :
- **B3** · axe B · §3.3 — a `token` request's Basic header names a client before the body; actor `anonymous` looked wrong. Sort : corrigé — the header is not authenticated before the body; stated.
- **B4** · axe B · §3.3 — whether a 408 counts against the OAuth budgets. Sort : corrigé — not a failed credential (BR-308); entry counting unchanged.
- **F2** · axe F · §3.3 — GET on `authorize` and `consent` was not said untouched. Sort : corrigé — stated.
- **C2** · axe C · §2, §10 — slice 2 shares no code or rule with slice 1. Sort : accepté — the orchestrator's packaging (D8), one PR per slice; §12; the split stays open at the freeze.

## 13. Changelog

| Date | Author | Change |
|------|--------|--------|
| 2026-11-18 | Claude (product, model:claude-opus-5-5) | Drafted as F-023 slice 2 (HULL-85, operator option (a) relayed by the orchestrator), stress-tested (6 findings applied) |
| 2026-11-18 | Claude (product, model:claude-opus-5-5) | Moved to F-024 on the orchestrator's decision (F-023 raise 7, option (b)); text unchanged but for the carrier (frontmatter, §1 lead, §2, §6, §9 D8, §10, §12) |
| 2026-11-18 | human:sstolz (operator) | Freeze: operator go on the text shown in full. |
| 2026-11-18 | Claude (implementation, model:claude-opus-5-5) | Close: status `implemented`, `capabilities` declared (INV-047), §9 pitfalls, §10 ticked, §11 tests named; BR-316 clarification extracted (no version) |
| 2026-11-18 | Claude (product, model:claude-fable-5-2) | Acceptance (HULL-85): AC-11 to AC-14 met, §5 ticked; replayed on `main` `8a4a362` (comment 13486) |
