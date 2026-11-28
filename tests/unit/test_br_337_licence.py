"""BR-337: `LICENSE` is the unmodified MIT text with one copyright line for 2026.

That the holder names the operator is read by review (the rule's `not-mechanisable` half).
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# The canonical MIT text (as published by the OSI), the copyright line set apart.
MIT_BODY = """\
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
COPYRIGHT = re.compile(r"Copyright \(c\) 2026 (\S.*\S|\S)")


def problems(text: str) -> list[str]:
    head, sep, body = text.partition("\n\n")
    errors = []
    if head != "MIT License":
        errors.append("first line is not `MIT License`")
    copyright_line, sep2, rest = body.partition("\n\n")
    if not (sep and sep2 and COPYRIGHT.fullmatch(copyright_line)):
        errors.append("not one copyright line for 2026 with a non-empty holder")
    if rest != MIT_BODY:
        errors.append("licence text differs from the canonical MIT text")
    return errors


def test_BR_337_001_licence_is_canonical_mit() -> None:
    assert problems((ROOT / "LICENSE").read_text(encoding="utf-8")) == []


def test_BR_337_001_one_licence_file() -> None:
    names = [p.name for p in ROOT.iterdir() if p.name.upper().startswith(("LICENSE", "LICENCE"))]
    assert names == ["LICENSE"]


def test_BR_337_001_negative_controls() -> None:
    good = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert problems(good.replace("2026", "2025", 1))
    assert problems(good.replace("Copyright (c) 2026 ", "Copyright (c) 2026  ", 1))
    assert problems(good.replace("MERCHANTABILITY", "MERCHANTABILITY OR TITLE"))
    assert problems(good.replace("\n\nPermission", "\nCopyright (c) 2026 other\n\nPermission"))
    assert problems(good + "Additional term.\n")
