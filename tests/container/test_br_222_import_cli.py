"""Installed-image export/import round trip, parser dependency and rollback boundary (AC-24)."""

from .conftest import APP_ENV, IMAGE, docker, wait_ready

PROBE = """
import json, subprocess, pathlib
p = pathlib.Path('/data/roundtrip.yaml')
p.write_text(subprocess.check_output(['hullgate', 'export'], text=True))
plan = json.loads(subprocess.check_output(['hullgate', 'import', '--preview', str(p)], text=True))
assert plan['changes'] == [], plan
cmd = ['hullgate', 'import', '--apply', plan['digest'], str(p)]
applied = json.loads(subprocess.check_output(cmd, text=True))
assert applied['changes'] == 0, applied
cmd = ['hullgate', 'import', '--apply', 'stale', str(p)]
bad = subprocess.run(cmd, capture_output=True, text=True)
assert bad.returncode == 2, bad
assert subprocess.check_output(['hullgate', 'export'], text=True) == p.read_text()
print('roundtrip and stale refusal passed')
"""


def test_br_222_installed_cli_roundtrip(name: str) -> None:
    docker("run", "-d", "--name", name, "--read-only", "-v", f"{name}:/data", *APP_ENV, IMAGE)
    wait_ready(name)
    result = docker("exec", name, "python", "-c", PROBE)
    assert "roundtrip and stale refusal passed" in result.stdout
