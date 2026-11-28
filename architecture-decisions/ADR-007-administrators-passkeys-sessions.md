---
id: ADR-007
title: Console administrators are host-bootstrapped accounts that sign in with a passkey only, on short server-side sessions
status: accepted
date: 2026-11-14
drafted-by: ["model:claude-opus-5-5"]
validated-by: "human:sstolz"
---

# ADR-007 — Console administrators are host-bootstrapped accounts that sign in with a passkey only, on short server-side sessions

## Context and Problem Statement

Before F-016 the only human authentication was the TOTP barrier of the OAuth consent page
(ADR-003), and the only administration path was `docker exec` into the container. A console over
the data of every account opens the first authenticated human surface; the PDF's threat table
(p. 17) requires that a gateway exposed on the internet cannot be taken over with less than a
passkey. On the production server the admin port answers neighbouring containers on the Traefik network, so no
network position can count as authentication (BR-068).

Decided at the F-016 freeze (§9: network position as trust, `Secure` on the tunnel, discoverable
credentials, session durations, fresh re-authentication, e-mail recovery dropped), recorded at
lot A's close. Completes ADR-003, which announced the passkey.

## Considered Options

- **Passkey only, accounts bootstrapped from the host** — WebAuthn with user verification,
  discoverable credentials, the first account and its enrolment link created by
  `hullgate admin bootstrap` in the container; server-side sessions stored as digests.
- **Password plus TOTP** — the barrier's factor reused for the console.
- **Local-address exemption** — no authentication when `ADMIN_URL` is a loopback URL (PDF p. 17).
- **Open first-run setup page** — the first visitor creates the administrator.

## Decision Outcome

Chosen: **passkey only, accounts bootstrapped from the host**, because a passkey is the only factor
that resists phishing and replay at once, discoverable credentials leave nothing to type and no
account to enumerate, and the host is the one place an attacker reaching the port does not hold:
an open setup page would hand the gateway to whoever reaches it first, and a loopback exemption is
an open door on the production server, where `127.0.0.1:14791` coexists with a port open to Traefik's neighbours.

Sessions are a random 256-bit value in an `HttpOnly`, `SameSite=Strict`, `Path=/admin` cookie,
stored as its SHA-256 digest; 30 minutes idle and 8 hours absolute; the CSRF token is derived from
the session value, so the base keeps digests only. Security acts (passkeys, allow-list) ask for a
fresh assertion. Lost access returns through the host (BR-228, lot E), never by e-mail.

### Consequences

- Good, because a stolen database yields no usable credential: public keys and digests only.
- Good, because the host, which the operator holds, is the root of every recovery: bootstrap,
  attempt window and allow-list are cleared from it (BR-201, BR-207, BR-235).
- Bad, because a passkey is bound to its relying party id: an operator who enrols on `localhost`
  re-enrols when a public admin name opens (Q3), and `ADMIN_URL` can no longer be an IP address,
  which moved the production server's value to `http://localhost:14791`.
- Bad, because losing every passkey needs host access; the runbook has the operator enrol a second
  one at the first sign-in.
- Neutral, because `Secure` is exempted for a loopback `ADMIN_URL` only (browsers disagree on it over
  `http://localhost`); any other `http` origin is refused at startup.

Revisit if several administrators with delegated recovery arrive (1.1.0), or if WebAuthn's
binding to the RP id becomes an obstacle to the public admin name.

### Confirmation

`tests/integration/test_br_201_bootstrap.py` to `test_br_210_fresh.py`, `test_br_235` cases in
`test_br_209_allowlist.py`, and `tests/browser/test_br_202_sign_in.py` (Chromium's virtual
authenticator against the image).

## More Information

F-016 §4 BR-200 to BR-210, BR-234, BR-235; §9 Architecture Decisions. ADR-003 (two families of
tokens and the TOTP barrier). py_webauthn 3.0.1 (BSD-3-Clause) verifies the ceremonies.
