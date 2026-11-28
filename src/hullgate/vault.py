"""Vault of the gateway: the database and the signing key, a single availability predicate.

BR-033 v2 and BR-064: `/readyz`, the access point and every OAuth route evaluate this predicate
on every request, without cache — database open, writable, write lock obtained, the code's schema,
then signing key loaded (`key_unavailable`).
"""

from __future__ import annotations

from dataclasses import dataclass

from hullgate.db import Database, Readiness
from hullgate.keys import SigningKey, TotpSecret


@dataclass(frozen=True, slots=True)
class Vault:
    db: Database
    key: SigningKey | None
    totp: TotpSecret | None = None

    def readiness(self) -> Readiness:
        """Blocking call: to run outside the loop."""
        state = self.db.readiness()
        if not state.ok:
            return state
        if self.key is None or not self.key.available():
            return Readiness(False, "key_unavailable")
        return state
