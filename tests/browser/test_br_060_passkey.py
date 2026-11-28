"""Browser wiring of the barrier's production script under its CSP, with virtual WebAuthn.

The HTTP ceremony is stubbed here; test_br_060_passkey in integration verifies its signatures,
request binding and limits against the real server. All browser requests are intercepted locally.
"""

import base64
import hashlib
import json
import subprocess
from typing import Any

from browser_support import GATEWAY, open_page


def test_br_060_barrier_script_submits_a_verified_assertion(browser: Any) -> None:
    w = open_page(browser)
    origin = "https://barrier.example.test"
    got: list[dict[str, Any]] = []
    result = subprocess.run(
        [
            "docker",
            "exec",
            GATEWAY,
            "python",
            "-c",
            "from hullgate.admin.barrier import SCRIPT; print(SCRIPT, end='')",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    script = result.stdout
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    csp = (
        f"default-src 'none'; script-src 'sha256-{digest}'; connect-src 'self'; form-action 'self'"
    )
    html = (
        "<form method=post action='/consent'><input type=hidden name=req value='pending'>"
        "<input type=hidden name=credential><button type=button id=passkey>"
        "Authorize with a passkey</button>"
        "<p id=passkey-error role=alert></p></form><script>" + script + "</script>"
    )
    challenge = base64.urlsafe_b64encode(b"barrier challenge of 32 bytes!!!!").decode().rstrip("=")

    def route(request: Any) -> None:
        from urllib.parse import parse_qs

        if request.request.method == "GET":
            request.fulfill(
                status=200,
                body=html,
                content_type="text/html",
                headers={"Content-Security-Policy": csp},
            )
            return
        data = parse_qs(request.request.post_data)
        if data["decision"] == ["passkey-options"]:
            request.fulfill(
                json={
                    "publicKey": {
                        "rpId": "barrier.example.test",
                        "challenge": challenge,
                        "userVerification": "required",
                        "allowCredentials": [],
                    }
                }
            )
        else:
            got.append(json.loads(data["credential"][0]))
            request.fulfill(status=200, body="Authorized", content_type="text/html")

    try:
        w.page.route("**/*", route)
        w.page.goto(origin + "/consent")
        # Establish a discoverable credential of this public origin on the virtual device.
        w.page.evaluate("""async () => {
          await navigator.credentials.create({publicKey: {
            challenge: new Uint8Array(32), rp: {id: 'barrier.example.test', name: 'test'},
            user: {id: new Uint8Array([1]), name: 'operator', displayName: 'operator'},
            pubKeyCredParams: [{type: 'public-key', alg: -7}], attestation: 'none',
            authenticatorSelection: {residentKey: 'required', userVerification: 'required'}
          }});
        }""")
        w.page.get_by_role("button", name="Authorize with a passkey").click()
        w.page.get_by_text("Authorized", exact=True).wait_for()
        assert len(got) == 1
        response = got[0]["response"]
        raw = response["clientDataJSON"]
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        assert data["origin"] == origin
        assert data["challenge"] == challenge
        assert not w.csp_violations()
    finally:
        w.close()
