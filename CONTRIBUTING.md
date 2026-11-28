# Contributing to hullgate

Thank you for your interest. This page says how a change reaches `main`, what it must pass and
how the project is run.

## Bugs and requests

Report a bug or ask for a feature in a [GitHub issue](https://github.com/sstolz/hullgate/issues).
Say which version you run, what you did, what you expected and what happened.

A vulnerability never goes in an issue: follow [SECURITY.md](SECURITY.md).

## Proposing a change

1. Fork the repository and create a branch from `main`.
2. Make the change, with its tests (see below).
3. Open a pull request toward `main`. Describe the change, why it is needed and how you tested
   it; cite the issue it answers as `#N`.

A pull request merges only when:

- the CI check is green: the workflow under `.github/workflows/` runs `make gate`, then
  `make qa`, on every push and pull request. A first-time contributor's workflow runs once a
  maintainer has approved it;
- it has had the cairn review: the maintainer's automated review, published on the pull
  request as a comment review. GitHub requires no approval, so the review is a project rule,
  not a forge setting.

Pull requests are squash-merged: one commit per pull request on `main`. Nobody pushes to `main`
directly, the maintainer included. Version tags are cut by the maintainer.

hullgate is developed with AI assistance, and its changes go through an automated review.

## What a change must pass

Install `uv` (the version pinned by `[tool.uv] required-version` in `pyproject.toml`), GNU
`make` and Docker. Then run, before you open the pull request:

- `make gate` — the quality gate: lockfile, formatting, lint, `mypy --strict`, the `unit` and
  `integration` tests (`tests/unit/`, `tests/integration/`), the SDK guard and the console's web
  checks. Its last line must be `GATE: OK`.
- `make qa` — the analysis tools outside the gate. Its `blocking` tools decide: `qa-audit`
  (known vulnerabilities of the runtime lock) and `qa-secrets` (secret scanner); the others are
  informative. Its last line must be `QA: OK`.
- `make test-container` — the `container` tests (`tests/container/`), when the change touches
  the `Dockerfile`, a Compose file (`compose*.yaml`), `deploy/` or the `Makefile`.
- `make test-browser` — the `browser` tests (`tests/browser/`, Playwright and Chromium against
  the application image), when the change touches `web/` or `src/hullgate/admin/`.

These are the four test categories of the project: `unit`, `integration`, `container` and
`browser`. A test named `test_br_NNN_*` checks business rule BR-NNN.

## How the project is run

- **Specifications** — each feature has a specification under `features/` (`F-NNN-*.md`),
  indexed in `features/index.md`.
- **Business rules** — each rule the code enforces lives under `business-rules/`
  (`BR-NNN.md`), indexed in `business-rules/index.md`, with the tests that check it.
- **Architecture decisions** — recorded under `architecture-decisions/` (`ADR-NNN-*.md`).

The maintainer keeps these documents with cairn, a specification workflow. Contributors need
not install it: a pull request is judged by the CI check and the review. When a change alters a
behaviour a rule describes, say so in the pull request; the maintainer updates the specification
and the rule.

## Identifiers

- `#N` names a GitHub issue or pull request of this repository, except in the tree of tag
  `v1.0.0`, which still writes the private tracker's tickets that way.
- `HULL-NN` names a ticket or pull request of the project's private tracker before publication:
  a historical reference, never a GitHub issue number.
- `GH-N`, in a branch name or in a field cairn composes (a specification's `ticket:` key, the
  `Ticket` column of `features/index.md`, a rule's `introduced-by`, a derogation's `ref`, the
  first cell of a rule's Traceability row, the ticket in a rule's Versioning reason), names
  GitHub issue #N.

## Licence of contributions

hullgate is released under the [MIT licence](LICENSE). By contributing, you agree that your
contribution is licensed under the same terms (inbound = outbound). There is no contributor
licence agreement and no sign-off to add to commits.
