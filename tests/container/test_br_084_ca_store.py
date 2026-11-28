"""F-006 (BR-084) in the image: the TLS context of outbound calls loads the image's system
certificate store (OpenSSL's compiled paths), without reading `SSL_CERT_FILE`."""

from __future__ import annotations

import json

from .conftest import IMAGE, docker

PROBE = (
    "import json, ssl\n"
    "from hullgate.outbound import default_context\n"
    "print(json.dumps(default_context().cert_store_stats()))\n"
)


def test_br_084_image_default_context_has_system_cas() -> None:
    result = docker("run", "--rm", "--entrypoint", "python", IMAGE, "-c", PROBE)
    stats = json.loads(result.stdout)
    assert stats["x509_ca"] > 0  # the image's system store (ca-certificates)
    # SSL_CERT_FILE to an empty file: no effect on the context of outbound calls
    env = ["-e", "SSL_CERT_FILE=/dev/null"]
    again = docker("run", "--rm", *env, "--entrypoint", "python", IMAGE, "-c", PROBE)
    assert json.loads(again.stdout)["x509_ca"] == stats["x509_ca"]
