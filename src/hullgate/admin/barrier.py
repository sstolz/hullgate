"""Barrier-only WebAuthn ceremonies. No console cookie or admin session is issued here."""

from __future__ import annotations

import sqlite3
from typing import Any
from urllib.parse import urlsplit

from hullgate import audit
from hullgate.admin import accounts, auth
from hullgate.config import AdminOrigin, is_loopback_name
from hullgate.limits import limit_key


def origin(public_url: str, admin: AdminOrigin | None) -> AdminOrigin | None:
    parts = urlsplit(public_url)
    host = parts.hostname or ""
    if admin is None or parts.scheme != "https" or is_loopback_name(admin.rp_id):
        return None
    if is_loopback_name(host) or not (host == admin.rp_id or host.endswith("." + admin.rp_id)):
        return None
    port = parts.port or 443
    canonical = f"https://{host}" + ("" if port == 443 else f":{port}")
    return AdminOrigin(canonical, host, port, True, admin.rp_id)


def usable(conn: sqlite3.Connection, configured: AdminOrigin | None) -> bool:
    return (
        configured is not None
        and conn.execute(
            "SELECT 1 FROM passkey p JOIN administrator a ON a.id = p.administrator_id"
            " WHERE a.state = 'active' LIMIT 1"
        ).fetchone()
        is not None
    )


def failed(
    conn: sqlite3.Connection, address: str, now: int, reason: str, *, limited: bool = False
) -> None:
    if not limited:
        accounts.record_failure(conn, address, now)
    audit.write(
        conn,
        actor=audit.ANONYMOUS,
        action="oauth.consent",
        result="refused",
        reason=reason,
        detail={"factor": "passkey", "address": address},
        now=now,
    )


def verify(
    conn: sqlite3.Connection,
    credential: dict[str, Any],
    configured: AdminOrigin,
    request_digest: str,
    address: str,
    now: int,
) -> str:
    challenge = auth.take_challenge(conn, credential, "barrier", now)
    if (
        challenge is None
        or challenge.enrolment_digest != request_digest
        or challenge.address != limit_key(address)
    ):
        raise auth.CeremonyFailed("unknown_challenge")
    administrator, _ = auth.verify_assertion(conn, credential, challenge, configured, now)
    return administrator.id


# Constant script, authorised by its SHA-256 CSP hash. Values come from the existing form;
# credentials and the request reference travel only in POST bodies. The final native form
# submission follows the OAuth redirect without fetching a cross-origin callback.
SCRIPT = """const button = document.getElementById('passkey');
button.addEventListener('click', async () => {
  const form = button.form;
  const error = document.getElementById('passkey-error');
  button.disabled = true;
  try {
    const body = new URLSearchParams({req: form.elements.req.value, decision: 'passkey-options'});
    const response = await fetch(form.action, {method: 'POST', body, credentials: 'same-origin'});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error_description || 'Passkey refused. Try again.');
    const decode = value => Uint8Array.from(
      atob(value.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0));
    const encode = value => btoa(String.fromCharCode(...new Uint8Array(value)))
      .replace(/=/g, '').replace(/\\+/g, '-').replace(/\\//g, '_');
    const options = result.publicKey;
    options.challenge = decode(options.challenge);
    options.allowCredentials = options.allowCredentials.map(c => ({...c, id: decode(c.id)}));
    const key = await navigator.credentials.get({publicKey: options});
    const r = key.response;
    form.elements.credential.value = JSON.stringify({
      id: key.id, rawId: encode(key.rawId), type: key.type,
      response: {authenticatorData: encode(r.authenticatorData),
        clientDataJSON: encode(r.clientDataJSON),
        signature: encode(r.signature), userHandle: r.userHandle ? encode(r.userHandle) : null}});
    const decision = document.createElement('input');
    decision.type = 'hidden'; decision.name = 'decision'; decision.value = 'passkey';
    form.appendChild(decision);
    form.submit();
  } catch (e) {
    error.textContent = e.message || 'Passkey refused. Try again.'; button.disabled = false;
  }
});"""
