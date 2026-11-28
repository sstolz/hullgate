# Administer the console

## Step mapping

Source roles refer to the private server runbooks. Identifiers are preserved; the merged
core keys, connections and relay namespaces do not collide.

| Source role | Runbook step | Guide step |
|---|---|---|
| admin | P1 | P1 |
| admin | A1 | A1 |
| admin | A2 | A2 |
| admin | A3 | A3 |
| admin | A4 | A4 |
| admin | A5 | A5 |
| admin | A6 | A6 |
| admin | A7 | A7 |
| admin | B1 | B1 |
| admin | B2 | B2 |
| admin | B3 | B3 |
| admin | B4 | B4 |
| admin | C1 | C1 |
| admin | C2 | C2 |
| admin | C3 | C3 |
| admin | D1 | D1 |
| admin | D2 | D2 |
| admin | D3 | D3 |

Replace the documented placeholders using the [parameter table](install.md#parameters).
Commands run as your SSH account on the target unless marked as workstation or browser actions.
Use the documented defaults below, or consistently substitute your own installation paths.


Administer through the loopback SSH tunnel by default. Before enrolling passkeys, decide
whether to use that origin or the optional public route (D3). A passkey is bound to its RP id;
changing it requires host recovery (D1). Enrol a second device at the first sign-in.

Blocks run as pasted under **bash or zsh**. The clone directory is the operator's choice,
`<install-dir>` in the installation guide's parameter table. These blocks use its documented
default `/opt/hullgate`; substitute your own path consistently. Server commands run as your
SSH account there. Workstation commands run on the machine whose browser you use.
In every new server shell:

```sh
cd /opt/hullgate
```

## Step P1 — Declare the trusted proxy before deploying F-020 (the server, BR-302)

This prerequisite applies to **every deployment**, including the SSH-tunnel console. Play it
before the next `make deploy`; the production composition now refuses an absent or empty
`TRUSTED_PROXIES`. These are operator actions under BR-043, including the network read.

Read the network name from `.env`, then its subnet, gateway and attached container addresses:

```sh
cd /opt/hullgate
TRAEFIK_NETWORK=$(sed -n 's/^TRAEFIK_NETWORK=//p' .env | tail -n 1)
docker network inspect "$TRAEFIK_NETWORK" --format '{{json .IPAM.Config}}'
docker network inspect "$TRAEFIK_NETWORK" --format '{{json .Containers}}'
```

Identify Traefik in the container list and check its composition for a fixed address. Prefer
that address alone (`/32` IPv4, `/128` IPv6). Set `TRUSTED_PROXIES` in `.env` to the actual
reading, for example `TRUSTED_PROXIES=172.18.0.2/32` **only if that is Traefik's fixed address**.
If it has no fixed address, fix it in Traefik's composition first, or use the network subnet for
the public MCP listener. A subnet also trusts every other container and Docker's gateway;
it is unsuitable for D3's public console, which requires Traefik's own address. Never use
`0.0.0.0/0` or `::/0`. Recheck the value after network or Traefik recreation.

Then validate the composition **from the selected deployment SHA**, already fetched in the
clone (update runbook M1, or installation runbook O5, which reads `origin/main` once into
`REF`), and parse the resolved CIDR list. Replace `<ref>` below with that selected value;
on the installation path, replace `REF='<ref>' &&` with `test -n "${REF:?Run O5 first}" &&`
to validate the existing variable. This read-only check needs `python3` on the server; it neither checks out the reference nor starts a
container. The explicit project directory preserves the composition's relative paths.

```sh
(
cd /opt/hullgate &&
REF='<ref>' &&
COMPOSITION=$(git show "$REF:deploy/compose.prod.yaml") &&
printf '%s\n' "$COMPOSITION" |
  docker compose --project-directory "$PWD/deploy" -f - --env-file "$PWD/.env" \
    config --format json |
  python3 -c '
import ipaddress, json, sys
try:
    config = json.load(sys.stdin)
    raw = config["services"]["hullgate"]["environment"].get("TRUSTED_PROXIES", "")
    networks = [ipaddress.ip_network(s.strip(), strict=False)
                for s in raw.split(",") if s.strip()]
    if not networks:
        raise ValueError("at least one CIDR expected")
except (ValueError, KeyError, TypeError, AttributeError):
    sys.exit("P1: KO composition or TRUSTED_PROXIES invalid; do not deploy")
print("P1: OK composition and TRUSTED_PROXIES CIDRs")
'
)
```

**Expected result**: `P1: OK composition and TRUSTED_PROXIES CIDRs`. Only after this succeeds,
deploy that same SHA with step M2 of `update.md` (O6 of the installation
runbook for a first deployment). An absent, empty, separators-only or invalid CIDR value
must fail this check before deployment. A valid but wrong range can start: it must still pass
the external address check below. A forwarding peer outside the trust list is named in the
gateway log at most once per hour.

After deployment, perform F-020 **AC-OP-01**. **On your outside machine**, replace `<MCP_HOST>`,
`<org-id>` and `<role-path>` with the public host, organisation identifier and an existing role path.
Record this machine's public address for each published family. Read the `dig` results and
run the corresponding `curl` commands below (omit the entire `curl -4` command if there is
no A record, or the entire `curl -6` command if there is no AAAA record):

```sh
MCP_HOST='<MCP_HOST>'
MCP_URL="https://$MCP_HOST/o/<org-id>/p/<role-path>/mcp"
dig A "$MCP_HOST"
dig AAAA "$MCP_HOST"
curl -4 -i -X POST -H 'Authorization: Bearer bad' \
  -H 'X-Forwarded-For: 203.0.113.9' "$MCP_URL"
curl -6 -i -X POST -H 'Authorization: Bearer bad' \
  -H 'X-Forwarded-For: 203.0.113.9' "$MCP_URL"
```

Then **on the server**, read the audit immediately:

```sh
cd /opt/hullgate &&
docker compose -f deploy/compose.prod.yaml --env-file .env \
  exec -T hullgate hullgate audit tail -n 3 --json
```

Each request must return 401; its audit entry's `detail.address` must be that machine's public
address for the family, unchanged by the forged header, never `203.0.113.9`, Traefik or
Docker's gateway.

**If the result differs**: a pre-deployment failure means stop before M2/O6, correct the SHA,
composition or `TRUSTED_PROXIES` value, and rerun the read-only check; a refused composition
names `TRUSTED_PROXIES` and this step. If the post-deployment audit differs, correct the
forwarding path and trust list, revalidate, redeploy and repeat AC-OP-01 before accepting the
deployment. If other traffic displaced the entries from the last three rows, repeat one
family's request and read the audit immediately before proceeding to the other family.


## Step A1 — Deploy the reference of lot A (the server)

```sh
cd /opt/hullgate
git fetch origin
git log --oneline -3 origin/main
grep -c '^ADMIN_URL=.' .env || true
make deploy REF=<ref>
make verify
```

Replace `<ref>` by the first line of the `git log`. The first `make deploy` of lot A builds the image's new `console` stage on the server: it pulls the pinned `node` image (Node 26.13.0, by digest) from Docker Hub and runs `npm ci`, which fetches the console's packages from `registry.npmjs.org`. the server must reach both; later deploys reuse the build cache while `web/package-lock.json` does not change.

**Expected result**: `grep -c` prints `0` (no non-empty `ADMIN_URL` in `.env`: the base compose file sets it); `make deploy` ends on its success line (the console stage adds a minute or two the first time); `make verify` ends on `VERIFY: 6/6 OK`.

**If the result differs**: `grep -c` prints `1` → for the default tunnel, clear `ADMIN_URL`; for an intentional public console follow D3 instead. The container restarts in a loop and `make logs` shows `ADMIN_URL : a hostname is expected, not an IP address` → the deployed compose file is not the lot A one: check the SHA. `make deploy` fails at `npm ci` with `ENOTFOUND registry.npmjs.org` or at the `node` pull → the server cannot reach the npm registry or Docker Hub: restore that access, then replay the block; the running gateway is untouched by a failed build. `VERIFY admin-local KO` → `make logs`, then stop here.


## Step A2 — Read-only checks of the console (the server)

```sh
cd /opt/hullgate
make verify-admin
```

**Expected result**: four lines `VERIFY-ADMIN healthz OK …`, `foreign-host OK … 421`, `api-session OK … 401`, `shell-csp OK default-src 'self'; …`, then `VERIFY-ADMIN: 4/4 OK`. The banner matches `^VERIFY-ADMIN: 4/4 OK$`.

**If the result differs**: `healthz KO` → the container is down (`make status`, `make logs`). `foreign-host KO` with `200` → the image is not lot A's. `api-session KO` with `421` → `ADMIN_LOCAL_PORT` in `.env` differs from the published port: they must be equal. `shell-csp KO absent` → the console bundle is missing from the image: rebuild with `make deploy REF=<ref>`.


## Step A3 — Open the tunnel (your machine)

```sh
ssh -p <ssh-port> -N -L 14791:127.0.0.1:14791 <user>@<server>
```

Use your configured `ADMIN_LOCAL_PORT` on both sides of `-L` if it differs from 14791.
Leave it running in its own terminal; every browser step below goes through it.

**Expected result**: the command prints nothing and keeps running; `http://localhost:14791/admin` opens a page with one button, "Sign in with a passkey".

**If the result differs**: `bind: Address already in use` → another program holds port 14791 on your machine: stop it, or use `-L 24791:127.0.0.1:14791` **and** know that the console then refuses you (421: its origin is `localhost:14791`). A blank page with `Misdirected Request` → you typed `127.0.0.1` in the address bar: use `localhost`.


## Step A4 — Create the administrator (the server)

Set `E=` to your e-mail address; it is the account's name, shown at enrolment, never mailed:

```sh
cd /opt/hullgate
E=<email>
docker compose -f deploy/compose.prod.yaml --env-file .env exec -T hullgate hullgate admin bootstrap "$E" --name "Operator"
```

**Expected result**: `administrator pending: adm_… <email>`, the line `the link below is shown once and expires in 30 minutes`, then `ENROLMENT-LINK: http://localhost:14791/admin/enrol/…`. The credential line matches `^ENROLMENT-LINK: `; keep its value private.

**If the result differs**: `refused: an active administrator exists` → the instance already has one: sign in with its passkey, or follow A7. A typo in the e-mail → replay the block with the right one: the pending account is replaced and the old link dies (BR-201).


## Step A5 — Enrol two passkeys (your machine, browser)

Open the link of A4 in the browser of A3, within 30 minutes. The page shows your e-mail and asks for a passkey label: type the device's name, click **Create a passkey**, and confirm with your device. The console opens on the Tree tab.

Then open the account menu (your e-mail, top right) → **Passkeys**, type a label for the second device, click **Add a passkey**: the console asks for your first passkey again (BR-210), then for the second device's.

```text
http://localhost:14791/admin/enrol/<enrolment-token>
```

**Expected result**: the address bar shows `http://localhost:14791/admin/enrol` without the token as soon as the page has loaded; after the first ceremony, the Tree tab; the Passkeys dialog lists two passkeys. In the browser's cookie inspector, `hullgate_admin` has HttpOnly, SameSite Strict and Path `/admin`; Secure is off at the localhost origin.

**If the result differs**: "This enrolment link is used, expired or replaced" → replay A4 and use the new link. "This browser does not support passkeys" → use a current Chromium, Firefox or Safari. "Too many attempts from your address" → A7, `attempts clear`. Adding the second passkey fails right after the confirmation → you used the same authenticator: a passkey already registered on it is refused, use another device or security key.


## Step A6 — Sign out, sign in, read the audit (your machine, then the server)

In the browser: account menu → **Sign out**, then **Sign in with a passkey**. Then on the server:

```sh
cd /opt/hullgate
docker compose -f deploy/compose.prod.yaml --env-file .env exec -T hullgate hullgate admin list --json
docker compose -f deploy/compose.prod.yaml --env-file .env exec -T hullgate hullgate audit tail -n 3 --json
```

**Expected result**: the browser returns to the Tree tab after the passkey; `admin list --json` shows your e-mail, `"state": "active"`, `"passkeys": 2`; the audit's last entries are `admin.sign_out` then `admin.sign_in`, each with `"actor": "admin:adm_…"`. The `address` on `admin.sign_in` is Docker's bridge address for the tunnel.

**If the result differs**: the sign-in page says "Sign-in failed" → the passkey offered is not one of this console's (another RP id, or deleted): pick the other one. No `admin.sign_in` entry → the browser did not reach the console: check the tunnel of A3.


## Step A7 — The way back from the host (the server, only if needed)

Five failed sign-ins from one address close sign-in to it for 15 minutes; over the tunnel every caller may share Docker's bridge address (BR-207). An allow-list that no longer holds your address closes the console (BR-209). Both are reopened from the host:

```sh
cd /opt/hullgate
docker compose -f deploy/compose.prod.yaml --env-file .env exec -T hullgate hullgate admin attempts clear
docker compose -f deploy/compose.prod.yaml --env-file .env exec -T hullgate hullgate admin allowlist clear
```

**Expected result**: `sign-in attempts cleared: <n>` and `allow-list cleared: <n> entries removed`; the sign-in page answers again.

**If the result differs**: `database unavailable` → the container is not running (`make status`). If you lost every passkey, use the host recovery in D1 below (BR-228).


---

## Console connections and tokens

Use B1–B4 to verify placement and token operations from the console after deployment.
The CLI shortcuts above still apply. The Tree, Tokens and Audit tabs and the Library share
the same state as the CLI. Destructive actions show the server's impact before confirmation.

## Step B1 — Deploy the reference of slice 2 (the server)

```sh
cd /opt/hullgate
git fetch origin
git log --oneline -3 origin/main
make deploy REF=<ref>
make verify
make verify-admin
```

**Expected result**: `make deploy` ends on its success line; `VERIFY: 6/6 OK`; `VERIFY-ADMIN: 4/4 OK`.

**If the result differs**: as A1 and A2. `make logs` shows `schema_error` → migration 9 failed: stop here and keep the diagnostic locally.


## Step B2 — Sign in again (your machine, browser)

Open the tunnel of A3, then `http://localhost:14791/admin` and **Sign in with a passkey** with a passkey enrolled at A5.

```text
http://localhost:14791/admin
```

**Expected result**: the Tree tab, with the Library, the tree and the inspector side by side; the Tokens and Audit tabs beside it.

**If the result differs**: "Sign-in failed" → the passkey offered is not one of this console's; A7 if the address is limited.


## Step B3 — Place a connection and issue a token (your machine, browser)

In the Tree tab: select the role you will connect (for instance `<client>/<project>/<env>/principal`), drag a connection of the Library onto it, or select the connection and use **Place in…** then the role. If a share dialog opens, read it: placing a connection outside its client or between a sensitive and a non-sensitive role needs the ticked confirmation; **Connect a dedicated one** is the recommended way out. Then in the Tokens tab, **Issue** a token on that role with a label, copy the value shown once, and click **I copied it — hide**.

```text
Tree → <role> → Place in… → <connection>      Tokens → Issue → <role>, <label>
```

**Expected result**: the inspector of the role lists the connection with its prefix; a notice says connected clients see the new list at their next `tools/list`; the token appears in the list with its expiry, its value nowhere.

**If the result differs**: the drop is refused with the server's text (a lock, a level, the prefix) → read it, it is the CLI's refusal. "The impact changed since it was shown" → the state moved: read the new numbers, act again.


## Step B4 — Connect Claude Code, see the change at the next list (your machine, then the server)

On your workstation, connect Claude Code to the role's MCP URL (the inspector's **Copy**
button) with the token of B3. Use a folder outside a repository: the local scope binds the
entry to this folder. Replace `<mcp-url>` before running the block; paste the token only at
the silent terminal prompt.

```sh
mkdir -p ~/hullgate-acop02 && cd ~/hullgate-acop02
read -rs HG_TOKEN   # paste the value Tokens → Issue showed once, then Enter
claude mcp add --transport http --scope local hullgate-acop02 "<mcp-url>" --header "Authorization: Bearer $HG_TOKEN"
unset HG_TOKEN
claude
```

In Claude Code, `/mcp` → `hullgate-acop02` shows its tools. In the console remove the placement
(inspector → the connection → **Unplace**). Then `/mcp` → `hullgate-acop02` → **Reconnect**
asks for the list again. Afterwards, in the same folder, run
`claude mcp remove hullgate-acop02 --scope local`, then revoke the token in the Tokens tab:
Claude Code stored it in clear in its config. Never run `claude mcp get`, which prints the
header. Then on the server:

```sh
cd /opt/hullgate
docker compose -f deploy/compose.prod.yaml --env-file .env exec -T hullgate hullgate audit tail -n 5 --json
```

**Expected result**: before the removal the client lists the connection's tools under its prefix; after it, at its next `tools/list`, they are gone, with no restart of the gateway (BR-215: nothing is pushed to the client). The audit's last entries include `connection.unplace` with `"actor": "admin:adm_…"`.

**If the result differs**: the client still lists the tools → it has not asked for the list again: some clients cache it until reconnected, which is the client's behaviour, not the gateway's (Q1 = a). The client cannot connect → the token is not on this role (`token list` in the Tokens tab) or the URL is not the inspector's.


---

## Policy screens

Use C1–C3 to compare the role's rights preview with its client's actual tools, then change a
placement mode and verify the next list reflects it. An unreachable upstream is unknown,
never silently absent. The preview uses the same evaluator as `tools/list`.

## Step C1 — Deploy the reference of slice 3 (the server)

```sh
cd /opt/hullgate
git fetch origin
git log --oneline -3 origin/main
make deploy REF=<ref>
make verify
make verify-admin
```

**Expected result**: `make deploy` ends on its success line; `VERIFY: 6/6 OK`; `VERIFY-ADMIN: 4/4 OK`.

**If the result differs**: as A1 and A2.


## Step C2 — Place, read the preview, connect a client (your machine, browser, then the server)

Sign in (B2). Place a connection on a role and issue a token on that role (B3). Select the role: the inspector shows its rights preview. Connect Claude Code using [B4's workstation block](#step-b4--connect-claude-code-see-the-change-at-the-next-list-your-machine-then-the-server), with this role's MCP URL and token, and list its tools. Follow B4's cleanup after C3. Then on the server, with `<role>` read back from `tree show`:

```sh
cd /opt/hullgate
docker compose -f deploy/compose.prod.yaml --env-file .env exec -T hullgate hullgate policy preview <role> --json
```

**Expected result**: the preview's exposed tools are the client's list, name for name, by eye; hidden tools carry their reason (`above_mode`, `write_closed`, `denied`…).

**If the result differs**: a connection shown unknown → its upstream did not answer the listing: `connection tools <connection> --role <role>` on the server says why. The client lists more or fewer tools than the preview → compare both lists: it is BR-217's failure.


## Step C3 — Change a mode, see the client's list change (your machine, then the server)

In the inspector of the role, change the placed connection's mode (for instance to `read`), read the preview again, then in Claude Code use `/mcp` → `hullgate-acop02` → **Reconnect** to list its tools again. Then on the server:

```sh
cd /opt/hullgate
docker compose -f deploy/compose.prod.yaml --env-file .env exec -T hullgate hullgate policy preview <role> --json
docker compose -f deploy/compose.prod.yaml --env-file .env exec -T hullgate hullgate audit tail -n 5 --json
```

**Expected result**: the notice says connected clients see the new list at their next `tools/list`; the preview's exposed tools shrink to the new mode; the client's next `tools/list` gives the same names, with no restart (BR-215). The audit's last entries include `policy.mode` with `"actor": "admin:adm_…"`.

**If the result differs**: the client keeps the old list → it has not asked for the list again (some clients cache it until reconnected, Q1 = a). The mode is refused → read the server's text: a lock above the role (BR-049).


## Step D1 — Host recovery (BR-228, AC-29)

For an active administrator who lost their devices, run on the host:

```sh
docker compose -f deploy/compose.prod.yaml --env-file .env exec hullgate hullgate admin recover ops@example.test
```

**Expected result**: this revokes **every passkey**, ends every session and prints one 30-minute enrolment link. Open
that link at the configured console origin, enrol a new passkey, then add a second device. The old
passkeys must fail. Recovery can be rerun if the link expires; the previous unused link is replaced.
A pending account is refused with an instruction to use `admin bootstrap`. There is no recovery
API or e-mail recovery.

**If the result differs**: verify the account's e-mail with `admin list`; use `admin bootstrap`
for a pending account. If the link expired, repeat recovery and use only the latest link.


## Step D2 — Organisation and first-run guide (AC-OP-03)

On your workstation, use a fresh local instance on `compose.yaml`, under project name
`hullgate-acop03`. Prerequisites: git, uv, Docker with Compose v2, Claude Code, a browser
with passkeys, and ports 4790 and 4791 free. The example clone `~/hullgate-acop03` is your
choice; substitute it consistently. Set `REPOSITORY_URL` from the installation guide's
parameter table. These are disposable local credentials; do not post their values.
`compose.yaml` mounts no connections key: the first block creates it in the new volume.

  1. *Workstation, your account, bash or zsh, folder `~`, then the new clone* — the volume check must print `0` before anything is built:
     ```sh
     docker volume ls -q --filter name='^hullgate-acop03_data$' | wc -l
     git clone "$REPOSITORY_URL" ~/hullgate-acop03 && cd ~/hullgate-acop03
     git log --oneline -1
     make sync
     mkdir -p secrets
     uv run --locked --no-sync python -m hullgate.keys signing-key secrets/signing_key
     uv run --locked --no-sync python -m hullgate.keys totp-secret secrets/authorize_totp --label hullgate-acop03
     chmod 0444 secrets/signing_key secrets/authorize_totp
     docker compose -p hullgate-acop03 build
     docker compose -p hullgate-acop03 run --rm --entrypoint python hullgate -m hullgate.keys connections-key /data/connections_key
     docker compose -p hullgate-acop03 run -d --service-ports --name hullgate-acop03 -e CONNECTIONS_KEY_FILE=/data/connections_key hullgate
     curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4790/readyz
     ```
     `$REPOSITORY_URL` as in the installation guide's parameter table. Expected: `0` first, `200` last. The TOTP URI the second key command prints is this throwaway instance's: not posted. The volume check prints `1` → an earlier replay left its volume: removing it (`docker compose -p hullgate-acop03 down -v` in its clone) is the operator's call, or replay under another project name.
  2. *Workstation, your account, bash or zsh, folder `~/hullgate-acop03`* — the measure starts:
     ```sh
     date -u '+T0 %H:%M:%S'
     docker exec hullgate-acop03 hullgate admin bootstrap "<email>" --name "Operator"
     ```
     Expected: `administrator pending: adm_… <email>`, then `ENROLMENT-LINK: http://localhost:4791/admin/enrol/…`.
  3. *Workstation, your browser* — open the link, enrol one passkey, open the setup guide and follow its steps through the ordinary screens: a service whose MCP upstream this container reaches, with a credential you hold for it; its connection; its placement on a role; a token issued on that role in the Tokens tab (1 h). Copy the role's MCP URL from the inspector: `http://127.0.0.1:4790/o/<org-id>/p/<role-path>/mcp`.
  4. *Workstation, your account, bash or zsh, folder `~/hullgate-acop03`* — Claude Code, token read from the terminal:
     ```sh
     read -rs HG_TOKEN   # paste the value Tokens → Issue showed once, then Enter
     claude mcp add --transport http --scope local hullgate-acop03 "<mcp-url>" --header "Authorization: Bearer $HG_TOKEN"
     unset HG_TOKEN
     claude
     ```
     In Claude Code, `/mcp` → `hullgate-acop03`: the first tool listed ends the measure. At once, in a second terminal (same machine, account, shell): `date -u '+T1 %H:%M:%S'`.
  5. *Workstation, your account, bash or zsh, folder `~/hullgate-acop03`* — read back:
     ```sh
     docker exec hullgate-acop03 hullgate admin list --json
     docker exec hullgate-acop03 hullgate tree show / --json
     docker exec hullgate-acop03 hullgate connection list --json
     docker exec hullgate-acop03 hullgate audit tail -n 5 --json
     ```

The five guide links each open or focus their step's ordinary control: the client field,
**Declare a service**, **Connect…**, the first connection's **Place in…** (or **Connect…**
if there is none), and the Tokens issue form's role field. Register the displayed return
URIs at the provider and request the service's displayed scopes. Refresh the steps to read
progress. Dismiss the guide and reopen it from Organisation; its status must still match
the real state. Record elapsed time against the 15-minute objective. Organisation also
manages the name, passkeys and allow-list and shows the configured origins read-only.
Afterwards, remove the Claude Code entry with `claude mcp remove hullgate-acop03 --scope local`
and revoke its token. Removing the disposable container and volume is the operator's choice.

```sh
docker exec hullgate-acop03 hullgate settings show /
```

For a provided token with a known expiry, supply the optional ISO 8601 timestamp including its
timezone when connecting/replacing it. The default warning is seven days. Change the global
threshold in Organisation or with `settings set / connection_expiry_days 5`; the next read uses
five days. A replacement without an expiry has an unknown end, not the old token's deadline.

**Expected result**: the guide tracks all five ordinary acts, can be dismissed and reopened, and
the expiry warning changes on the next read after changing the threshold.

**If the result differs**: use Refresh steps, read the refusal on the ordinary screen, and check
the server's global setting above. Do not mark a guide step complete manually.


## Step D3 — Optional public routing (BR-230, AC-OP-05)

The default remains the SSH tunnel. To enable a public console, configure `ADMIN_HOST`, an exact
`ADMIN_URL=https://<ADMIN_HOST>` and Traefik's own address in `TRUSTED_PROXIES` in `.env`.
Trust only that address (`/32` for IPv4, `/128` for IPv6), not its subnet or the network gateway.
A host caller of the loopback-published admin port arrives from the network gateway; trusting
the whole subnet would let that caller forge `X-Forwarded-For` to choose the address used by the
allow-list and attempt window. For example, if Traefik is `172.18.0.2`, declare:

```dotenv
TRUSTED_PROXIES=172.18.0.2/32
```

Replace the example with Traefik's actual address on the shared network; keep it stable across
container recreation and recheck it after network changes. `deploy/ops.sh`
automatically adds `deploy/compose.admin.yaml`; it refuses missing proxy trust or a mismatched
origin before invoking Docker. The application validates those prerequisites again at startup.
Do not enable the overlay manually with mismatched values.

A dedicated hostname is recommended. Sharing `MCP_HOST` is supported: only `/admin` is routed to
4791, with higher router priority than the public listener. TLS is mandatory; the host-published
admin port stays on loopback. The default composition has no admin router. The local smoke can
be played in both configurations with `SMOKE_ADMIN_HOST=admin.example.test make prod-smoke`, or
with its fixture's MCP hostname for the shared-name case; no production host is used by that test.

```sh
make prod-smoke
SMOKE_ADMIN_HOST=admin.example.test make prod-smoke
SMOKE_ADMIN_HOST=hullgate.smoke.invalid make prod-smoke
```

Choose `ADMIN_RP_ID` **before enrolment**. Sibling admin and MCP hostnames need a common parent RP
id for the barrier passkey; a shared hostname can use that exact name. Every subdomain of a parent
RP id can request assertions, which is part of this configuration decision. `localhost` stays
TOTP-only at the barrier. Changing the RP id requires D1 recovery and enrolment at the new origin.
Changing `ADMIN_URL` also requires registering the new web return URI at every OAuth provider and
refreshing the application's web-return confirmation; the stored flag cannot prove provider state.

Before accepting public exposure, prove the shell responds, its session API refuses anonymously,
a foreign Host gets 421 on the listener, the configured allow-list excludes an unlisted address,
and a passkey passes the barrier even after five wrong TOTP entries. Record the address displayed
by Organisation and compare it to the caller's address to verify proxy trust.

Also perform a forged `X-Forwarded-For` check through the loopback publication. With an allow-list
that includes your real external caller address but excludes the network gateway, run these on
the deployment host (substitute the configured hostname, local port and an allowed caller IP):

```sh
ADMIN_HOST='<ADMIN_HOST>'
ADMIN_LOCAL_PORT=14791  # replace if your .env uses another port
ALLOWED_CALLER_IP='<caller-ip>'
curl -sS -o /dev/null -w '%{http_code}\n' \
  -H "Host: ${ADMIN_HOST}" \
  "http://127.0.0.1:${ADMIN_LOCAL_PORT}/admin/api/v1/session"
curl -sS -o /dev/null -w '%{http_code}\n' \
  -H "Host: ${ADMIN_HOST}" -H "X-Forwarded-For: ${ALLOWED_CALLER_IP}" \
  "http://127.0.0.1:${ADMIN_LOCAL_PORT}/admin/api/v1/session"
```

Both requests must return **403**. A forged request returning 401 has passed the allow-list and
fails this check, even though sign-in is still required. Keep the loopback publication for the
SSH tunnel.

**Expected result**: `admin-not-routed` by default and `admin-routed` for both explicit host cases;
public API authentication, Host checks and the allow-list remain enforced.

**If the result differs**: keep the default tunnel, check exact origin and Traefik's trusted address,
and inspect the local smoke's CHECK lines before enabling the public route.
