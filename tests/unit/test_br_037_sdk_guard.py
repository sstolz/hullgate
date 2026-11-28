"""BR-036 and BR-037 — version guard of the `mcp` SDK: one condition per case (F-009, AC-02-04)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import sdk_guard

# The repository itself is judged by the gate's `sdk-guard` step, not by a test: a PR that
# loosens the constraint must end in `GATE: KO sdk-guard` (AC-02).

LOCK_TEMPLATE = """version = 1
requires-python = ">=3.13"

[[package]]
name = "mcp"
version = "{mcp}"
source = {{ registry = "https://pypi.org/simple" }}

[[package]]
name = "mcp-types"
version = "{mcp_types}"
source = {{ registry = "https://pypi.org/simple" }}
"""


def _lock(mcp: str = "2.3.0", mcp_types: str = "2.3.0") -> str:
    return LOCK_TEMPLATE.format(mcp=mcp, mcp_types=mcp_types)


# Written out: a test reading them from the guard would stay green if the guard emptied them.
N3 = "H-10/HULL-5-review-2-n3"
REVOKE_NOMINAL = "H-10/HULL-5-review-3-revoke-nominal"


def _controls() -> list[dict[str, Any]]:
    ids = ["BR-013", N3, REVOKE_NOMINAL, "BR-022"]
    return [{"id": i, "result": "réussi"} for i in ids]


def _evidence() -> dict[str, Any]:
    return {
        "meta": {
            "target": "v1",
            "dev_mode": False,
            "partial_run": False,
            "module_versions": {"mcp": "2.3.0", "mcp-types": "2.3.0", "pyjwt": "2.15.1"},
        },
        "tests": [{"id": f"T{n:02d}", "result": "réussi"} for n in range(1, 17)],
        "controls": _controls(),
    }


def _write_repo(
    root: Path,
    *,
    dependency: str = "mcp==2.3.0",
    v1_lock: str | None = None,
    mutate: Callable[[dict[str, Any]], None] | None = None,
    evidence_file: str = "results-v1.json",
) -> Path:
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "x"\nversion = "0"\ndependencies = ["{dependency}", "uvicorn"]\n',
        encoding="utf-8",
    )
    (root / "uv.lock").write_text(v1_lock or _lock(), encoding="utf-8")
    evidence = _evidence()
    if mutate is not None:
        mutate(evidence)
    path = root / sdk_guard.EVIDENCE.parent / evidence_file
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(evidence), encoding="utf-8")
    return root


def _fails(root: Path, step: str, fragment: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert sdk_guard.main(["--root", str(root)]) == 1
    err = capsys.readouterr().err
    assert f"SDK-GUARD {step} KO" in err
    assert fragment in err


def test_br_037_conforming_repository_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert sdk_guard.main(["--root", str(_write_repo(tmp_path))]) == 0
    out = capsys.readouterr().out
    assert "SDK-GUARD pin OK mcp==2.3.0" in out
    assert "SDK-GUARD bench OK mcp 2.3.0, mcp-types 2.3.0 (results-v1.json)" in out


@pytest.mark.parametrize(
    "dependency", ["mcp>=2.3.0", "mcp~=2.3.0", "mcp", "mcp[cli]==2.3.0", "mcp==2.3.0; os_name"]
)
def test_br_036_pin_must_be_exact_equality(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], dependency: str
) -> None:
    _fails(_write_repo(tmp_path, dependency=dependency), "pin", "exact equality", capsys)


def test_br_036_lock_must_resolve_pinned_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _write_repo(tmp_path, dependency="mcp==2.4.0")
    _fails(root, "pin", "uv.lock resolves mcp to 2.3.0", capsys)


def test_br_037_evidence_absent_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _write_repo(tmp_path)
    (root / sdk_guard.EVIDENCE).unlink()
    _fails(root, "bench", "evidence absent: poc/isolation-bench/evidence/results-v1.json", capsys)


@pytest.mark.parametrize("target", ["py-gateway", "gateway", "V1", None])
def test_br_037_target_must_be_v1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], target: str | None
) -> None:
    def retarget(e: dict[str, Any]) -> None:
        if target is None:
            del e["meta"]["target"]
        else:
            e["meta"]["target"] = target

    _fails(
        _write_repo(tmp_path, mutate=retarget),
        "bench",
        f"target `{target}` instead of `v1`",
        capsys,
    )


def test_br_037_poc_evidence_alone_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The POC witness (v1 of the rule) is no longer enough: the file read is the v1 target's."""

    def poc(e: dict[str, Any]) -> None:
        e["meta"]["target"] = "py-gateway"

    root = _write_repo(tmp_path, mutate=poc, evidence_file="results-py-gateway.json")
    _fails(root, "bench", "evidence absent", capsys)


def test_br_037_dev_mode_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def dev(e: dict[str, Any]) -> None:
        e["meta"]["dev_mode"] = True

    _fails(_write_repo(tmp_path, mutate=dev), "bench", "dev_mode", capsys)


def test_br_037_partial_run_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def partial(e: dict[str, Any]) -> None:
        e["meta"]["partial_run"] = True

    _fails(_write_repo(tmp_path, mutate=partial), "bench", "partial_run", capsys)


def test_br_037_mcp_version_differs_from_bench(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _write_repo(tmp_path, dependency="mcp==2.4.0", v1_lock=_lock(mcp="2.4.0"))
    _fails(root, "bench", "mcp: v1 uv.lock 2.4.0, bench 2.3.0", capsys)


def test_br_037_mcp_types_version_differs_from_bench(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _write_repo(tmp_path, v1_lock=_lock(mcp_types="2.4.0"))
    _fails(root, "bench", "mcp-types: v1 uv.lock 2.4.0, bench 2.3.0", capsys)


@pytest.mark.parametrize("package", sdk_guard.SDK_PACKAGES)
def test_br_037_module_version_absent_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], package: str
) -> None:
    def drop(e: dict[str, Any]) -> None:
        del e["meta"]["module_versions"][package]

    _fails(_write_repo(tmp_path, mutate=drop), "bench", f"module_versions.{package} absent", capsys)


def test_br_037_module_versions_block_absent_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def drop(e: dict[str, Any]) -> None:
        del e["meta"]["module_versions"]

    _fails(_write_repo(tmp_path, mutate=drop), "bench", "module_versions.mcp absent", capsys)


def test_br_037_meta_absent_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def drop(e: dict[str, Any]) -> None:
        del e["meta"]

    _fails(_write_repo(tmp_path, mutate=drop), "bench", "`meta` absent", capsys)


def test_br_037_fifteen_tests_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def fifteen(e: dict[str, Any]) -> None:
        e["tests"] = [t for t in e["tests"] if t["id"] != "T16"]

    _fails(
        _write_repo(tmp_path, mutate=fifteen),
        "bench",
        "tests absent from the evidence: T16",
        capsys,
    )


def test_br_037_tests_key_absent_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def drop(e: dict[str, Any]) -> None:
        del e["tests"]

    _fails(
        _write_repo(tmp_path, mutate=drop), "bench", "tests absent from the evidence: T01", capsys
    )


@pytest.mark.parametrize("test_id", sdk_guard.ALL_TESTS)
@pytest.mark.parametrize("verdict", ["échoué", "non jouable"])
def test_br_037_failed_line_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], test_id: str, verdict: str
) -> None:
    def failed(e: dict[str, Any]) -> None:
        for t in e["tests"]:
            if t["id"] == test_id:
                t["result"] = verdict

    root = _write_repo(tmp_path, mutate=failed)
    _fails(root, "bench", f"tests not passed: {test_id}", capsys)


def test_br_037_controls_absent_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def drop(e: dict[str, Any]) -> None:
        del e["controls"]

    _fails(_write_repo(tmp_path, mutate=drop), "bench", "controls absent or empty", capsys)


def test_br_037_controls_empty_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def empty(e: dict[str, Any]) -> None:
        e["controls"] = []

    _fails(_write_repo(tmp_path, mutate=empty), "bench", "controls absent or empty", capsys)


def test_br_037_failed_control_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def failed(e: dict[str, Any]) -> None:
        e["controls"][0]["result"] = "échoué"

    _fails(_write_repo(tmp_path, mutate=failed), "bench", "controls not passed: BR-013", capsys)


def test_br_037_failed_control_not_masked_by_duplicate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def duplicate(e: dict[str, Any]) -> None:
        e["controls"][0]["result"] = "échoué"
        e["controls"].append({"id": "BR-013", "result": "réussi"})

    _fails(_write_repo(tmp_path, mutate=duplicate), "bench", "controls not passed: BR-013", capsys)


def test_br_037_guard_names_both_harnesses() -> None:
    """Each harness is matched after its ticket prefix: the current one, and the former one of the
    evidence written before HULL-58 (built from fragments, so that no file spells it)."""
    former = "CL" + "OI"
    for pattern, ident in zip(sdk_guard.HARNESSES, (N3, REVOKE_NOMINAL), strict=True):
        assert pattern.match(ident)
        assert pattern.match(ident.replace("HULL", former))
        assert not pattern.match(ident + "-x")
        assert not pattern.match(ident.replace("HULL-5", "HULL-6"))


@pytest.mark.parametrize("harness", [N3, REVOKE_NOMINAL])
def test_br_037_harness_missing_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], harness: str
) -> None:
    def drop(e: dict[str, Any]) -> None:
        e["controls"] = [c for c in e["controls"] if c["id"] != harness]

    root = _write_repo(tmp_path, mutate=drop)
    pattern = next(p.pattern for p in sdk_guard.HARNESSES if p.match(harness))
    _fails(root, "bench", f"harnesses absent from the controls: {pattern}", capsys)


def test_br_037_readings_and_freshness_ignored(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-04: readings, freshness and fingerprints are not read; red if the guard reads one."""

    def noise(e: dict[str, Any]) -> None:
        meta = e["meta"]
        meta["lectures_v1"] = {"T06": "something else entirely"}
        meta["bench_commit"] = "0000000"
        meta["source_dirty"] = True
        meta["config_dirty"] = True
        meta["config_sha256_start"] = {"../../uv.lock": "0" * 64}

    assert sdk_guard.main(["--root", str(_write_repo(tmp_path, mutate=noise))]) == 0
    assert "SDK-GUARD bench OK" in capsys.readouterr().out


def test_br_037_poc_not_needed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """AC-03: neither `poc/py-gateway/` nor the POC evidence exists, and the guard passes."""
    root = _write_repo(tmp_path)
    assert not (root / "poc" / "py-gateway").exists()
    assert not (root / "poc/isolation-bench/evidence/results-py-gateway.json").exists()
    assert sdk_guard.main(["--root", str(root)]) == 0
    assert "(results-v1.json)" in capsys.readouterr().out


def test_br_037_failed_test_not_masked_by_duplicate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def duplicate(e: dict[str, Any]) -> None:
        e["tests"][4]["result"] = "échoué"
        e["tests"].append({"id": "T05", "result": "réussi"})

    _fails(_write_repo(tmp_path, mutate=duplicate), "bench", "tests not passed: T05", capsys)


@pytest.mark.parametrize("verdict", ["échoué", "non jouable"])
def test_br_037_recorded_test_outside_recipe_must_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], verdict: str
) -> None:
    """Every recorded test must have passed, even an identifier outside T01..T16."""

    def extra(e: dict[str, Any]) -> None:
        e["tests"].append({"id": "T17", "result": verdict})

    _fails(_write_repo(tmp_path, mutate=extra), "bench", "tests not passed: T17", capsys)


def test_br_037_recorded_test_outside_recipe_passing_is_accepted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def extra(e: dict[str, Any]) -> None:
        e["tests"].append({"id": "T17", "result": "réussi"})

    assert sdk_guard.main(["--root", str(_write_repo(tmp_path, mutate=extra))]) == 0
    assert "SDK-GUARD bench OK" in capsys.readouterr().out


@pytest.mark.parametrize("key", ["tests", "controls"])
@pytest.mark.parametrize("entry", ["BR-099 échoué", 3, None, ["T01", "réussi"]])
def test_br_037_non_object_entry_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], key: str, entry: object
) -> None:
    def junk(e: dict[str, Any]) -> None:
        e[key].append(entry)

    _fails(
        _write_repo(tmp_path, mutate=junk),
        "bench",
        f"entry of `{key}` that is not an object",
        capsys,
    )


def test_br_037_invalid_json_refused_with_step(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _write_repo(tmp_path)
    (root / sdk_guard.EVIDENCE).write_text("{not json", encoding="utf-8")
    _fails(root, "bench", "evidence unreadable (invalid JSON)", capsys)
