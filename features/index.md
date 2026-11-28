# Feature Index

> Rendered by `cairn render-index features` from each spec's frontmatter.
> Do not edit it: correct the spec, then render the index (`skills/feature-index-maintenance.md`).

| ID | Title | Status | Domain | BRs | Spec | Ticket |
|----|-------|--------|--------|-----|------|--------|
| F-001 | Trial of ContextForge against the isolation acceptance run | implemented | isolation, evaluation | 11 | ./features/F-001-essai-contextforge.md | HULL-2 |
| F-002 | Gateway Go POC, judged by the transposed isolation acceptance run | implemented | isolation, gateway, poc | 11 | ./features/F-002-poc-go.md | HULL-5 |
| F-003 | Comparison Python POC, judged on the Go POC's bench | implemented | isolation, gateway, poc | 4 | ./features/F-003-poc-python.md | HULL-16 |
| F-004 | Foundation — image, quality gate, hosting and deployment on the production server | implemented | foundation, operations | 16 | ./features/F-004-fondation.md | HULL-6 |
| F-005 | Core — tree, tokens, inbound OAuth, authorization order | implemented | core, isolation, oauth | 30 | ./features/F-005-socle.md | HULL-7 |
| F-006 | Connection library and outbound OAuth | implemented | connections, oauth, core | 24 | ./features/F-006-connexions.md | HULL-8 |
| F-007 | Quality analysis tooling — coverage, CVE, SonarQube, open list | implemented | quality-gate, quality | 8 | ./features/F-007-outillage-qualite.md | HULL-40 |
| F-008 | Relay — aggregation, renaming, sessions and upstream MCP servers | implemented | relay, isolation, mcp | 25 | ./features/F-008-relais.md | HULL-9 |
| F-009 | SDK guard witness — switch to the bench's v1 target | implemented | foundation, quality-gate | 0 | ./features/F-009-sdk-guard-v1-witness.md | HULL-21 |
| F-010 | English contracts and product rename to hullgate — CLI, wire codes, stored values, audit values, ops lines | implemented | contracts, operations | 2 | ./features/F-010-english-contracts.md | HULL-56 |
| F-011 | Rename the names bound to data, by a fresh start — data volume, database file, key-check label, token prefix, OAuth application | implemented | contracts, operations | 0 | ./features/F-011-data-names-rename.md | HULL-66 |
| F-014 | Organisation URL scheme — /o/<id>/p/… and public identifier | implemented | core, oauth, operations | 2 | ./features/F-014-organisation-url.md | HULL-42 |
| F-015 | Action policy — tool modes, lists, argument rules, definition pinning, per-role response cap | implemented | policy, relay, isolation | 19 | ./features/F-015-action-policy.md | HULL-10 |
| F-016 | Admin console — SPA, JSON admin API, passkey sign-in, history and undo, YAML import | implemented | administration, core, operations | 35 | ./features/F-016-admin-console.md | HULL-11 |
| F-017 | Call events, retention, audit queries, analyses and the Statistics screen | ready-for-dev | audit, statistics, administration, operations | 18 | ./features/F-017-call-events-and-statistics.md | HULL-68 |
| F-019 | Threshold alerts over webhook and ntfy | ready-for-dev | observability, operations | 10 | ./features/F-019-threshold-alerts.md | HULL-69 |
| F-020 | Rate limiting and trusted client address on the public listener | implemented | operations, oauth, relay | 16 | ./features/F-020-rate-limiting-client-address.md | HULL-70 |
| F-021 | 1.0.0 delivery — one version source, a tagged cut, the tag running on the production server | ready-for-dev | operations, release | 5 | ./features/F-021-tagged-delivery.md | HULL-12 |
| F-022 | Public tree for 1.0.0 — no operator infrastructure, no secret, no workshop | implemented | publication, operations | 9 | ./features/F-022-public-release-cleanup.md | HULL-98 |
| F-023 | Setup guide links that land, and the admin guide aligned with F-016's operator protocols | implemented | administration, operations | 0 | ./features/F-023-console-guide-fixes.md | HULL-109 |
| F-024 | OAuth routes read their body under BR-316's deadline | implemented | oauth, relay | 0 | ./features/F-024-oauth-body-deadline.md | HULL-85 |
