"""BR-088, BR-131 v2 (F-011 AC-01): the key-check label is a fixed constant, pinned by its digest.

The stored key check is an HMAC of a constant label under the connections key; a changed label
would make every stored key read `key_different`. The check of a fixed 32-byte key is pinned to a
literal digest computed once from the label `hullgate/connections/key-check/v1` (F-011 §3.1), and
the label is assigned once in `cipher.py`.
"""

from __future__ import annotations

from pathlib import Path

from hullgate import cipher

ROOT = Path(__file__).resolve().parents[2]
DIGEST = "ddb55de08262ebfd99acddf9c37f5ad3"


def test_br_088_key_check_label_digest_is_pinned() -> None:
    assert cipher.key_check(bytes(range(32))).hex() == DIGEST


def test_br_088_key_check_label_digest_sees_a_change() -> None:
    """Negative control: another key gives another digest (the pin is not a constant)."""
    assert cipher.key_check(bytes(32)).hex() != DIGEST


def test_br_088_key_check_label_is_assigned_once() -> None:
    """AC-01: `grep -c '^_CHECK_LABEL = ' src/hullgate/cipher.py` prints 1."""
    lines = (ROOT / "src" / "hullgate" / "cipher.py").read_text(encoding="utf-8").splitlines()
    assert [ln for ln in lines if ln.startswith("_CHECK_LABEL = ")] == [
        '_CHECK_LABEL = b"hullgate/connections/key-check/v1"'
    ]
