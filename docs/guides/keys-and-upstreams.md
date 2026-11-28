# Keys and upstream connections

## Step mapping

Source roles refer to the private server runbooks. Identifiers are preserved; the merged
core keys, connections and relay namespaces do not collide.

| Source role | Runbook step | Guide step |
|---|---|---|
| core keys | S1 | S1 |
| core keys | S2 | S2 |
| core keys | S3 | S3 |
| core keys | S4 | S4 |
| core keys | S5 | S5 |
| core keys | S6 | S6 |
| core keys | S7 | S7 |
| core keys | S8 | S8 |
| core keys | S9 | S9 |
| core keys | S10 | S10 |
| core keys | S11 | S11 |
| core keys | S12 | S12 |
| connections | C1 | C1 |
| connections | C2 | C2 |
| connections | C3 | C3 |
| connections | C4 | C4 |
| connections | C5 | C5 |
| connections | C6 | C6 |
| connections | C7 | C7 |
| connections | C8 | C8 |
| connections | C9 | C9 |
| connections | C10 | C10 |
| connections | C11 | C11 |
| connections | C12 | C12 |
| connections | C13 | C13 |
| relay | R1 | R1 |
| relay | R2 | R2 |
| relay | R3 | R3 |
| relay | R4 | R4 |
| relay | R5 | R5 |
| relay | R6 | R6 |
| relay | R7 | R7 |
| relay | R8 | R8 |
| relay | R9 | R9 |
| relay | R10 | R10 |
| relay | F1 | F1 |
| relay | F2 | F2 |
| relay | F3 | F3 |
| relay | F4 | F4 |
| relay | F5 | F5 |
| relay | F6 | F6 |

Replace the documented placeholders using the [parameter table](install.md#parameters).
Commands run as your SSH account on the target unless marked as workstation or browser actions.
Use the documented defaults below, or consistently substitute your own installation paths.


Create secrets on the target in `/opt/hullgate/secrets` (or your chosen `<secrets-dir>`),
never in tracked files. Preserve encrypted recovery copies separately from database backups.
Only UID/GID `10001:10001` may read the mounted files, mode `0400`; verify their metadata,
not readability as root. S2, S3, S4, C2 and C3 run before installation O6 and need no image.
Other steps use the running deployment. Use service accounts for upstreams.

## Step S1 — Read the selected reference

```sh
cd /opt/hullgate && git log --oneline -1 && git show HEAD:deploy/compose.prod.yaml | grep -c signing_key
```

**Expected result**: the selected commit and at least two signing-key references.

**If the result differs**: select a release containing the deployment composition before creating secrets.

## Step S2 — Create the TOTP secret and enroll it in your app

```sh
cd /opt/hullgate && install -d -m 0700 secrets && test ! -e secrets/authorize_totp && (umask 077; head -c 20 /dev/urandom | base32 | tr -d '=\n' > secrets/authorize_totp) && printf 'Enter in your authenticator (TOTP, SHA1, 6 digits, 30 s) : %s\nURI : otpauth://totp/hullgate%%3Aserver?secret=%s&issuer=hullgate\n' "$(cat secrets/authorize_totp)" "$(cat secrets/authorize_totp)"
```

**Expected result**: two lines, the secret in base32 (32 characters `A-Z2-7`) then the `otpauth://` URI. Add an account in your authenticator app (manual entry of the secret, or the URI); it displays a 6-digit code that changes every 30 s. Keep the secret and codes private; clear the terminal after enrolment.

**If the result differs**: nothing is displayed and `test` failed → `secrets/authorize_totp` already exists: do not overwrite it, it may already be enrolled; check with `ls -l secrets/`. `base32: command not found` → install coreutils before continuing.


## Step S3 — Create and protect the signing key

```sh
(
cd /opt/hullgate && test -s secrets/authorize_totp && test ! -e secrets/signing_key &&
  (umask 077; openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out secrets/signing_key) &&
sudo chown 10001:10001 secrets/signing_key secrets/authorize_totp &&
sudo chmod 0400 secrets/signing_key secrets/authorize_totp &&
stat -c '%u:%g %a' secrets/signing_key secrets/authorize_totp
)
```

**Expected result**: two lines `10001:10001 400`. Save recovery copies in your secrets manager,
using its secure import flow; do not display key contents as a permission test.

**If the result differs**: an existing key must not be overwritten. Fix ownership and permissions
before deploying; install OpenSSL if missing. Rotating the signing key invalidates outstanding
access tokens; schedule the change, keep the recovery copy and require clients to renew.
For TOTP rotation, enrol the replacement in your authenticator before protecting it and redeploying;
the old codes then stop working. Restore the original files from recovery copies if lost.

## Step S4 — Declare signing and TOTP paths

```sh
cd /opt/hullgate &&
${EDITOR:-nano} .env &&
grep -E '^(SIGNING_KEY_PATH|AUTHORIZE_TOTP_PATH)=' .env &&
test "$(grep -c '^SIGNING_KEY_PATH=' .env)" = 1 &&
test "$(grep -c '^AUTHORIZE_TOTP_PATH=' .env)" = 1 &&
docker compose -f deploy/compose.prod.yaml --env-file .env config --quiet && echo 'CONFIG OK'
```

**Expected result**: `SIGNING_KEY_PATH=/opt/hullgate/secrets/signing_key` and
`AUTHORIZE_TOTP_PATH=/opt/hullgate/secrets/authorize_totp` (or your chosen absolute paths), then
`CONFIG OK`. Set these existing keys in `.env`; do not add duplicate declarations.

**If the result differs**: correct the paths and the required Traefik values (install O5), then retry.

## Step S5 — Deploy the core reference

Replace `<ref>` with the SHA noted in S1:

```sh
cd /opt/hullgate && time make deploy REF=<ref>
```

**Expected result**: `BACKUP: OK …`, the build, `DOWNTIME <s>` then `DEPLOY: OK <ref>`. The F-004 database (no schema) is migrated at startup (migrations 1 and 2).

**If the result differs**: `DEPLOY: KO … /readyz ≠ 200` → look at the 20 log lines displayed: `invalid signing key` or `signing key: EC P-256 expected` → S3 (permissions or file); `schema` → record the diagnostic locally with the line. In all cases the rollback is printed (`Rollback to the previous ref: make deploy REF=…`): the F-004 reference does not mount the secrets and restarts without them.


## Step S6 — Foundation checks

```sh
cd /opt/hullgate && make verify
```

**Expected result**: `VERIFY: 6/6 OK` (`readyz-public` 200 now also means "key loaded"; `restore-check` starts the copy with a throwaway key generated in its throwaway volume).

**If the result differs**: `readyz-public KO … 503` → `docker compose -f deploy/compose.prod.yaml --env-file .env exec hullgate python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:4790/readyz').read())"` gives the reason (`key_unavailable` → S3). Other lines: `install.md`, step O7.


## Step S7 — Create a test tree through the CLI and read a role's path

```sh
cd /opt/hullgate && C="docker compose -f deploy/compose.prod.yaml --env-file .env exec -T hullgate hullgate admin" && $C tree add / client "Recette socle" && $C tree add recette-socle project "Essai" && $C tree add recette-socle/essai environment "Dev" && $C tree show
```

**Expected result**: three lines `created: …` (the environment also creates its `principal` role), then the tree: the root line `/  « Organisation »  organisation  <org-id>`, `<org-id>` being the organisation's public identifier (12 characters, `a`-`z` and `2`-`7`, F-014), and a role line `principal « principal » role non-sensitive https://<MCP_HOST>/o/<org-id>/p/recette-socle/essai/dev/principal/mcp`. The role's path is the part between `/p/` and `/mcp`: `recette-socle/essai/dev/principal`; the instruments of S8 take that path, never the identifier.

**If the result differs**: `refused: segment already used` → the tree already exists (this runbook replayed): go to `$C tree show`. `database unavailable` → S5/S6.


## Step S8 — Core checks

Replace `<role-path>` with the path read in S7 (never an imposed path):

```sh
cd /opt/hullgate && make verify-auth ROLE=<role-path>
```

**Expected result**: four lines `VERIFY-AUTH key-loaded OK`, `admin-routes OK`, `discovery OK https://<MCP_HOST>/o/<org-id>/p/<role-path>/mcp`, `challenge OK 401 resource_metadata`, then `VERIFY-AUTH: 4/4 OK`. The instrument reads `<org-id>` itself from `tree show / --json` (key `public_id`).

**If the result differs**: `key-loaded KO` → S3 then S6. `admin-routes KO` → the administration answers something other than 404 at `/admin`: `make logs` and record the diagnostic locally. `discovery KO 404` → wrong path: reread S7. `discovery KO … resource=` another URL → the `MCP_HOST` of `.env` does not match the name served by Traefik. `challenge KO` → the line shows the status and the header received: record the diagnostic locally. `ROLE absent or malformed` → `ROLE=` must have four segments. `organisation identifier unreadable` → `$C tree show / --json` gives no `public_id`: the container runs code older than F-014, or its database is unreadable (S5/S6).


## Step S9 — Pre-register the Claude client on the test tree

```sh
cd /opt/hullgate && C="docker compose -f deploy/compose.prod.yaml --env-file .env exec -T hullgate hullgate admin" && $C client add claude-operator --path recette-socle --redirect-uri https://claude.ai/api/mcp/auth_callback --redirect-uri https://claude.com/api/mcp/auth_callback --name "Claude (operator)" && $C client list
```

**Expected result**: `client preregistered: claude-operator on recette-socle`, the two return addresses, then the list (`claude-operator  preregistered  recette-socle  active  …`).

**If the result differs**: `client already registered` → already done: go to S10.


## Step S10 — Connect Claude.ai to the test role through OAuth (reading, no threshold)

In Claude.ai: Settings → Connectors → Add custom connector.

| Field | Value |
|-------|-------|
| URL | `https://<MCP_HOST>/o/<org-id>/p/<role-path>/mcp` (S7) |
| Advanced settings → OAuth Client ID | `claude-operator` |
| Advanced settings → OAuth Client Secret | leave empty |

Click "Connect": a tab opens on the gateway's "Authorize access" page (client, role, sensitivity). Type the 6-digit code from your app, then "Authorize". Then read what the gateway saw:

```sh
cd /opt/hullgate && C="docker compose -f deploy/compose.prod.yaml --env-file .env exec -T hullgate hullgate admin" && $C token list --client recette-socle && $C audit tail -n 15
```

**Expected result**: a line `lin_…  oauth  claude-operator  recette-socle/essai/dev/principal  …  active`; in the audit, `oauth.code.issue` then `oauth.token.issue` (`"grant": "authorization_code"`). The connector appears "connected" in Claude.ai, with no tool (no tool in F-005). If Claude.ai identifies its client otherwise (metadata document `https://…` or dynamic registration), the audit shows it in `actor`: it is a local observation, not a failure. After one hour, repeat the audit block: `oauth.token.issue` with `"grant": "refresh_token"` means the client renewed with `resource`; `oauth.token refused invalid_target` means it omitted it (BR-058).

**If the result differs**: "Code refused" page → expired code: wait for the next one (a code is good for one use only). "Barrier closed" (429) → five wrong codes in 15 min: wait 15 min. "Client unknown to this access point" page → Claude.ai did not send `claude-operator`: read the `actor` of the `oauth.authorize refused unknown_client` line of the audit and record the diagnostic locally; `$C client dcr on` would allow dynamic registration (still behind the barrier) — an operator decision, not this runbook's. `invalid_target` → Claude.ai does not send `resource`: a local observation.


**Reconnection after F-014's deployment** (the URL scheme `/o/<org-id>/…`): the connector is reconnected, not migrated — its URL lives on Claude's side and its tokens are bound to the old URL, which now answers 404. Before the deployment, read the role's path and the old URL in `$C tree show`; after it, read the new URL there (root line identifier, role line URL). Then `$C client list` gives the connector client's kind (a reading: the kind is a hypothesis until read). On Claude.ai, remove the connector, add a custom connector with the new URL and pass the barrier as above; Claude iOS follows by account (S11). A CIMD or pre-registered client needs no `client add`; a `dcr` client re-registers behind the barrier. Check the old URL answers 404 without a `location:` header: `curl -sS -o /dev/null -D - https://<MCP_HOST>/p/<role-path>/mcp`.

## Step S11 — Claude iOS (if available, reading)

Open the Claude app on iOS with the same account: the connector added in S10 appears there. Open a conversation and enable it.

```sh
cd /opt/hullgate && docker compose -f deploy/compose.prod.yaml --env-file .env exec -T hullgate hullgate admin audit tail -n 10
```

**Expected result**: either nothing new (iOS reuses Claude.ai's authorization), or a new `oauth.code.issue` / `oauth.token.issue` sequence after a new barrier (then type a code).

**If the result differs**: the app does not display the connector → a local observation, nothing to fix here.


## Step S12 — Remove the test access (optional)

```sh
cd /opt/hullgate && C="docker compose -f deploy/compose.prod.yaml --env-file .env exec -T hullgate hullgate admin" && $C client remove claude-operator && $C tree delete recette-socle --confirm recette-socle
```

**Expected result**: `client removed: claude-operator`, `OAuth lineages revoked: <n>`, then the counts and `deleted: recette-socle`. The Claude.ai connector stops responding (401, then 404).

**If the result differs**: `locked by …` → a lock was set: `$C tree unlock <node>` then paste again.


## Connections

C1–C13 configure encrypted upstream connections after the core is running. Create the
connections key at C2 before the first deployment (install O5); on an existing instance,
keep its original key. Keep recovery copies outside the target using your secrets manager.
Loss of that key makes every encrypted upstream token unreadable; C12 covers recovery.
Use a dedicated test or service account at the provider, never your personal identity.

## Step C1 — Read the state and choose the reference

```sh
cd /opt/hullgate && make status && git fetch origin && git log --oneline -5 origin/main && git show origin/main:deploy/compose.prod.yaml | grep -c 'connections_key'
```

**Expected result**: the `hullgate` container `Up … (healthy)`, the deployed reference, the last five commits of `main` (the first is usually the merge of F-006 lot B), then a number ≥ 3 (the composition on `main` mounts the connections key).

**If the result differs**: `0` on the last line → the lot B PR is not merged: stop here. `.env absent` → you are not in the clone.


## Step C2 — Create the connections key and protect it

```sh
(
cd /opt/hullgate && install -d -m 0700 secrets && test ! -e secrets/connections_key &&
  (umask 077; head -c 32 /dev/urandom | base64 > secrets/connections_key) &&
sudo chown 10001:10001 secrets/connections_key &&
sudo chmod 0400 secrets/connections_key &&
stat -c '%u:%g %a' secrets/connections_key
)
```

**Expected result**: `10001:10001 400`. Keep a secure recovery copy outside the target, with the
corresponding database backup. Only the service identity reads the key.

**If the result differs**: an existing key must not be overwritten: check its metadata and C5.
Correct the owner and mode before deployment. Loss without a recovery copy requires C12;
rotation also requires removing encrypted connections/application secrets, forgetting the old
key check, and reauthorizing under the new key. A different key cannot decrypt existing tokens.

## Step C3 — Declare the connections key path

```sh
cd /opt/hullgate &&
${EDITOR:-nano} .env &&
grep '^CONNECTIONS_KEY_PATH=' .env &&
test "$(grep -c '^CONNECTIONS_KEY_PATH=' .env)" = 1 &&
docker compose -f deploy/compose.prod.yaml --env-file .env config --quiet && echo 'CONFIG OK'
```

**Expected result**: `CONNECTIONS_KEY_PATH=/opt/hullgate/secrets/connections_key` (or the absolute
path you chose) and `CONFIG OK`. Set the existing key once, with no duplicate lines.

**If the result differs**: match the path to C2; do not deploy before `CONFIG OK`.

## Step C4 — Deploy the F-006 reference

Replace `<ref>` with the SHA noted in C1:

```sh
cd /opt/hullgate && time make deploy REF=<ref>
```

**Expected result**: `BACKUP: OK …`, the build, `DOWNTIME <s>` then `DEPLOY: OK <ref>`. The core database is migrated at startup (migrations 3 and 4: connection tables).

**If the result differs**: `DEPLOY: KO …` → look at the 20 log lines displayed; `invalid mount config` or `bind source path does not exist` → C2/C3. The way back is printed (`Rollback to the previous ref: make deploy REF=…`).


## Step C5 — Check that the key is loaded (after every deployment)

```sh
cd /opt/hullgate && make verify && docker compose -f deploy/compose.prod.yaml --env-file .env exec -T hullgate hullgate admin connection status
```

**Expected result**: `VERIFY: 6/6 OK`, then the last line `connections key: usable` (preceded by one line per connection if any already exist). `/readyz` stays green even without a key (BR-088): **only this line tells whether the key is loaded**.

**If the result differs**: `connections key: key_absent` → the file is not mounted or is not readable by 10001: C2 (permissions) then C3, then `make deploy REF=<ref>`. `key_invalid` → the file does not contain 32 bytes in base64: it was modified; if no connection exists yet, recreate it (C2 after `sudo rm secrets/connections_key`). `key_different` → the database has already encrypted under another key: restore the original file (backup copy), otherwise C12.


## Step C6 — Declare the Gitea service

Replace `<gitea-host>` with the name of your Gitea (without `https://`) and `<scopes>` with the scopes of the test account, separated by commas (`openid` is added by the model; §12: at Gitea, a scope outside the list means full access, choose them with care):

```sh
cd /opt/hullgate && C="docker compose -f deploy/compose.prod.yaml --env-file .env exec -T hullgate hullgate admin" && $C service declare gitea --server https://<gitea-host> --model gitea --scopes <scopes> && $C service list
```

**Expected result**: `service declared: gitea (gitea) https://<gitea-host>`, the scopes, then the discovered endpoints (`authorization_endpoint`, `token_endpoint`, `userinfo_endpoint`, and `revocation_endpoint` if announced), repeated by `service list`.

**If the result differs**: `discovery impossible: address refused: 10.…` (or 172.16–31, 192.168) → Gitea is reached by a private address: rerun the same command with `--internal-host` at the end; the output then shows the admitted addresses. `address refused: 127.…` or `169.254.…` → never admitted, even as an internal host: record the diagnostic locally. `discovery impossible: deadline exceeded` or `connection interrupted` → Gitea unreachable from the container: `$C service list` then record the diagnostic locally. `service already declared` → already done: go to C7.


## Step C7 — Create the OAuth application at Gitea and declare it

At Gitea, logged in as `hullgate-test`, the account that owns the application: **Settings → Applications → Manage OAuth2 Applications**, name `hullgate`, redirect URI **exactly** `http://127.0.0.1/oauth/callback`, box "Confidential client" **unchecked** (public PKCE client). Note the client identifier shown, then replace `<client-id>`:

```sh
cd /opt/hullgate && C="docker compose -f deploy/compose.prod.yaml --env-file .env exec -T hullgate hullgate admin" && $C application declare gitea-app --service gitea --mode declared --client-id <client-id> && $C application list
```

**Expected result**: `application declared: gitea-app (service gitea, declared)`, the `client_id`, `redirect_uri to enter at the provider: http://127.0.0.1/oauth/callback`, `secret: none (public client)`.

**If the result differs**: Gitea only offers the confidential client → check it, copy the secret shown into a file `secrets/gitea-app.secret` (`umask 077`), then replace the command with `… --client-id <client-id> --secret-file - < secrets/gitea-app.secret && shred -u secrets/gitea-app.secret`. `connections key unavailable` → C5.


## Step C8 — Connect the test Gitea account by OAuth

```sh
cd /opt/hullgate && C="docker compose -f deploy/compose.prod.yaml --env-file .env exec -T hullgate hullgate admin" && $C connection add recette-socle/gitea-test --service gitea --application gitea-app --prefix gt
```

Open the printed URL in a browser **logged in to Gitea under the test account** (private window if your personal account is open there), authorize. The browser shows a connection error toward `127.0.0.1`: that is expected. Copy the whole URL from its address bar, then, on the server, within 10 minutes:

```sh
cd /opt/hullgate && (umask 077; cat > retour.txt) && C="docker compose -f deploy/compose.prod.yaml --env-file .env exec -T hullgate hullgate admin" && $C connection authorize recette-socle/gitea-test --callback - < retour.txt; shred -u retour.txt
```

(Paste the URL, Enter, then `Ctrl-D` to end `cat`.)

**Expected result**: `connection created: recette-socle/gitea-test (oauth), state pending` and the URL; then `authorized: recette-socle/gitea-test, account <account>, scopes …, expires …`.

**If the result differs**: `state expired` or `state already used` → rerun `$C connection reauthorize recette-socle/gitea-test` and redo the second block with the new URL. `refusal from the provider: access_denied` → you refused at Gitea: same. `code exchange refused: invalid_grant` → different `redirect_uri` at Gitea (C7) or code already used: same. The `account` shown is your personal account → `$C connection remove recette-socle/gitea-test --confirm recette-socle/gitea-test` and start again with the test account. **Keep the return URL private**: it carries the code.


## Step C9 — Place the connection on the trial role and read its name

```sh
cd /opt/hullgate && C="docker compose -f deploy/compose.prod.yaml --env-file .env exec -T hullgate hullgate admin" && $C connection place recette-socle/gitea-test recette-socle/essai/dev/principal && $C connection list && $C connection status
```

**Expected result**: `placed: recette-socle/gitea-test on recette-socle/essai/dev/principal (non-sensitive)`, then the `connection list` line (`recette-socle/gitea-test  service gitea  prefix gt  oauth  state active`), then `connection status` (account, scopes, expires, placement) and `connections key: usable`. The connection name is the first column of `connection list`.

**If the result differs**: `placement: a connection is placed on a role` → wrong path: reread `$C tree show`. `locked by …` → the test tree is locked: `$C tree unlock <node>`.


## Step C10 — Connection checks

Replace `<connection-name>` with the name read in C9 from `connection list` (never an imposed name):

```sh
cd /opt/hullgate && make verify-connections CONNECTION=<connection-name>
```

**Expected result**: the lines `VERIFY-CONNECTIONS state: OK`, `token: OK`, `renewal: OK`, `output: OK`, `reading …` lines (`account`, `gitea-api`, deadlines, `refresh_token`, rotation, version), then the banner `VERIFY-CONNECTIONS: 4/4 OK`. No token is displayed.

**If the result differs**: `state: KO state …` → C8. `token: KO access_key_unavailable` → C5. `renewal: KO …` → record the diagnostic locally (provider unavailable, or Gitea without `refresh_token`). `instrument not run` → container stopped: `make status`. `CONNECTION absent or malformed` → `CONNECTION=` must have the form `<client>/<name>`.


## Step C11 — Inspect provider readings (no threshold)

```sh
cd /opt/hullgate && make verify-connections CONNECTION=<connection-name> | grep -E '^VERIFY-CONNECTIONS reading '
```

**Expected result**: `reading gitea-api: <status-code>` (200: Gitea accepts the OAuth token as `Authorization: Bearer`, matrix path 2), the account, the deadlines before and after renewal (seconds remaining: Gitea's `expires_in`), the presence of the `refresh_token`, its rotation (`yes` / `no`) and the Gitea version.

**If the result differs**: `reading gitea-api: 401` → Gitea refuses the OAuth token through this channel: note it down, the provided-token connection (`connection add … --provided --file -`) becomes the fallback locally. `reading version: failed …` → no effect on the checks.


## Step C12 — Lost key: recovery gesture (only if needed)

A database that has encrypted under a key refuses any other key (`key_different`, BR-088), even after all connections are removed: the key check remains. First, **restore the original key** if you have the copy (C2): nothing else needs doing. Otherwise, remove each connection and each application with a secret (their tokens are unreadable anyway), then forget the key check:

```sh
cd /opt/hullgate && C="docker compose -f deploy/compose.prod.yaml --env-file .env exec -T hullgate" && $C hullgate admin connection list && $C python -m hullgate.keys connections-key-forget --confirm
```

**Expected result**: `key check forgotten: the next key is adopted at the first ciphertext`. Then recreate the key (C2 after `sudo rm secrets/connections_key`), redeploy (C4), check (C5), then reauthorize each connection (C8 and C9).

**If the result differs**: `refusal: ciphertexts still live (connection: n, application: m …)` → remove them first (`connection remove <c> --confirm <c>`, `application remove <org-id>`); nothing was written.


## Step C13 — Remove the test connection (optional)

```sh
cd /opt/hullgate && C="docker compose -f deploy/compose.prod.yaml --env-file .env exec -T hullgate hullgate admin" && $C connection revoke recette-socle/gitea-test && $C connection remove recette-socle/gitea-test --confirm recette-socle/gitea-test
```

**Expected result**: `connection revoked: recette-socle/gitea-test, reauthorization required` with `revocation at the provider: revoked` (or `unsupported` if Gitea does not announce a `revocation_endpoint`: then revoke the authorization at Gitea, Settings → Applications → Authorized OAuth2 Applications), then `connection removed: recette-socle/gitea-test`.

**If the result differs**: `locked by …` on removal → `$C tree unlock <node>` then start again; `connection revoke` remains allowed under a lock.


## Relay and upstreams

R1–R10 start the bundled Gitea MCP upstream on the private upstream network, declare it,
place a connection, and verify listing, calls, refusal, sensitive-role isolation and output
masking. Complete the core and connection steps first. Other MCP upstreams use the same
`service upstream` command with their own URL and type; a public upstream requires HTTPS,
while the explicit internal-host exception requires private resolved addresses.

Run server blocks as your SSH account in `/opt/hullgate`:

```sh
ssh -p <ssh-port> <user>@<server>
```

If connected under another account, switch with `sudo -iu <user>` before continuing.

**Copy-paste.** Each block is written in short lines; the values to replace are set once at the top of the block (`R=…`, `C=…`, `ROLE=…`). Paste the whole block, line by line if need be: a line cut at pasting breaks only that line, which you replay.

Keep token values on the target and pass them through standard input as shown below.

**Since F-015 (action policy), a placement exposes only what its policy allows**: on a non-sensitive role, the tools whose class is within the mode (`write` by default, so a tool without annotations, `destructive` by default, is hidden until it is classified or opened); a connection placed on at least one sensitive role exposes, on every one of its roles, only the reads of its `allowlist` and its `open` tools (BR-175, BR-186). Read what a role sees with `$C policy preview <role>` before plugging a client in.

**stdio bridge (`mcp-internal`)**: a stdio → HTTP bridge without authentication must **never answer 401**. A 401 from a provided-token connection puts it in `reauth_required` at the first call (BR-127, H22): it would deactivate itself. Configure the bridge to ignore the `Authorization` header without refusing it.

Each step: a block to paste, the expected result, what to do if it differs.

Shortcut common to steps R5 to R10 (to paste again in each new shell):

```sh
cd /opt/hullgate
F="-f deploy/compose.prod.yaml --env-file .env"
C="docker compose $F exec -T hullgate hullgate admin"
```

---

## Step R1 — Read the state and choose the reference

```sh
cd /opt/hullgate
make status
git fetch origin
git log --oneline -5 origin/main
git show origin/main:deploy/upstreams.compose.yaml | grep -c 'gitea-mcp'
```

**Expected result**: the `hullgate` container `Up … (healthy)`, the deployed reference, the last five commits of `main` (the first is usually the merge of the batch to verify), then a number ≥ 2.

**If the result differs**: `fatal: path 'deploy/upstreams.compose.yaml' does not exist` → the PR of batch B is not merged: stop there. `.env absent` → you are not in the clone.


## Step R2 — Declare the Gitea URL for gitea-mcp in .env

Set `H=` to the name of your Gitea, **without** `https://` (for example `gitea.example.net`):

```sh
cd /opt/hullgate
H=<gitea-host>   # replace with your Gitea hostname
grep -q '^GITEA_URL=' .env || printf 'GITEA_URL=https://%s\n' "$H" >> .env
grep -q '^UPSTREAMS_NETWORK=' .env || printf 'UPSTREAMS_NETWORK=hullgate_upstreams\n' >> .env
grep -E '^(GITEA_URL|UPSTREAMS_NETWORK)=' .env
grep -n '<[A-Z_]*>' .env || echo "no placeholder"
```

**Expected result**: `GITEA_URL=https://<gitea-host>`, `UPSTREAMS_NETWORK=hullgate_upstreams`, then `no placeholder`.

**If the result differs**: a line `GITEA_URL=https://<gitea-host>` (or any other value between `<` and `>`) is a template left as is (seen on 2026-10-31): fix it (`${EDITOR:-nano} .env`), then replay the last two lines of the block. A wrong value → fix `.env` the same way.


## Step R3 — Deploy the reference

Set `R=` to the SHA noted in R1:

```sh
cd /opt/hullgate
R=<ref>
time make deploy REF=$R
make verify
docker network ls --format '{{.Name}}' | grep -x hullgate_upstreams
```

**Expected result**: `BACKUP: OK …`, the build, `DOWNTIME <s>`, `DEPLOY: OK <ref>` (migration 5 at startup: services' MCP upstream, tool-name table; no migration at batch C), `VERIFY: 6/6 OK`, then `hullgate_upstreams` (upstream network created by the composition).

**If the result differs**: `DEPLOY: KO …` → look at the 20 log lines displayed; the rollback is printed (`Rollback to the previous ref: make deploy REF=…`). No `hullgate_upstreams` line → the deployed reference is not that of batch B or later (R1).


## Step R4 — Start gitea-mcp 1.11.0 on the upstream network

```sh
cd /opt/hullgate
A="-f deploy/upstreams.compose.yaml --env-file .env"
D=sha256:ccdb5660ecdf54c5e480f10cc8120ba2bf150b582cea8a1974de2e59883582d3
I=docker.gitea.com/gitea-mcp-server@$D
docker compose $A up -d
sleep 3
docker compose $A ps
docker image inspect "$I" --format '{{.Created}}'
docker network inspect hullgate_upstreams --format '{{range .Containers}}{{.Name}} {{end}}'
```

**Expected result**: the `gitea-mcp` service `Up`, the image date (2026-10-22, version 1.11.0), then exactly two containers on `hullgate_upstreams`: the gateway and `gitea-mcp` (no other service: the traffic from the relay to `gitea-mcp`, token included, is in clear on this network, F-008 §12).

**If the result differs**: `GITEA_URL required` → R2. `network hullgate_upstreams declared as external, but could not be found` → R3. A third container on the network → remove it from this network before continuing. `gitea-mcp` restarting in a loop → `docker compose $A logs --tail 30 gitea-mcp`, and inspect the diagnostic locally.


## Step R5 — Declare the MCP upstream of the Gitea service

The `gitea` service is the one from `keys-and-upstreams.md` (C6). The internal-host exception is named here for the MCP URL alone (BR-106); its addresses must be **all private**:

```sh
cd /opt/hullgate
F="-f deploy/compose.prod.yaml --env-file .env"
C="docker compose $F exec -T hullgate hullgate admin"
U=http://gitea-mcp:8080/mcp
$C service upstream gitea --type mcp-public --mcp-url $U --mcp-internal-host
$C service list
```

**Expected result**: `upstream declared: gitea mcp-public http://gitea-mcp:8080/mcp` with the admitted addresses displayed (one or more `172.…` or `10.…` addresses), then, under the `gitea` service's card, the line `  upstream: mcp-public gitea-mcp  internal host` (services without an upstream carry `  no MCP upstream declared`).

**If the result differs**: `address refused` or `public address: https required` → `gitea-mcp` is not resolved to a private address of the upstream network: R4. `upstream already declared` → an upstream exists: `$C service list`; to change it, add `--confirm gitea`.


## Step R6 — Place a connection on a trial non-sensitive role and read its tools

Set `N=` to the connection read from `$C connection list` (the one from C9, for example) and `ROLE=` to a **non-sensitive** role read from `$C tree show`. The connection must be placed on **no** sensitive role (BR-186: otherwise its placement is sensitive and exposes nothing without a list):

```sh
cd /opt/hullgate
F="-f deploy/compose.prod.yaml --env-file .env"
C="docker compose $F exec -T hullgate hullgate admin"
N=<client>/<connection>
ROLE=<client>/<project>/<env>/<role>
$C connection status $N
$C connection tools $N --role $ROLE
```

**Expected result**: `connection status` displays `upstream: mcp-public gitea-mcp`; `connection tools` lists the tools of `gitea-mcp` with their public name `<prefix>__<tool>` (for example `gt__issue_read`) and the negotiated version.

**If the result differs**: `not placed` → `$C connection place $N $ROLE`. Tools listed but none exposed in `$C policy preview $ROLE` (reason `not_allowlisted`) → the connection is also placed on a sensitive role (BR-186): choose another connection, remove the sensitive placement (`$C connection unplace $N <sensitive-role>`), or configure its lists (F-015 block, F4). `no MCP upstream declared` → R5. `401 from the MCP server` (code 1) → `gitea-mcp` refuses the connection's token: record it (hypothesis of F-008 §12, route 2 of the matrix) and inspect the diagnostic locally.


## Step R7 — Relay checks

Set `ROLE=` to the role from R6 and `TOOL=` to a **reading** tool read from the R6 output (for example `gt__get_me`). The short-lived operator tokens go through the standard input of `make verify-relay` (line 1: token of `ROLE`, line 2: token of the sensitive role), never on the command line. `token create --json` prints only the value extracted by `sed`.

**Without a sensitive role** (the case of the trial tree): a single token.

```sh
cd /opt/hullgate
F="-f deploy/compose.prod.yaml --env-file .env"
C="docker compose $F exec -T hullgate hullgate admin"
ROLE=<client>/<project>/<env>/<role>
TOOL=gt__get_me
V='s/.*"value": "\([^"]*\)".*/\1/p'
$C token create --path $ROLE --label verify-relay --duration 1h --json \
  | sed -n "$V" \
  | make verify-relay ROLE=$ROLE TOOL=$TOOL
```

**With a sensitive role**: run this block in place of the previous one (two tokens, in order); it sets its variables again, so that a fresh shell does not issue a useless token. Also set `SENS=` to a sensitive role read from `$C tree show`:

```sh
cd /opt/hullgate
F="-f deploy/compose.prod.yaml --env-file .env"
C="docker compose $F exec -T hullgate hullgate admin"
ROLE=<client>/<project>/<env>/<role>
TOOL=gt__get_me
V='s/.*"value": "\([^"]*\)".*/\1/p'
SENS=<client>/<project>/<sensitive-env>/<role>
{ $C token create --path $ROLE --label verify-relay --duration 1h --json | sed -n "$V"
  $C token create --path $SENS --label verify-relay --duration 1h --json | sed -n "$V"
} | make verify-relay ROLE=$ROLE SENSITIVE=$SENS TOOL=$TOOL
```

**Expected result**: `VERIFY-RELAY list: OK`, lines `reading tool: <public-tool>`, `call: OK`, `refusal: OK`, `sensitive: OK` (or `SKIP no sensitive role named` with the first block), `output: OK`, readings `reading …` (tool count, time, negotiated version, addresses), then the banner `VERIFY-RELAY: 5/5 OK` (or `VERIFY-RELAY: 4/5 OK` with `sensitive: SKIP`). No token is displayed.

**If the result differs**: `list: KO no tool` → R5, R6. `call: KO isError (connection_reauth_required)` → `gitea-mcp` answered 401: `$C connection status $N` says `mcp_refusal_401`; record the diagnostic locally. `call: KO TOOL absent` → `TOOL=` empty. `sensitive: KO sensitive role token absent` → second token missing (second block). `ROLE absent or malformed` or `operator token absent` → a line cut at pasting: set the variables again and replay the block. `instrument not run` → `make status`.

**Revoke the trial tokens** (one hour of life, but revoke them right away):

```sh
$C token list $ROLE | grep verify-relay
```

Each line starts with the token's identifier, then its type, its label (`verify-relay`), its path, its dates and its state (`active`). Set `J=` to the identifier of an `active` line, then:

```sh
J=<token-id>
$C token revoke $J
```

Replay for each token (and for `$SENS` if there is one: `$C token list $SENS | grep verify-relay`). The identifier can also be read in `$C audit tail -n 20` (`token.issue` entries).


## Step R8 — Inspect relay readings (no threshold)

```sh
cd /opt/hullgate
F="-f deploy/compose.prod.yaml --env-file .env"
C="docker compose $F exec -T hullgate hullgate admin"
$C audit tail -n 50 | grep -E 'relay\.(tool_call|list_omitted)'
```

**Expected result**: the latest relay audit entries (actor, role, connection, public name and upstream, negotiated version, outcome, duration, sizes), with no argument, result or token. With the readings from R7: tool count and `list` time, `call` time, negotiated protocol version, `gitea-mcp` version (R4), length of the longest public name, addresses of `gitea-mcp` (IPv4 or IPv6 on a dual-stack host).

**If the result differs**: no `relay.tool_call` entry → the R7 call did not reach the relay: reread R7.


## Step R9 — Claude.ai and Claude iOS (clause read by hand)

```text
1. Claude.ai > Settings > Connectors: reconnect the gateway connector using the R6 role URL.
2. Open a conversation: the role tools <prefix>__… appear in the connector.
3. Request a read (for example, read my Gitea profile): the call succeeds.
4. Claude iOS: open a conversation, add the connector and repeat step 3.
```

**Expected result**: on each client, the role's tools appear, the connector can be added to a conversation, a read call succeeds; the output of R7 contains no connection token value (read it).

**If the result differs**: tools absent in Claude.ai → disconnect and reconnect the connector (the list is reread at connection, F-008 §12 `list_changed`); failure on the client side only → note `client-side failure` and what you saw.


## Step R10 — Batch C: progress, cancellation, client non-regression (AC-OP replayed)

Batch C serves a `tools/call` that carries a `progressToken` **as a stream (SSE)**, from a client that accepts `text/event-stream`; any other request stays JSON as in batch B. When a client closes the connection of a call in progress, the upstream call is cancelled (`notifications/cancelled`, cut) and audited `cancelled`. Nothing else changes: no migration, no configuration, no `gitea-mcp`.

**What `gitea-mcp` 1.11.0 lets you see.** Its binary carries only the progress types of its MCP library (`ProgressNotificationParams`), no emission call found: **none of its tools is known to emit progress**. On the server, the step therefore judges **non-regression** (clients keep listing and calling, whether their request goes out as a stream or not) and notes whether the client sends a `progressToken` (`stream` field of the audit); progress itself is proven by the session's tests (AC-25), not on the server.

1. Deploy the batch C reference: R1 (SHA of the batch C merge), R3. R2, R4, R5 and R6 are not replayed (nothing changes), unless R3 failed.
2. Replay R7 (first block): expected banner `VERIFY-RELAY: 4/5 OK` with `sensitive: SKIP` (or `5/5 OK`), as in batch B. Revoke the tokens.
3. Replay R9 on Claude.ai then on Claude iOS, with a read **and** a somewhat long read (for example "list the open tickets of <repository>").
4. Note how the clients' calls were served:

```sh
cd /opt/hullgate
F="-f deploy/compose.prod.yaml --env-file .env"
C="docker compose $F exec -T hullgate hullgate admin"
$C audit tail -n 30 | grep 'relay.tool_call'
```

**Expected result**: R7 as in batch B; on each client, tools visible, connector added, calls succeeded; in the audit, one `relay.tool_call` entry per client call, outcome `ok`, with `"stream": true` if the client sent a `progressToken` (response served as a stream), without this field otherwise. An entry with outcome `cancelled` appears only if a call was interrupted on the client side (conversation closed, network loss): it is expected in that case, and only in that case.

**If the result differs**: a client that no longer displays a result, or an error on the client side, while the audit says `ok` with `"stream": true` → the client reacts badly to the stream: note `client-side failure` and what you saw; F-008 §12 then provides for **abandoning or deferring batch C** (return to the batch B reference with `make deploy REF=<previous-ref>`, without touching batches A and B). `cancelled` entries without any interruption on your part → retain their timestamps for diagnosis.


---

## F-015 block — action policy on a real Gitea connection (AC-OP of F-015)

What changes with F-015 (lots A, B and C, deployed together: never one lot alone, spec §9 *Upgrade window*):

- every tool has a class, `read`, `write` or `destructive`, taken from its manual classification, else its MCP annotations, else `destructive` (BR-171). `gitea-mcp` 1.11.0 publishes 61 tools: 36 `read`, 25 `destructive`, **none `write`** (spec §12);
- at the default mode `write`, a non-sensitive role keeps the 36 reads and **loses every Gitea write** (`issue_write`, `pull_request_write`, `create_or_update_file`, …) until you classify or open it. The Claude.ai connector live on the gateway (`gt__`) is concerned from the deployment on: F1 prepares the commands, F3 plays them right after `DEPLOY: OK`;
- a connection placed on a sensitive role exposes, on every one of its roles, only the reads of its `allowlist` and its `open` tools (BR-175, BR-186);
- migration 7 at start: the policy tables, and the settings `mode` (`write`), `pinning` (`auto`) and `response_cap` (`1MiB`) on the organisation;
- with `pinning` at `auto`, each sensitive placement records the definitions of its tools at its first listing after the deployment (trust on first use, spec §12); a tool whose definition changes later is suspended until `tool validate`;
- a tool result larger than the role's `response_cap` is replaced by `response_too_large` (BR-184).

Run this policy checklist as your SSH account on the target in `/opt/hullgate`. Choose classifications and permissions for your own roles. On a fresh installation F2 may redeploy the same selected release; on an upgrade it marks the point after which the chosen policy must be checked.

Common shortcut (re-paste it in every new shell):

```sh
cd /opt/hullgate
F="-f deploy/compose.prod.yaml --env-file .env"
C="docker compose $F exec -T hullgate hullgate admin"
```

## Step F1 — Before the deployment: read the connections and choose the writes

Still on the reference before F-015. Set `N=` on the Gitea connection read back from `$C connection list --json`, and `ROLE=` on the non-sensitive role it serves:

```sh
cd /opt/hullgate
F="-f deploy/compose.prod.yaml --env-file .env"
C="docker compose $F exec -T hullgate hullgate admin"
N=<client>/<connection>
ROLE=<client>/<project>/<env>/<role>
$C connection list --json
$C connection tools $N --role $ROLE
```

**Expected result**: the connection, its roles, and its tools with their public names (`gt__…`).

**If the result differs**: `not placed` → R6.

**Decide, and write down**: which upstream writes this role must keep (for example `issue_write`, `pull_request_write`). Each one becomes one `tool classify gitea <tool> write` in F3 (the class binds the service, so every Gitea connection gets it), or one `policy add … open <tool>` for this role only. Keep the choice locally.

## Step F2 — Deploy the selected policy reference

```sh
cd /opt/hullgate && make deploy REF=<ref> && make verify
```

**Expected result**: `DEPLOY: OK` and `VERIFY: 6/6 OK`. Continue immediately with F3.

**If the result differs**: inspect the failed build or readiness check. Keep the prior backup;
older code may refuse the newer schema, so rollback can require restoring that backup.

## Step F3 — Right after the deployment: give back the writes chosen in F1

Set `W=` on the writes chosen in F1, upstream names separated by spaces:

```sh
cd /opt/hullgate
F="-f deploy/compose.prod.yaml --env-file .env"
C="docker compose $F exec -T hullgate hullgate admin"
N=<client>/<connection>
ROLE=<client>/<project>/<env>/<role>
W="issue_write pull_request_write"
$C connection tools $N --role $ROLE
for T in $W; do $C tool classify gitea $T write; done
$C tool queue --service gitea
$C policy preview $ROLE
```

**Expected result**: one line `gitea <tool>: class write (manual)` per write, the queue of the Gitea tools (61 the first time, each with its class, source and proposal), then the preview: the reads and the classified writes `exposed`, the other writes hidden with `above_mode`.

**If the result differs**: `unknown tool` on `tool classify` → the catalogue does not know it yet: replay `connection tools` first. `locked by …` → a lock lies above a placement of the service (BR-049, D10): unlock, or classify under the lock only upward.


## Step F4 — Configure the reviewer (spec §3.3) on a role of your choosing

Set `RC=` on the Gitea connection the reviewer uses, and `RR=` on the reviewer's role, both read back from `$C connection list --json` (sensitive or not: on a sensitive role the allowlist is mandatory, BR-175):

```sh
cd /opt/hullgate
F="-f deploy/compose.prod.yaml --env-file .env"
C="docker compose $F exec -T hullgate hullgate admin"
RC=<client>/<connection>
RR=<client>/<project>/<env>/<role>
$C connection tools $RC --role $RR
$C policy add $RC --role $RR allowlist --class read
$C policy add $RC --role $RR open pull_request_review_write
$C policy arg $RC --role $RR pull_request_review_write method create submit
$C policy add $RC --role $RR deny pull_request_write issue_write
$C policy show $RC --role $RR
```

**Expected result**: `added to allowlist: <n>` with the reads listed, `added to open: 1`, `argument rule: pull_request_review_write method create submit`, `added to deny: 2`, then `policy show` with these lists and the rule.

**If the result differs**: `value outside the enum of method` → `gitea-mcp` names its methods differently: read them in `connection tools … --json` and inspect them, do not guess. `upstream unavailable` → R4. `locked by …` → unlock the node first or choose another role.

## Step F5 — The three assertions of AC-OP

1. `make verify-relay` on the reviewer's role. Set `TOOL=` on a read exposed in F4 (for example `gt__get_me`). Name `SENSITIVE=` only for a **blank** sensitive placement (BR-175: `sensitive` checks an empty list); otherwise use the first block of R7:

```sh
cd /opt/hullgate
F="-f deploy/compose.prod.yaml --env-file .env"
C="docker compose $F exec -T hullgate hullgate admin"
RR=<client>/<project>/<env>/<role>
TOOL=gt__get_me
V='s/.*"value": "\([^"]*\)".*/\1/p'
$C token create --path $RR --label verify-relay --duration 1h --json \
  | sed -n "$V" \
  | make verify-relay ROLE=$RR TOOL=$TOOL
```

**Expected result**: `VERIFY-RELAY: 5/5 OK`, or `4/5 OK` with `sensitive: SKIP`. Revoke the token as in R7.

2. The preview row of the merge tool:

```sh
$C policy preview $RR --json | grep -A 14 '"upstream_name": "pull_request_write"'
```

**Expected result**: the row reads `"exposed": false`, with `denied` among its `reasons`.

3. From Claude.ai, on the connector of `RR`, ask for a merge: a call to `<prefix>__pull_request_write` with `method: merge`. **Expected result**: the client reports an unknown tool. Then:

```sh
$C audit tail -n 20 | grep 'relay.tool_call'
```

**Expected result**: a `relay.tool_call` line for that call, `issue` `refused` (reason `denied`, or `not_allowlisted` if the client guessed another name).

**If the result differs**: `pull_request_write` visible in Claude.ai → disconnect and reconnect the connector (the list is read again at connection, F-008 §12); still visible → inspect the preview and stop. `exposed: true` → F4 was not played on this role.

## Step F6 — Compare the policy readings

```sh
$C policy preview $RR --json | grep -c '"exposed": true'
$C policy preview $RR --json | wc -c
```

**Expected result**: the count of `exposed: true` rows; the count of tools in Claude.ai's list for that connector (read by hand); the size in bytes of the preview output. Compare the two counts yourself and investigate any gap, without judging it.

**If the result differs**: do not redeploy an older reference as is: the database is at migration 7 and an older reference refuses it (`schema_too_new`) and leaves it untouched. Going back means restoring the backup that F2's `make deploy` took (`BACKUP: OK …`), per `backup-restore.md`. record the diagnostic locally before choosing.
