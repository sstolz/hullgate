---
id: ADR-008
title: Organisation-scoped URLs under /o/<id>, with one authorization server per access point
status: accepted
date: 2026-11-15
drafted-by: ["model:claude-opus-5-5"]
validated-by: "human:sstolz"
---

# ADR-008 — Organisation-scoped URLs under /o/<id>, with one authorization server per access point

## Context and Problem Statement

ADR-002 placed one logical OAuth authorization server per access point, at `${PUBLIC_URL}/oauth/p/<path>`, the access point being `${PUBLIC_URL}/p/<path>/mcp`; neither carries the organisation, although the tree has an organisation root (BR-044). A hosted, multi-company offer is planned with isolation at the organisation level (HULL-42, HULL-67). ADR-002's own consequence makes the URL a contract: changing the path of a role, or the host, invalidates its OAuth authorizations and every client reconnects. The organisation must therefore be in every organisation-scoped URL before real clients rely on them (HULL-13), even though one organisation per instance stays true in 1.0.0. ADR-002's statements on the issuer's location and on "no additional data" no longer hold; its decision (one server per access point) does, and is restated here, since a supersession is never partial (`conventions/adr.md`).

Operator go of 2026-11-01 (HULL-42 comment 11163, arbitration Z2): a path segment with a short random base32 identifier, no wildcard certificate, no DNS provider; old URLs are not migrated, the operator reconnects the connector. Spec F-014.

## Considered Options

- **A subdomain per organisation**, `<id>.<domain>/p/<path>/mcp`: today's paths unchanged, browser origin isolation per organisation, a custom domain later through a CNAME. It needs a wildcard DNS record and a wildcard certificate (DNS-01, so DNS API access for Traefik) or one certificate per organisation, each name then published in Certificate Transparency logs. Discarded by the operator on 2026-11-01.
- **A path prefix**, `/o/<id>/…` (chosen): nothing to change in DNS, TLS or the Traefik `Host(...)` rule; one origin for every organisation.
- **The issuer outside the prefix**, `/oauth/o/<id>/p/<path>`: keeps the `/oauth` family together, but splits an organisation's routes over two prefixes, for a proxy rule, a cookie path or a v2 router alike.
- **Identifier kind**: a UUID (36 characters), a short random base32 string, or a name derived from the company (never, if anonymity matters).
- **Old URLs**: a redirect or an alias for a grace period, or a clean break.

## Decision Outcome

Chosen option: **every organisation-scoped route is the previous route with `/o/<id>` in front**; the RFC 8414 and RFC 9728 metadata keep their `/.well-known/<suffix>` insertion, the prefix following it:

| Surface | Form |
|---|---|
| Access point of a role, RFC 8707 audience and `resource` | `${PUBLIC_URL}/o/<id>/p/<path>/mcp` |
| Authorization server (issuer, JWT `iss`) | `${PUBLIC_URL}/o/<id>/oauth/p/<path>` |
| `authorize`, `consent`, `token`, `revoke`, `register` | `/o/<id>/oauth/p/<path>/<action>` |
| RFC 9728 metadata | `/.well-known/oauth-protected-resource/o/<id>/p/<path>/mcp` |
| RFC 8414 metadata | `/.well-known/oauth-authorization-server/o/<id>/oauth/p/<path>` |
| Consent cookie `Path` | `/o/<id>/oauth/p/<path>/consent` |

`/healthz` and `/readyz` stay at the root: they belong to the instance. `/admin` (ADR-006) is an instance route on the admin listener, outside `/o/<id>`.

- **One logical authorization server per access point**, as ADR-002 decided: a single code base, issuer and role being request parameters; a code, an access token or a renewal carries the role of its server, and any other server or access point refuses it.
- **The identifier** is 12 characters of the lowercase RFC 4648 base32 alphabet (60 random bits), drawn once by the schema migration that adds it, stored on the organisation row, derived from nothing, and permanent: the store refuses any update of a value once set (BR-160). It is public, an anonymous name and not a credential.
- **The grammar reads the request's raw path** (`raw_path`), never the server's percent-decoded `path`; an organisation-scoped route answers only under the instance's exact identifier, checked inside step 1 of the authorization order, and every other form (the old paths included) answers 404 with no redirect, no alias and no hint (BR-161). A foreign well-formed identifier answers as an unknown role, byte for byte.
- **The host rule**: the public host (`MCP_HOST`, `PUBLIC_URL`) is part of every URL, so a host change is a path change under ADR-002's consequence: every client reconnects. The prefix lets HULL-67 add organisations without moving the URL of the existing one; it does not make the host movable.

### Consequences

- Good: the URL of a role never has to change for multi-organisation; HULL-67 adds organisations under their own prefix.
- Good: everything of an organisation sits under one prefix, for a proxy, a cookie path or a future router.
- Good: no DNS, certificate or Traefik change; no wildcard certificate, no name in Certificate Transparency logs.
- Bad: one stored value more (ADR-002's "no additional data" no longer holds): the identifier lives in the database, so a backup taken before the migration that created it, restored under the new code, draws a new identifier and changes every URL. The data is disposable in 1.0.0; rebuild rather than restore such a backup.
- Bad: the change itself is a reconnection of every client, once, before HULL-13; tokens issued before it are refused by construction (their audience is a URL no longer served), without any revocation.
- Bad, inherited from ADR-002: changing a role's path, the host or `PUBLIC_URL` invalidates its OAuth authorizations.
- Neutral: the identifier only hides an organisation from a scanner: 60 random bits behind a byte-identical 404 cost 2^59 requests on average for one hit; no rate limit or longer identifier is needed.
- Neutral: one origin for every organisation: cookies, CSP and the consent page are shared by origin, which a subdomain would have separated.

### Confirmation

`tests/unit/test_br_160_organisation_id.py`; `tests/integration/test_br_161_url_refusal.py`; `tests/integration/test_br_056_discovery.py`; isolation bench, target `v1`, replayed on the new URLs (`evidence/results-v1.json`).

## More Information

F-014 (BR-044 v2, BR-056 v2, BR-068 v2, BR-070 v5, BR-160, BR-161); supersedes ADR-002; HULL-42, HULL-67 (multi-organisation), HULL-13 (first real client); code `src/hullgate/routes.py`, `src/hullgate/migrations.py` (migration 10).
