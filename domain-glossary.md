# Domain glossary — hullgate

> What things are called here. One concept = one canonical term; the terms of the "do not use" column are listed so that they stay out. The rules live in `business-rules/`. The canonical terms are English since F-010; since HULL-58 the documentation is English too, and each former French term sits in the "do not use" column, unless it is spelled like the English term.

## core

| Term | Definition | Also seen as (do not use) | Origin |
|---|---|---|---|
| **Access point** | MCP URL of a role, `/o/<id>/p/<path>/mcp` from F-014, `/p/<path>/mcp` until then. | endpoint (for an access point), profil, Point d'accès | `BR-044` |
| **Segment** | Part of the path that belongs to one node, unique among siblings. | slug (outside code), identifiant | `BR-044` |
| **Sensitive environment** | Marked environment whose roles accept only tokens placed on them. | prod (as a status), environnement protégé, Environnement sensible | `BR-047` |
| **Lock** | Freeze of a node and its subtree: structure, settings, sensitivity (setting `lock`). | figé, figeage (reserved for tool definitions, #10), gel, Verrou | `BR-049` |
| **Operator token** | Opaque token the operator places on a node (`token create`). | jeton statique, clé d'API, PAT, role token, Jeton opérateur | `BR-053` |
| **Authorization order** | Fixed sequence of the access point's checks. | pipeline d'auth, chaîne de vérification, Ordre d'autorisation | `BR-055` |
| **Organisation identifier** | Public, permanent, random 12-character base32 name of the organisation in URLs. | org id, tenant id, slug | `F-014` |
| **Organisation prefix** | `/o/<id>`, the head of every organisation-scoped route. | — | `BR-044` |

## oauth

| Term | Definition | Also seen as (do not use) | Origin |
|---|---|---|---|
| **Lineage** | Sequence of the OAuth tokens issued from one authorization, revoked as a block. | famille de jetons, session OAuth, Lignée | `BR-059` |
| **Authorization barrier** | Page where the operator authenticates (TOTP in F-005, passkey with #11) before any code. | consentement (seul), page opérateur, Barrière d'autorisation | `BR-060` |
| **Request reference** | Short non-secret value derived from a pending authorization request, shown on the barrier page and by `consent list`. | — | `F-020` |

## connections

| Term | Definition | Also seen as (do not use) | Origin |
|---|---|---|---|
| **Library** | The set of the organisation's connections. | écran Connexions, catalogue de comptes, Bibliothèque | `BR-074` |
| **Connection** | An account at a service, authorized once, with a single encrypted token set (`connection`). | compte connecté, intégration, profil, Connexion | `BR-074` |
| **Provided-token connection** | Connection whose token value the operator provides (`--provided`), encrypted like any connection, never renewed. | jeton statique, PAT, connexion manuelle, Connexion à jeton fourni | `BR-085` |
| **Service** | Declarative record of a provider: model, base URL, requested scopes, discovered endpoints. | profil fournisseur, catalogue de services | `BR-074` |
| **Application (outbound OAuth)** | OAuth client the gateway declares or registers at a provider, shared by several connections. | client sortant, app, Application (OAuth sortante) | `BR-081` |
| **Placement** | Placing a connection on a role of the tree (`connection place`). | affectation, rattachement | `BR-077` |
| **Tool prefix** | Short prefix, unique in the organisation, of the tool names of a connection. | alias, slug de connexion, Préfixe d'outil | `BR-076` |

## quality

| Term | Definition | Also seen as (do not use) | Origin |
|---|---|---|---|
| **Quality tool** | Tool that judges the code or its dependencies outside the gate: a `qa-<name>` target of the `Makefile`, with a role (`blocking` or `info`) declared in `QA_TOOLS`. | linter (reserved for the gate's ruff), scanner (reserved for the SonarQube image), check, Outil d'analyse | `BR-098` |
| **QA line** | The single line `QA <name> <role> <verdict> — <detail>` a quality tool prints; verdict `OK`, `FINDINGS`, `SKIP` or `ERROR`. | rapport QA, résultat d'outil, Ligne QA | `BR-098` |

## relay

| Term | Definition | Also seen as (do not use) | Origin |
|---|---|---|---|
| **Relay** | Part of the gateway that serves, at a role's access point, the tools of the connections placed there and routes each call to the upstream of the right connection. | agrégateur, backend, Relais | `BR-109` |
| **Upstream** | MCP server a service declares (type `mcp-public` or `mcp-internal`, `service upstream`) and that the relay reaches with the connection's token. | backend, serveur d'arrière-plan, Amont | `BR-106` |
| **Public (tool) name** | Name `<prefix>__<tool>` under which the relay announces an upstream tool, stable as long as the connection exists. | alias d'outil, nom préfixé, Nom public (d'un outil) | `BR-110` |

## operations

| Term | Definition | Also seen as (do not use) | Origin |
|---|---|---|---|
| **Witness (SDK guard)** | The conclusive full replay of the bench's `v1` target that the SDK guard reads. | — | `F-009` |
| **Client address** | The address BR-208 yields for a request: the socket peer, or the forwarded entry a declared proxy vouches for. | — | `F-016` |
| **Limit key** | IPv4 form of a client address, or its /48 for IPv6: the unit every address-keyed limit counts, and one source. | — | `F-020` |
| **Budget** | N requests in a window T for one key, held over every span of T. | — | `F-020` |
| **Anonymous surface** | The OAuth routes and unknown credentials at the access point and at `token`. | — | `F-020` |
| **Coalesced refusal** | One `rate_limited.refused` entry per surface, key and 60 seconds, its `suppressed` counting the refusals not written. | — | `F-020` |

## documentation

| Term | Definition | Also seen as (do not use) | Origin |
|---|---|---|---|
| **Former product name** | The product's name before `hullgate`; removed by HULL-58 and written only through a placeholder or fragments. | — | `F-016` |

## policy

| Term | Definition | Also seen as (do not use) | Origin |
|---|---|---|---|
| **Tool class** | The ordered class of a tool: `read`, `write`, `destructive`. | — | `F-015` |
| **Mode (policy)** | Setting a placement may override, bounding the tool class exposed: `read`, `write`, `destructive`. | — | `F-015` |
| **Classification source** | Where a tool class comes from: manual classification, else MCP annotations, else `destructive`. | — | `F-015` |
| **Tool catalogue** | One row per service and upstream tool name: first seen, manual class, queue state. | — | `F-015` |
| **Classification queue** | Where a tool seen for the first time waits for classification, its exposure unchanged. | — | `F-015` |
| **Open list** | A placement's `open` set: tools exposed past the allowlist and the class bound. | — | `F-015` |
| **Deny list** | A placement's `deny` set: tools hidden, whatever the lists and the mode say. | — | `F-015` |
| **Allowlist** | A placement's `allowlist` set of upstream tool names; not the admin IP list. | — | `F-015` |
| **Allowlist in force** | An allowlist that binds: on a sensitive placement, and on any placement whose `allowlist` is not empty. | — | `F-015` |
| **Argument rule** | Per tool on a placement: one argument name and its allowed values. | — | `F-015` |
| **Grouped tool** | One tool whose actions an argument selects, by enumerated values. | — | `F-015` |
| **Pin, pinning (tool definition)** | Holding a tool to its definition baseline, per tool, placement, then setting; not a lock. | figeage | `F-015` |
| **Definition baseline** | SHA-256 and text of the pinned upstream definition, per tool on a placement. | — | `F-015` |
| **Suspended tool** | A pinned tool whose upstream definition no longer hashes to its baseline: hidden and refused on that placement. | — | `F-015` |
| **Sensitive placement** | A placement whose connection is placed on at least one sensitive role. | — | `F-015` |
| **Rights preview** | `policy preview`: what a role sees of each tool, and why. | — | `F-015` |
| **Response cap** | `response_cap`: the size above which a `tools/call` result is replaced by `response_too_large`, never truncated. | — | `F-015` |

## administration

| Term | Definition | Also seen as (do not use) | Origin |
|---|---|---|---|
| **Administrator** | Named account of the organisation with a unique e-mail, a display name and a state; every active one has every right. "Operator" stays the CLI human. | — | `F-016` |
| **Pending (administrator)** | State of an administrator created by the host and not enrolled yet; the other state is `active`. | invited | `F-016` |
| **Console** | The web administration interface served under `/admin` on the admin listener. | — | `F-016` |
| **Admin session** | Random 256-bit value held in a `Path=/admin` cookie and stored as its digest. | — | `F-016` |
| **Enrolment link** | Single-use link of 256 random bits, bound to one account, kept as a digest, that registers a passkey. | — | `F-016` |
| **Fresh authentication** | A passkey assertion at most 5 minutes old, required by a security act. | — | `F-016` |
| **Impact digest** | Digest of the impact the server computes, which an irreversible write must carry. | — | `F-016` |
| **Undo (compensating change)** | A new change restoring an audit entry's `before`; the audit keeps both. | — | `F-016` |
| **Import preview** | The creations, changes and deletions of an import with their impact and a digest, written nowhere. | — | `F-016` |
| **Allow-list (admin IP)** | The organisation's CIDR list closing `/admin` to other client addresses; not a placement's allowlist. | — | `F-016` |
| **Web return URI** | `${ADMIN_URL}/admin/oauth/callback`, an application's second redirect URI, registered at the provider for the console. | — | `F-016` |
| **Setup guide** | The console's steps computed from the instance's state, with no acts of its own. | — | `F-016` |

## statistics

| Term | Definition | Also seen as (do not use) | Origin |
|---|---|---|---|
| **Call entry** | The `relay.tool_call` journal entry of one `tools/call`. | — | `F-017` |
| **Traffic entry** | An entry of the closed list of BR-243, kept 90 days. | — | `F-017` |
| **Daily aggregate** | One row per UTC day and dimension, holding counts and sums of rolled entries. | — | `F-017` |
| **Arguments digest** | Keyed, truncated digest of a call's arguments, comparable within a salt period. | — | `F-017` |
| **Token estimate** | `ceil(bytes / 4)`, an approximation of context cost; no credential. | — | `F-017` |
| **Role catalogue** | What a role announces at `tools/list`: public names and their weight; not the tool catalogue. | catalogue (alone) | `F-017` |
| **Chain** | Consecutive call entries of one token on one role, gaps under 5 minutes. | — | `F-017` |
| **Loop** | The same call repeated in a burst. | — | `F-017` |
| **Finding (security report)** | One row of the `security` report; not a QA line's `FINDINGS` verdict. | — | `F-017` |
| **Weighted refusals** | Refusals counted with `rate_limited.refused` entries weighing `suppressed` + 1. | — | `F-017` |
| **Refusal rate** | Weighted refusals over calls plus weighted refusals. | — | `F-017` |

## observability

| Term | Definition | Also seen as (do not use) | Origin |
|---|---|---|---|
| **Alert** | A condition true of one subject, open at most once per kind and subject. | — | `F-019` |
| **Alert kind** | One of the eight of F-019 §3.2, with its severity, subject and condition. | — | `F-019` |
| **Alert key** | Kind plus subject. | — | `F-019` |
| **Open alert** | An alert fired and not yet resolved. | — | `F-019` |
| **Resolution** | How an alert closed: `cleared`, `subject_removed`, `expired`, `no_recent_traffic`, `disabled`. | — | `F-019` |
| **Evaluator (alerts)** | The server-process task that checks alert conditions every 60 seconds; not the policy evaluator. | — | `F-019` |
| **Channel (alert)** | A `webhook` or `ntfy` destination declared in the channel file. | — | `F-019` |
| **Channel file** | The mounted secret `ALERT_CHANNELS_FILE`. | — | `F-019` |
| **Notification (alert)** | One `fired`, `reminder`, `resolved` or `test` message; not an MCP notification. | — | `F-019` |
| **Delivery (alert)** | One notification to one channel, retried on the schedule; not the tagged delivery of a release. | — | `F-019` |

## release

| Term | Definition | Also seen as (do not use) | Origin |
|---|---|---|---|
| **Version file** | `src/hullgate/VERSION`, the only place the product version is written. | — | `F-021` |
| **Version placeholder** | The constant `0.0.0` of `pyproject.toml`, which no release changes. | — | `F-021` |
| **Version cut** | The `cut-release` act changing the version file and tagging the release commit. | — | `F-021` |
| **Version tag** | The annotated `vX.Y.Z` tag a version cut sets. | — | `F-021` |

## publication

| Term | Definition | Also seen as (do not use) | Origin |
|---|---|---|---|
| **Operator infrastructure** | Every name, host, domain, account, path and port of the operator's own installation. | — | `F-022` |
| **Denylist** | The private list of patterns BR-325 matches against tracked paths and contents. | — | `BR-325` |
| **Generic guide** | A deployment guide written for any installation of the supported baseline. | — | `BR-328` |
| **Export rehearsal** | The export of a commit, judged without a local context, then with a scratch local context. | — | `F-022` |
| **Private source** | The Gitea repository that keeps the full history before and after public export. | — | `F-022` |
