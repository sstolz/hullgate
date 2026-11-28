"""BR-212 (AC-11, image part) — the console is a static bundle built at image build time and served
by the admin listener; the runtime image contains no Node."""

from __future__ import annotations

import json
import re

from .conftest import APP_ENV, IMAGE, docker, wait_ready

FETCH = (
    "import json, urllib.request as u\n"
    "def get(path):\n"
    "    r = u.urlopen(u.Request('http://127.0.0.1:4791' + path,"
    " headers={'Host': 'localhost:4791'}), timeout=3)\n"
    "    return {'status': r.status, 'headers': dict(r.headers), 'body': r.read().decode()}\n"
    "shell = get('/admin')\n"
    "paths = __import__('re').findall(r'/admin/assets/[^\"]+', shell['body'])\n"
    "assets = [get(p)['status'] for p in paths]\n"
    "print(json.dumps({'shell': shell, 'assets': assets}))\n"
)


def test_br_212_runtime_image_has_no_node() -> None:
    probe = "command -v node nodejs npm npx corepack; find / -xdev -name 'node' -type f 2>/dev/null"
    result = docker("run", "--rm", "--entrypoint", "sh", IMAGE, "-c", probe, check=False)
    assert result.stdout.strip() == "", result.stdout


def test_br_212_bundle_is_in_the_image() -> None:
    out = docker("run", "--rm", "--entrypoint", "sh", IMAGE, "-c",
                 "ls /app/console /app/console/assets").stdout  # fmt: skip
    assert "index.html" in out
    assert re.search(r"index-[\w-]+\.js", out), out
    assert re.search(r"\.woff2", out), out  # self-hosted fonts
    # their licence text ships beside them (§8, OFL 1.1 condition 2; review 560, N3)
    for name in ("LICENSE-inter.txt", "LICENSE-jetbrains-mono.txt"):
        assert name in out, out
    licence = docker("run", "--rm", "--entrypoint", "sh", IMAGE, "-c",
                     "cat /app/console/assets/LICENSE-inter.txt").stdout  # fmt: skip
    assert "SIL Open Font License" in licence


def test_br_212_listener_serves_the_bundle_under_its_csp(name: str) -> None:
    docker("run", "-d", "--name", name, "--read-only", "-v", f"{name}:/data", *APP_ENV, IMAGE)
    wait_ready(name)
    got = json.loads(docker("exec", name, "python", "-c", FETCH).stdout)
    shell = got["shell"]
    assert shell["status"] == 200
    headers = {k.lower(): v for k, v in shell["headers"].items()}
    assert headers["content-security-policy"].startswith("default-src 'self'; script-src 'self';")
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert '<div id="app"></div>' in shell["body"]
    assert "<script>" not in shell["body"]  # nothing inline
    assert "<style" not in shell["body"]
    assert got["assets"]
    assert set(got["assets"]) == {200}
