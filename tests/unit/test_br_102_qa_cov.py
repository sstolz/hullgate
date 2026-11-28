"""BR-102 — coverage is measured and reported, without a threshold (AC-04)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import qa_contract
import qa_cov

import qa


def _xml(path: Path, rate: str) -> Path:
    path.write_text(
        f'<?xml version="1.0" ?><coverage version="7" line-rate="{rate}" branch-rate="0">'
        "<packages/></coverage>",
        encoding="utf-8",
    )
    return path


def test_br_102_reports_the_line_rate_whatever_it_is(tmp_path: Path) -> None:
    for rate, shown in (("0.8734", "87.3%"), ("0", "0.0%"), ("1", "100.0%")):
        verdict, detail = qa_cov.evaluate("info", _xml(tmp_path / "coverage.xml", rate))
        assert verdict == "OK"
        assert shown in detail


def test_br_102_detail_carries_the_modification_date(tmp_path: Path) -> None:
    path = _xml(tmp_path / "coverage.xml", "0.5")
    os.utime(path, (1_700_000_000, 1_700_000_000))  # 2023-11-14 22:13 UTC
    _, detail = qa_cov.evaluate("info", path)
    assert "2023-11-14 22:13 UTC" in detail


def test_br_102_absent_file_is_a_skip_that_says_what_to_do(tmp_path: Path) -> None:
    verdict, detail = qa_cov.evaluate("info", tmp_path / "coverage.xml")
    assert verdict == "SKIP"
    assert "make gate" in detail


def test_br_102_never_findings_and_no_threshold(tmp_path: Path) -> None:
    verdict, _ = qa_cov.evaluate("info", _xml(tmp_path / "coverage.xml", "0.01"))
    assert verdict == "OK"


def test_br_102_unreadable_file_is_an_error(tmp_path: Path) -> None:
    bad = tmp_path / "coverage.xml"
    bad.write_text("not xml", encoding="utf-8")
    assert qa_cov.evaluate("info", bad)[0] == "ERROR"


def test_br_102_main_prints_exactly_one_conforming_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = qa_cov.main(["qa_cov.py", "info", str(_xml(tmp_path / "coverage.xml", "0.9"))])
    out = capsys.readouterr().out
    assert code == 0
    assert qa_contract.line_problems("cov", "info", out) == []
    assert out.startswith("QA cov info OK — ")
    assert len(out.splitlines()) == 1
    absent = qa_cov.main(["qa_cov.py", "info", str(tmp_path / "nope.xml")])
    assert absent == 0
    assert capsys.readouterr().out.startswith("QA cov info SKIP — ")
    assert qa.parse_line(out.strip()) is not None
