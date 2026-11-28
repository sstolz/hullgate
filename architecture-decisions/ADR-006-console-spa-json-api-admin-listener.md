---
id: ADR-006
title: The admin console is a Svelte SPA over a JSON API, served by the admin listener under /admin
status: accepted
date: 2026-11-14
drafted-by: ["model:claude-opus-5-5"]
validated-by: "human:sstolz"
---

# ADR-006 — The admin console is a Svelte SPA over a JSON API, served by the admin listener under /admin

## Context and Problem Statement

F-016 gives the operator a browser console over the rules the CLI already enforces. The prototype
the operator validated on 2026-10-28 (HULL-18) is a rich interactive client: three collapsible columns,
drag and drop of connections onto roles, a map view, a live rights preview. The specification of
the product (PDF p. 21) suggested server-rendered HTMX. The console also has to live with a strict
Content-Security-Policy, stay out of the public listener (BR-068), add no Node to the runtime image
(BR-212), and leave every decision to the server (BR-211).

Decision taken at the F-016 freeze (§9, `technical-autonomy: decide`), recorded at lot A's close.

## Considered Options

- **SPA + JSON API** — a static Svelte 5 bundle under `/admin`, built at image build time, calling
  `/admin/api/v1` on the admin listener.
- **Server-rendered HTMX** (PDF p. 21) — HTML fragments rendered by Starlette, on the same listener.
- **Console on the public listener** — one origin with the MCP endpoints, `PathPrefix(/admin)`.

## Decision Outcome

Chosen: **SPA + JSON API on the admin listener**, because the validated prototype's interactions
(drag and drop, map, live preview) would have to be rewritten as fragments under HTMX, while a JSON
API gives the CLI and the console one set of operations and refusals to share (BR-211), and the
admin listener keeps its own port, routed independently (PDF p. 6), with the public listener's 404
on `/admin` unchanged.

### Consequences

- Good, because the API is the single place where an admin act is decided; parity with the CLI is
  testable verb by verb (`test_br_211_parity.py`), and later screens (F-017's Statistics) add one
  route group and one tab entry.
- Good, because the bundle is static and identical for everyone: it holds no data, needs no
  session, and runs under `default-src 'self'` with no inline script or style.
- Bad, because the project gains a second toolchain (Node 26, npm, Vite, Svelte, Vitest, ESLint,
  Playwright) with a much larger dependency tree than `uv.lock`; it is held by a committed lockfile,
  a build stage on `$BUILDPLATFORM` whose output only is copied, the gate's `web` step and
  `qa-web-audit` (BR-029 v2, BR-035 v2, BR-231).
- Bad, because a browser category of tests (`make test-browser`) is needed for what unit tests
  cannot prove (WebAuthn ceremonies, CSP, keyboard paths), outside the gate.
- Neutral, because the CSP forbids inline styles: dynamic styles go through CSSOM.

Revisit if the console's interactions shrink to forms and tables, or if Node's supply chain cost
outweighs the prototype's interactivity.

### Confirmation

`tests/container/test_br_212_bundle.py` (no Node in the runtime image, bundle served under its CSP),
`tests/browser/test_br_212_csp.py` (no external request, no CSP violation),
`tests/integration/test_br_068_no_admin_routes.py` (route sweep), the gate's `web` step.

## More Information

F-016 §3.1, §3.15, §9 (Architecture Decisions: UI stack, Which listener), BR-068 v2, BR-211, BR-212.
Complements ADR-001 (Python stack of the gateway): the gateway stays Python; Node exists only at
build and test time.
