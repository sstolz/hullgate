"""Limits of the public listener (F-020): limit key, sliding windows, coalesced refusals.

- BR-301: the limit key of a client address is its IPv4 form (IPv4-mapped folded) or its IPv6
  /48; every address-keyed limit counts by it, the audit keeps the full address (BR-313).
- BR-305: a budget of N in T holds over every span of T ending now (a sliding log); a refused
  request does not count, BR-308 excepted, and the delay returned runs until the count falls
  below the value in force.
- BR-312: a limit refusal writes one `rate_limited.refused` entry per (surface, key) and period;
  later refusals of the period add to a count carried by the next entry as `suppressed`.
- BR-314: each store holds at most 10 000 keys, the least recently touched evicted, and drops a
  key once its window is empty. Nothing here is persisted.

One process serves the public listener (F-020 §9): the counts are exact there. The stores are
called from the event loop and from worker threads; each holds one lock for constant work (an
eviction pass is amortised), never any I/O.
"""

from __future__ import annotations

import contextvars
import ipaddress
import math
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field

from hullgate.admin.client_address import parse

type Clock = Callable[[], float]

MAX_KEYS = 10_000  # BR-314
IPV6_PREFIX = 48  # BR-301

# Budgets of the anonymous surface (F-020 §3.2): constants, no setting (§Out of Scope).
OAUTH_BUDGET = (60, 60.0)  # BR-306: requests in seconds, OAuth routes but `token`
REGISTER_BUDGET = (20, 3600.0)  # BR-307
CREDENTIAL_BUDGET = (30, 60.0)  # BR-308: unknown credentials, every one counted
CIMD_PER_KEY = 2  # BR-309: live fetches of one key
COALESCE_S = 60.0  # BR-312: one entry per (surface, key) and period

# The closed list of `surface` values of `rate_limited.refused` (BR-312).
TOKEN = "token"  # noqa: S105 - a surface name, not a secret
CONNECTION = "connection"
OAUTH = "oauth"
REGISTER = "register"
CREDENTIAL = "credential"
SURFACES = (TOKEN, CONNECTION, OAUTH, REGISTER, CREDENTIAL)

A_REFUSED = "rate_limited.refused"
RATE_LIMITED = "rate_limited"  # BR-116 v3: the code of a tool-call refusal

# The client address of the request being served on the public listener (BR-300), set once at
# the top of each request; worker threads inherit it (anyio copies the context). Read by the
# refusal entries only (BR-313).
CLIENT_ADDRESS: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "hullgate_client_address", default=None
)


def limit_key(address: str) -> str:
    """BR-301: `192.0.2.1` and `::ffff:192.0.2.1` → `192.0.2.1`; an IPv6 → its /48 prefix.

    A value that is not an IP address (a test client's peer) is its own key."""
    parsed = parse(address)
    if parsed is None:
        return address
    if isinstance(parsed, ipaddress.IPv6Address):
        return str(ipaddress.IPv6Network((parsed, IPV6_PREFIX), strict=False))
    return str(parsed)


def address_detail() -> dict[str, str]:
    """`{"address": …}` for a refusal entry of the public listener (BR-313), or nothing."""
    address = CLIENT_ADDRESS.get()
    return {"address": address} if address else {}


def whole_seconds(delay: float) -> int:
    """A delay rounded up to the second, never below one (BR-305)."""
    return max(1, math.ceil(delay - 1e-9))


@dataclass
class Window:
    """Sliding log of admitted requests per key (BR-305), bounded (BR-314).

    `admit(key, value)` admits and counts the request when fewer than `value` requests were
    counted in the last `span` seconds, and returns None; otherwise it returns the delay in
    seconds until the count falls below `value`, and counts nothing — unless `count_refused`
    (BR-308: every presented unknown credential counts)."""

    span: float
    clock: Clock = time.monotonic
    max_keys: int = MAX_KEYS
    _keys: OrderedDict[str, deque[float]] = field(default_factory=OrderedDict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def admit(self, key: str, value: int, *, count_refused: bool = False) -> float | None:
        with self._lock:
            now = self.clock()
            log = self._keys.get(key)
            if log is None:
                log = deque()
                self._keys[key] = log
            else:
                self._keys.move_to_end(key)
            horizon = now - self.span
            while log and log[0] <= horizon:
                log.popleft()
            count = len(log)
            delay: float | None = None
            if count >= value:
                # the (count − value + 1)th oldest counted request leaves the window
                delay = log[count - value] + self.span - now
            if delay is None or count_refused:
                log.append(now)
                # Only the last `value` entries decide (review 585, N2): a counted refusal drops
                # the oldest, so a key's log never outgrows its budget whatever the flood.
                while len(log) > value:
                    log.popleft()
            if not log:  # refused with an empty log: impossible for value ≥ 1, kept defensive
                del self._keys[key]
            self._trim(horizon)
            return delay

    def count(self, key: str) -> int:
        with self._lock:
            log = self._keys.get(key)
            if log is None:
                return 0
            horizon = self.clock() - self.span
            return sum(1 for at in log if at > horizon)

    def __len__(self) -> int:
        with self._lock:
            return len(self._keys)

    def _trim(self, horizon: float) -> None:
        """Least recently touched keys beyond `max_keys` evicted; stale keys at the old end
        dropped (BR-314). Under the lock."""
        while len(self._keys) > self.max_keys:
            self._keys.popitem(last=False)
        # the oldest-touched keys come first: drop them while their whole log is past the window
        for _ in range(2):
            if not self._keys:
                return
            oldest = next(iter(self._keys))
            log = self._keys[oldest]
            if log and log[-1] > horizon:
                return
            del self._keys[oldest]


@dataclass
class _Period:
    opened: float
    pending: int = 0


@dataclass
class Coalescer:
    """BR-312: which limit refusal writes an entry, and the count it carries.

    `refused(surface, key)` returns the number of refusals of that (surface, key) not written
    since the last entry when this refusal opens a new period (the caller writes the entry),
    else None (counted in memory, nothing written)."""

    period: float = COALESCE_S
    clock: Clock = time.monotonic
    max_keys: int = MAX_KEYS
    _periods: OrderedDict[tuple[str, str], _Period] = field(default_factory=OrderedDict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def refused(self, surface: str, key: str) -> int | None:
        with self._lock:
            now = self.clock()
            slot = (surface, key)
            current = self._periods.get(slot)
            if current is not None and now - current.opened < self.period:
                current.pending += 1
                self._periods.move_to_end(slot)
                return None
            suppressed = current.pending if current is not None else 0
            self._periods[slot] = _Period(now)
            self._periods.move_to_end(slot)
            while len(self._periods) > self.max_keys:
                self._periods.popitem(last=False)
            return suppressed

    def __len__(self) -> int:
        with self._lock:
            return len(self._periods)


@dataclass
class Slots:
    """BR-309: at most `per_key` live holders per key; a holder releases its slot itself."""

    per_key: int = CIMD_PER_KEY
    _held: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def acquire(self, key: str) -> bool:
        with self._lock:
            held = self._held.get(key, 0)
            if held >= self.per_key:
                return False
            self._held[key] = held + 1
            return True

    def release(self, key: str) -> None:
        with self._lock:
            held = self._held.get(key, 0) - 1
            if held > 0:
                self._held[key] = held
            else:
                self._held.pop(key, None)

    def held(self, key: str) -> int:
        with self._lock:
            return self._held.get(key, 0)


@dataclass
class Limits:
    """The in-memory limits of one public listener, built once (F-020 §9).

    Tool calls (BR-303, BR-304) count by token and by connection against a value read from the
    base at each call; the anonymous surface (BR-306 to BR-308) counts by limit key against the
    constants above. `clock` is monotonic; the tests inject theirs."""

    clock: Clock = time.monotonic
    max_keys: int = MAX_KEYS
    tokens: Window = field(init=False)
    connections: Window = field(init=False)
    oauth: Window = field(init=False)
    register: Window = field(init=False)
    credentials: Window = field(init=False)
    coalescer: Coalescer = field(init=False)

    def __post_init__(self) -> None:
        def window(span: float) -> Window:
            return Window(span, self.clock, self.max_keys)

        self.tokens = window(60.0)
        self.connections = window(60.0)
        self.oauth = window(OAUTH_BUDGET[1])
        self.register = window(REGISTER_BUDGET[1])
        self.credentials = window(CREDENTIAL_BUDGET[1])
        self.coalescer = Coalescer(COALESCE_S, self.clock, self.max_keys)

    def unknown_credential(self, key: str) -> bool:
        """BR-308: counts a presented unknown credential; True while it may still be written
        (fewer than 30 in the last 60 s before this one)."""
        value, _ = CREDENTIAL_BUDGET
        return self.credentials.admit(key, value, count_refused=True) is None


def refusal_entry(
    surface: str,
    key: str,
    suppressed: int,
    *,
    actor: str,
    node_id: str | None = None,
    path: str | None = None,
) -> dict[str, object]:
    """The fields of a `rate_limited.refused` entry (BR-312), for `audit.write`, with the address
    of the request that opens the period (BR-313)."""
    return {
        "actor": actor,
        "action": A_REFUSED,
        "result": "refused",
        "node_id": node_id,
        "path": path,
        "reason": RATE_LIMITED,
        "detail": {"surface": surface, "key": key, "suppressed": suppressed, **address_detail()},
    }
