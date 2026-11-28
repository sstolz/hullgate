# Install behind an existing Traefik

## Step mapping

Source roles refer to the private server runbooks. Identifiers are preserved; the merged
core keys, connections and relay namespaces do not collide.

| Source role | Runbook step | Guide step |
|---|---|---|
| installation | O1 | O1 |
| installation | O2 | O2 |
| installation — HTTPS URL replaces private-forge token setup | O3 | O3 |
| installation | O4 | O4 |
| installation | O5 | O5 |
| installation | O6 | O6 |
| installation | O7 | O7 |
| installation | O8 | O8 |
| installation — local readings replace private-forge reporting | O9 | O9 |

Replace the documented placeholders using the [parameter table](install.md#parameters).
Commands run as your SSH account on the target unless marked as workstation or browser actions.
Use the documented defaults below, or consistently substitute your own installation paths.


## Parameters

One supported baseline: existing Traefik and a clone built on the target. The operator keeps
chosen values in unversioned `.env`. Directory placeholders are documentation only; Compose
still defaults key paths to `../secrets/…`. With a different secrets directory, replace every
`secrets/…` command path consistently and set the three existing key-path variables.
Optional steps also use the command/output slots listed here; none creates a new `.env` key.

| Parameter | Meaning | Default |
|---|---|---|
| `<server>` | Target hostname | required; example `server.example.test` |
| `<user>` | SSH account in the Docker group with sudo | required |
| `<ssh-port>` | SSH port | `22` |
| `<install-dir>` | Clone directory (documentation placeholder, no new deployment variable) | `/opt/hullgate` |
| `<secrets-dir>` | Mounted secrets directory (documentation placeholder) | `<install-dir>/secrets` |
| `<repository-url>` | HTTPS clone URL | `https://github.com/sstolz/hullgate` |
| `<compose-root>` | Directory of existing Compose projects inspected at O1 | required |
| `<ref>` | Release tag or SHA selected before deployment | required; read with `git log` / `git rev-parse` |
| `<previous-ref>` | Healthy SHA recorded before updating | required for rollback |
| `<role-path>` | Four-segment role path read from `tree show` | required |
| `<mcp-url>` | Role's MCP URL copied from the console inspector | required for the Claude Code checks in admin B4 and D2 |
| `<connection-name>` | Full connection name read from `connection list` | required |
| `<gitea-host>` | Your Gitea hostname without the scheme | required for Gitea |
| `<client-id>` | OAuth application client id from the provider | required |
| `<scopes>` | Comma-separated provider scopes | required; least privileges for the service account |
| `<backup-file>` | Backup selected and checked before restoring | required |
| `<caller-ip>` | Public address of the outside caller allowed by the console allow-list | required for admin D3 |
| `<email>` | Administrator account name; no mail is sent | required |
| `<enrolment-token>` | Single-use credential from bootstrap; keep private | required; shown once |
| `<timestamp>` | UTC backup timestamp read from the backup output | required |
| `<org-id>` | Public organisation id from `tree show` | required |
| `<account>` | Provider account returned after OAuth authorization | output only |
| `<c>` | Client segment in a four-segment role path | read from `tree show` |
| `<check>` | Name of a verification check, such as `readyz-public` | output only |
| `<client>` | Client segment of a tree or connection path | read from `tree show` |
| `<connection>` | Full connection identifier for policy and placement commands | read from `connection list` |
| `<env>` | Environment segment of a role path | read from `tree show` |
| `<file>` | File named by a diagnostic or selected for checkout/backup | choose the file named in that step |
| `<label>` | Human-readable label for a token or passkey | choose a unique descriptive label |
| `<n>` | Count of connections, attempts or removed entries | output only |
| `<name>` | Object name printed by Docker or a CLI command | read from the preceding command |
| `<node>` | Tree node path for a policy command | read from `tree show` |
| `<old-volume>` | Previous database volume retained during fresh start | read from `.env.pre-rebuild` |
| `<prefix>` | Connection tool-name prefix | read from `connection list` |
| `<project>` | Project segment of a role path | read from `tree show` |
| `<public-tool>` | Full prefixed tool name used by the relay verifier | read from `connection tools` |
| `<repository>` | Repository for a client read-call example | choose a repository the test account can read |
| `<role>` | Full tree path of the role whose policy is inspected | read from `tree show` |
| `<s>` | Elapsed seconds reported by deployment | output only |
| `<sensitive-env>` | Sensitive environment path for the policy scenario | read from `tree show` |
| `<sensitive-role>` | Sensitive role path for relay verification | read from `tree show` |
| `<status-code>` | HTTP status returned by a probe | output only |
| `<time>` | Chosen daily backup time | 03:15 server local time |
| `<token-id>` | Revocable token identifier, never the token credential | read from `token list` |
| `<tool>` | Tool name used by policy commands | read from `connection tools` |
| `<traefik-container>` | Running Traefik container name for log inspection | read at O1 |
| `<type>` | Token type printed on revocation | output only |
| `<value>` | Diagnostic value emitted by a verification check | output only |
| `<x>` | Router-name slot normalized by O1 label inspection | output only |
| `MCP_HOST` | Public MCP DNS hostname | required |
| `TRAEFIK_NETWORK` | Existing Traefik Docker network (O1) | required |
| `TRUSTED_PROXIES` | Traefik peer CIDRs (admin P1/D3) | required |
| `TRAEFIK_ENTRYPOINT` | Existing HTTPS entrypoint | required |
| `TRAEFIK_CERTRESOLVER` | Existing ACME resolver | required |
| `TRAEFIK_HTTPS_PORT` | Published HTTPS port | `443` |
| `ADMIN_LOCAL_PORT` | Loopback admin port (use the same workstation tunnel port) | `14791` |
| `SIGNING_KEY_PATH` | ES256 PEM signing key | `/opt/hullgate/secrets/signing_key` |
| `AUTHORIZE_TOTP_PATH` | Base32 TOTP secret | `/opt/hullgate/secrets/authorize_totp` |
| `CONNECTIONS_KEY_PATH` | Base64 connections encryption key | `/opt/hullgate/secrets/connections_key` |
| `HULLGATE_VOLUME` | Production data volume | `hullgate_data` |
| `HULLGATE_IMAGE` | Locally built image tag | `hullgate:current` |
| `GITEA_URL` | Gitea base URL for the optional upstream | `https://git.example.org` |
| `UPSTREAMS_NETWORK` | Private Docker upstream network | `hullgate_upstreams` |
| `ADMIN_HOST` | Optional public admin hostname | empty (SSH tunnel) |
| `ADMIN_URL` | Optional public admin origin (exact HTTPS hostname) | empty; Compose uses `http://localhost:14791` at the default port |
| `ADMIN_RP_ID` | Passkey RP id, choose before enrolment | empty; application uses the admin hostname |

Install from a clone built on the target, behind an existing Traefik with an HTTPS
entrypoint, ACME resolver and shared Docker network. You need an SSH account in the Docker
group with sudo, Docker 25+, Compose v2, buildx, make, git, curl, flock, python3,
OpenSSL, coreutils, DNS tools and a workstation browser. Allow about 45 minutes plus DNS.
Run commands in a POSIX shell on the server unless a step names the workstation.
Replace placeholders before pasting; angle brackets are documentation, not shell syntax.

## Step O1 — Check the server's tools and read Traefik's values

On the server:

```sh
id -nG | tr ' ' '\n' | grep -x docker; make --version | head -n 1; git --version; curl --version | head -n 1; flock --version; docker version --format 'docker {{.Server.Version}}'; docker compose version; docker buildx version; uname -m
T=$(docker ps --format '{{.Names}} {{.Image}}' | awk 'tolower($2) ~ /traefik/ {print $1; exit}'); echo "Traefik container: $T"; docker inspect "$T" --format '{{range .Config.Cmd}}{{println .}}{{end}}' | grep -E 'entrypoints|certificatesresolvers|providers.docker'; docker port "$T"; docker inspect "$T" --format '{{range $n, $v := .NetworkSettings.Networks}}network: {{$n}}{{println}}{{end}}'
grep -rhoE 'traefik\.(docker\.network|http\.routers\.[^.]+\.(entrypoints|tls\.certresolver))"?[=:][[:space:]]*"?[^"[:space:]]+' <compose-root>/*/*compose*.y*ml 2>/dev/null | sed -E 's/routers\.[^.]+\./routers.<x>./; s/"?[=:][[:space:]]*"?/=/' | sort | uniq -c
```

**Expected result**: `docker`, then a version for `make`, `git`, `curl`, `flock`, `docker` (25 or more: the healthcheck uses `start_interval`), `docker compose` (v2) and `docker buildx`, then the architecture (`x86_64` or `aarch64`). Then the name of the Traefik container, its options `--entrypoints.<name>.address=:443` and `--certificatesresolvers.<name>.acme…`, its published port (`443/tcp -> 0.0.0.0:443`), its network(s), and the labels of an existing project. Note four values for step O5: Traefik's **network** (`TRAEFIK_NETWORK`), the **HTTPS entrypoint** (`TRAEFIK_ENTRYPOINT`, the one on `:443`), the certificate **resolver** (`TRAEFIK_CERTRESOLVER`), and the **published HTTPS port** on the host (`TRAEFIK_HTTPS_PORT`, usually 443).

**If the result differs**: `docker` missing from the first line → your account is not in the `docker` group (`sudo usermod -aG docker $USER`, then log in again). A tool is missing → `sudo apt-get update && sudo apt-get install -y make git curl util-linux` (`flock` comes from `util-linux`); `docker buildx` or `docker compose` is missing → packages `docker-buildx-plugin` and `docker-compose-plugin`. Docker < 25 → update Docker before continuing. No Traefik container → the `Traefik container:` line is empty: give its name by hand (`docker ps`) in `T=…` and paste the second line again. Traefik's options are in a file (`traefik.yml`) rather than on the command line → read `entryPoints` and `certificatesResolvers` in that file (`docker inspect "$T" --format '{{range .Mounts}}{{println .Source}}{{end}}'` gives its location).


## Step O2 — Create the DNS record for MCP_HOST

Choose the gateway's public name (for example `mcp.example.test` (replace it with your real DNS name)), create at your DNS provider an `A` record (and `AAAA` if the server has an IPv6) to the server, or a `CNAME` to your `<server>` hostname. Then, on your workstation, replace `<MCP_HOST>` and paste:

```sh
H=<MCP_HOST>; dig +short "$H"; echo '---'; dig +short <server>
```

**Expected result**: the addresses under `---` are found again above it (a `CNAME` first shows the server's name).

**If the result differs**: nothing above `---` → the record is not propagated yet: wait a few minutes and paste again; another address → fix the record. You can continue O3 to O5 during propagation; O6 needs it for the certificate.


## Step O3 — Choose the clone URL

The public repository requires no read token. If you use a private mirror, configure your
own Git credential helper with read-only access on the target; never put a token in a clone URL.

```sh
REPOSITORY_URL='<repository-url>'
# Keep this shell open through O4; the next block uses this variable.
printf '%s\n' "$REPOSITORY_URL"
```

**Expected result**: the chosen HTTPS clone URL; the default is `https://github.com/sstolz/hullgate`.

**If the result differs**: use the repository's HTTPS clone URL and check read access before O4.

## Step O4 — Clone into the installation directory

```sh
test -n "${REPOSITORY_URL:?Repeat O3 in this shell}" &&
sudo install -d -o "$(id -un)" -g "$(id -gn)" /opt/hullgate &&
git clone "$REPOSITORY_URL" /opt/hullgate &&
cd /opt/hullgate && git log --oneline -1 && ls Makefile deploy/compose.prod.yaml deploy/env.example
```

**Expected result**: the selected repository's latest commit and all three files.

**If the result differs**: an existing directory must be inspected with `git status` before reuse;
keep local changes before fetching. For authentication failures, fix the private mirror's read
access (O3). A permission failure needs the directory owned by your SSH account. After moving
an existing clone, recreate containers to update bind mounts:
`docker compose -f deploy/compose.prod.yaml --env-file .env up -d --force-recreate`.

## Step O5 — Create .env from deploy/env.example

```sh
cd /opt/hullgate && (test -e .env || cp deploy/env.example .env) && chmod 600 .env && ${EDITOR:-nano} .env
```

In the editor, fill in `MCP_HOST` (the name from step O2, without `https://`), `TRAEFIK_NETWORK`, `TRAEFIK_ENTRYPOINT`, `TRAEFIK_CERTRESOLVER` (step O1), and `TRUSTED_PROXIES` (the Traefik address/CIDR read in `docs/guides/admin.md` step P1), check `TRAEFIK_HTTPS_PORT` (443 by default) and `ADMIN_LOCAL_PORT` (14791 by default), save. Then:

```sh
cd /opt/hullgate && grep -E '^(MCP_HOST|TRAEFIK_[A-Z_]+|TRUSTED_PROXIES|ADMIN_LOCAL_PORT)=' .env && docker compose -f deploy/compose.prod.yaml --env-file .env config --quiet && echo "CONFIG OK" && (ss -ltn | grep -q ":$(sed -n 's/^ADMIN_LOCAL_PORT=//p' .env) " && echo "ADMIN PORT BUSY" || echo "ADMIN PORT FREE")
```

**Expected result**: the listed `KEY=value` lines all filled in, `CONFIG OK`, then `ADMIN PORT FREE`.

Before the first deployment, create all three mounted secrets using [keys and upstreams](keys-and-upstreams.md), steps S2, S3, S4, C2 and C3. These steps need no running image.

Select the deployment SHA once, in this shell, then run the read-only validation in
`docs/guides/admin.md` step P1 with this value of `REF`:

```sh
cd /opt/hullgate && git fetch origin && REF=$(git rev-parse origin/main) && echo "REF=$REF"
```

Keep this shell open through O6. Continue only after P1 reports `P1: OK`; if the shell is lost,
repeat this selection and P1 validation together before deploying.

**If the result differs**: `required variable MCP_HOST is missing a value` (or another) → the value is empty in `.env`: reopen the editor. For `TRUSTED_PROXIES`, follow `docs/guides/admin.md` step P1, including its read-only validation of the SHA chosen for O6 before deploying. `network … declared as external, but could not be found` only appears at deployment: reread the network at step O1. `ADMIN PORT BUSY` → choose another free `ADMIN_LOCAL_PORT` and paste again.


## Step O6 — First deployment: make deploy on the merge SHA

```sh
cd /opt/hullgate && test -n "${REF:?Repeat O5 and P1 to select and validate REF}" && time make deploy REF="$REF"
```

**Expected result** (2 to 5 minutes on the first run, the time to build the image): `BACKUP: no database (volume hullgate_data absent)`, the build, then `DOWNTIME <s>` and `DEPLOY: OK <ref>`; `time` shows the duration (`real`). Note the duration and the value of `DOWNTIME`.

**If the result differs**: `DEPLOY: KO … image build failed` → read the build error (network to PyPI or the image registries) and paste again. `network … could not be found` → wrong `TRAEFIK_NETWORK` in `.env` (O1, O5). `port is already allocated` → `ADMIN_LOCAL_PORT` taken (O5). `DEPLOY: KO … /readyz ≠ 200 after 60 s` → the last 20 log lines are shown; `make logs` gives the rest; fix then paste this block again. `working tree modified` → `git status` in the clone: a tracked file was modified by hand, `git checkout -- <file>`. The output says "no previous ref": normal at the first deployment, there is nothing to go back to.


## Step O7 — Back up then verify (make backup, make verify)

```sh
cd /opt/hullgate && make backup && make verify; echo "--- readings"; docker image ls hullgate; uname -m
```

**Expected result**: `BACKUP: OK backups/hullgate-<timestamp>.db integrity=ok tables={…}`, then six lines `VERIFY <check> OK <value>` (`deployed-ref`, `readyz-public`, `admin-local`, `admin-not-public`, `backup`, `restore-check`) and the banner `VERIFY: 6/6 OK`; after `--- readings`, the image `hullgate:current` with its size, and the server's architecture. Then, **on your workstation** (DNS and TLS end to end), replace `<MCP_HOST>`:

```sh
curl -s https://<MCP_HOST>/readyz; echo
```

**Expected result**: `{"status":"ok"}`.

**If the result differs**: `VERIFY readyz-public KO … 000` → no valid TLS response: the ACME certificate is not issued yet (O2's DNS not yet propagated, or ACME challenge in progress): wait two minutes and paste again; if it persists, `docker logs <traefik-container> 2>&1 | grep -i -E 'acme|hullgate' | tail`. `readyz-public KO … 404` → Traefik has no router for this name: check `TRAEFIK_NETWORK` and `TRAEFIK_ENTRYPOINT` (O1, O5) then replay O6. `admin-local KO` → `make status`: the container must be `healthy`. `admin-not-public KO` while `readyz-public` is OK → a router serves the administration: stop (`docker compose -f deploy/compose.prod.yaml --env-file .env stop`) and record the diagnostic locally. `backup KO` or `restore-check KO` → the `  |` lines above name the failing step. On the workstation, `Could not resolve host` → DNS (O2); certificate error → wait for issuance as above.


## Step O8 — Schedule the daily backup

```sh
( crontab -l 2>/dev/null | grep -v 'hullgate && make backup'; echo '15 3 * * * cd /opt/hullgate && make backup >> backups/backup.log 2>&1' ) | crontab - && crontab -l | grep 'hullgate && make backup'
```

**Expected result**: the line `15 3 * * * cd /opt/hullgate && make backup >> backups/backup.log 2>&1` (a backup every night at 3:15, 14 kept). Pasting the block again replaces the matching schedule without duplicating it. A backup scheduled during a deployment exits with a "lock held" error without doing anything: this is intended.

**If the result differs**: `crontab: command not found` → `sudo apt-get install -y cron`. You prefer another time → change `15 3` (minute, hour) and paste again.


## Step O9 — Record the verification locally

```sh
cd /opt/hullgate && make verify 2>&1 | sed -n '/^VERIFY /,/^VERIFY: /p'
docker image ls hullgate --format 'image {{.Repository}}:{{.Tag}} {{.Size}}'
printf 'architecture %s\n' "$(uname -m)"
```

**Expected result**: six successful checks and `VERIFY: 6/6 OK`, image size and architecture.
Keep these readings with the deployment SHA, O6 build duration and `DOWNTIME`, O7's workstation
TLS response, and the O8 backup schedule. Do not include secrets or `.env` contents.

**If the result differs**: follow the failing check in O7; retain the diagnostic locally until resolved.
