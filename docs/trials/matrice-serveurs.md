# Matrix of targeted official servers — desk study (phase 1 of #4)

Ticket: #4 (HULL-4). Reading date of all sources: **2026-10-28**. No authenticated call to a provider, no account or application created. Only anonymous reads of public metadata (`/.well-known/...`) and of the 401 response of the MCP access points were made; they are marked "observed".

Conventions: `[Sn]` refers to the list of sources at the end of the document. "not verified" = no source read for this claim; it is a point to settle in phase 2.

## Summary table

| | Microsoft | Canva | Gitea (gitea-mcp 1.11.0) | Notion |
|---|---|---|---|---|
| Official MCP server | Yes, but **two very different families**: MCP Server for Enterprise (Entra, read-only, preview) and Work IQ MCP (M365 data, GA) | Yes, hosted: `https://api.canva.com/connect/v1/mcp` | Yes, binary to self-host (one process per Gitea server) | Yes, hosted: `https://mcp.notion.com/mcp` |
| Transport / sessions | HTTP (Streamable assumed); sessions not verified | Streamable HTTP; sessions not verified | stdio or HTTP `/mcp`, **stateless** | Streamable HTTP (+ SSE as fallback), `/mcp` **stateless** |
| OAuth registration | Pre-registered Entra application (no DCR, observed) | Application pre-registered in the Canva portal, confidential client (no DCR, observed) | OAuth2 application created by hand in each Gitea, public client + PKCE | DCR (RFC 7591) **and** CIMD, PKCE S256 mandatory (observed) |
| Tokens | Access: not verified (standard Entra duration); renewal 90 d rolling | Access: short, duration not documented; single-use renewal, duration not documented | Access 3600 s; renewal 730 h by default (configurable by the admin) | Access ~8 h; renewal: 180 d absolute **or** 30 d without use, rotation on every use |
| Multiple accounts with one OAuth client | Yes by design (one token per user × client pair, per tenant), but consent per tenant | Yes: individual authentication mandatory, no service account | Yes (one token per Gitea user), but one OAuth application per Gitea server | Yes by design (token bound to user + workspace), detail not verified |
| Tools grouped by argument | Work IQ: generic tools by path (`fetch`, `do_action`...); Enterprise: no | Not documented | Yes, `method` argument | Yes: `command`, `mode`, `query_type`, `action` |
| Main obstacle | Administrator consent per client tenant, and Work IQ licenses/billing | Status (public/private) of the application and Canva review: effects on third parties not verified | Two OAuth applications to create, Gitea scopes too broad by default | Absolute expiration at 180 d: periodic human reauthentication |

## Microsoft

### Official MCP server

There is no single "Microsoft 365 server", but several offers:

- **Microsoft MCP Server for Enterprise**: `https://mcp.svc.cloud.microsoft/enterprise`. Queries Entra directory data (users, groups, applications, devices, logs) **read-only**, in **preview**, three tools (`microsoft_graph_suggest_queries`, `microsoft_graph_get`, `microsoft_graph_list_properties`), 100 calls/min/user, global cloud only [S1]. It is not mail or calendar.
- **Work IQ MCP**: a single access point, ten generic tools according to Microsoft, operating on relative resource paths (among those cited: `fetch`, `fetch_blob`, `create_entity`, `update_entity`, `delete_entity`, `do_action`, `call_function`, `ask`, `retrieve`, `list_agents`, `get_schema`, `search_paths`); the tool is the verb, the path is the resource (examples from the doc: `do_action /me/sendMail`, `fetch /me/messages`). Fine-grained control is "by path, method and tenant policy", not by scope [S3]. Generally available; the Copilot Studio integration is in preview [S2]. Exact URL of the MCP endpoint: `https://workiq.svc.cloud.microsoft/mcp` answers 401 with an OAuth challenge (observed), but the doc read does not give it: to be confirmed.
- **M365 servers per workload** (Mail, Calendar, Teams, SharePoint, OneDrive, User, Word): Microsoft says they are kept "for compatibility" and **not to be used for new production projects**; replaced by Work IQ [S2].
- Community servers (for example Softeria) are not official: out of scope for this matrix.

Transport, protocol generation, sessions, tool annotations: not verified (`initialize` requires a token).

### OAuth

- **No DCR or CIMD**: the Entra `organizations/v2.0` metadata has no `registration_endpoint` and no CIMD flag (observed, [S6]). A **pre-registered Entra application** is therefore needed from the operator. Resource server discovery works (observed): Enterprise returns `resource=https://mcp.svc.cloud.microsoft/enterprise`, authorization server `login.microsoftonline.com/organizations/v2.0`, scope `api://e8c77dc2-.../.default` [S5].
- PKCE: supported by the Entra platform, but not verified in the sources read. `resource` (RFC 8707): not verified (Entra uses its own scope parameters `api://.../scope`).
- **Redirect URL**: to be declared in the application; form rules (HTTPS, wildcards) not verified in this study. The gateway's own domain is a priori acceptable (not verified).
- **Scopes**: Enterprise = delegated only, `MCP.<nom-Graph>` (e.g. `MCP.User.Read.All`), "application permissions are not supported" [S4]. Work IQ: prefix `api://workiq.svc.cloud.microsoft/`, delegated scope `WorkIQAgent.Ask` (administrator consent required), "four broad permissions" announced, three not read [S3, S7].
- **Tokens**: renewal 90 days by default, replaced on every use, the old one is not revoked by the platform; 24 h for SPAs; bound to the user × client pair, not to a tenant [S8]. Access token duration: not verified. Revocation: password change, reset by an admin, revocation by the user or the admin; for a B2B guest, revocation must be done in the home tenant [S8].

### Multi-tenant (several clients of the operator)

Two architectures, to choose from (neither is settled by the sources):

1. **One multi-tenant application** ("accounts in any organizational directory"), registered once in the operator's tenant, used with `/organizations`; a service principal is created in each client tenant at first consent [S9]. Administrator consent is required for application permissions and some delegated permissions; a tenant administrator can disable user consent, and if the admin consents "for all", users cannot withdraw access individually [S9].
2. **One single-tenant application per client**, registered by the client (or by the operator if they have a role in its tenant). More friction for the client, but maximum isolation. The Enterprise doc incidentally recommends "single tenant" for custom MCP clients [S4].

What each client must do, according to the sources:

- **Enterprise MCP**: an administrator (Application Administrator or Cloud Application Administrator role) runs `Grant-EntraBetaMCPServerPermission` **once per tenant** (PowerShell module `Microsoft.Entra.Beta` ≥ 1.0.13), then grants administrator consent to the `MCP.*` scopes of the operator's application [S4].
- **Work IQ**: a Global Administrator enables Work IQ in the tenant (creation of the service principal `fdcc1f02-fc51-4226-8753-f668596af7f7`), and a **pay-as-you-go billing plan** (Copilot Studio, Azure subscription, resource group) must exist with the user assigned to the plan [S10]. This is heavy for a client of the operator.
- The Agent 365 / Work IQ servers are governed in the M365 admin center ("Agents > Tools"): an administrator can block them, approve them and control the tools one by one; their availability varies by tenant, region, licenses [S2].
- Log: Enterprise MCP calls appear in the client's Graph activity logs, filterable by `AppId = e8c77dc2-69b3-43f4-bc51-3213c9d915b4` [S1].

**Delegated or application**: the two servers studied are **delegated only** for Enterprise (explicit) [S4]. For Work IQ, only the delegated permission is listed (Application column empty) [S7]. The "account authenticated once" model therefore corresponds to delegated.

**Conditional access**: no source read deals with the behavior of conditional access policies when a token is renewed from the gateway's server (IP, device compliance, sign-in frequency). Real risk but not verified; to be tested on a client tenant with active policies. Only established fact: a renewal can be refused and then requires an interactive reauthentication [S8].

**Operator's account in the client tenant**: native or guest (B2B) account change the effect of revocation (see above) and the scope of consent; not settled.

Sources: S1 to S10 (see end).

## Canva

### Official MCP server

Yes, hosted: `https://api.canva.com/connect/v1/mcp`, according to the developer doc [S11, S12]. (`mcp.canva.com/mcp`, cited by third-party sites, is not in the official doc read.) Streamable HTTP transport: indicated by third-party sources only; sessions and protocol generation: not verified. The server talks to `canva.com` and `canva.ai` [S13]. More than 50 tools, including `search-designs`, `generate-design`, `export-design`, `perform-editing-operations`, editing transactions (start/commit/cancel); global limit of 300 req/min **per user**, limits specific to some tools (e.g. `generate-image` 10/min) [S14]. Grouping by argument and annotations: not documented in the pages read.

### OAuth

- **Application pre-registered in the developer portal**: one creates an application, turns on the "Canva MCP" switch, generates a secret; **confidential** client (id + secret) [S12, S15]. No DCR or CIMD in the public metadata: the authorization server `www.canva.com` announces neither `registration_endpoint` nor CIMD (observed, [S16]). A research summary mentions a CIMD path via a waiting list; not found on the page read, hence not verified.
- PKCE S256 mandatory (REST Connect doc, assumed identical for MCP) [S17]; `S256` announced (observed). `resource` (RFC 8707): not announced, not verified.
- **Redirect URL**: managed in the portal and must match those of the client application; the doc gives no rule on own domain, HTTP or localhost [S12, S17]. To be tested: the gateway's domain.
- **Scopes** (observed): `profile:read`, `design:meta:read`, `design:content:read/write`, `folder:read/write`, `brandtemplate:content:read/write`, `brandtemplate:meta:read`, `comment:read/write`, `asset:read/write`, `brandkit:read`, `help:answers:read/write` [S18].
- **Tokens**: "short-lived" access (value not documented), **single-use renewal with rotation**, duration not specified; revocation of an access or renewal token: existing access point (URL not read) [S17].

### Multiple accounts

- **Each user authenticates individually**; no organization-level authentication, no shared service account [S13]. Access follows the user's permissions on each design [S12].
- The usage policy: a shared agent "must not run with another user's OAuth token", no cross-user caching of metadata, brand kits or prompts (§6.2), no acting for another user without explicit delegation (§6.3) [S19]. For the gateway: **one OAuth client, one token per person, never reused**. The operator's account and that of their daughter are two distinct connections, never merged or shared between roles of different people.
- Limit of simultaneous tokens per client / invalidation by new authorization: not documented, not verified.
- Plan: basic functions on all plans; resizing, autofill, brand kits and brand templates require Pro or above [S12, S14].
- Application status: "Public" or "Private" is final at creation; private ones are offered only to Enterprise teams; public ones must pass a Canva review for the marketplace [S15]. What an unreviewed public application allows (user limit, developer's team only): **not verified and decisive**, since the daughter's account is outside the operator's team.

### Minor's account

- Canva terms of use (research summary, direct text inaccessible): children (under 13, or the local age of consent) cannot use the service outside Canva Education; reference to the Canva Education supplementary terms [S20]. For ages 13-18, a secondary source indicates a parent's authorization; not confirmed in Canva's text [S20]. In Education, inviting students under 13 assumes parental consent, which the school collects [S21]. Nothing found on a "Family" plan: not verified. Age of the operator's daughter: not known to this study.
- Practical rule: **the account holder signs in herself on the Canva page during consent** (credentials entered at Canva only). The gateway receives only an authorization code and then tokens, never a password. The operator is not "added" to her account: there is a distinct connection, revocable by her.

## Gitea (gitea-mcp 1.11.0)

### Official MCP server

Yes: `gitea-mcp` (repository `gitea.com/gitea/gitea-mcp`), v1.11.0 of 2026-10-22 [S22]. It is not a hosted service: a binary to run.

- **Transports**: stdio (`-t stdio`) or HTTP (`-t http --port`) on `/mcp`; "HTTP is always stateless" (POST only, no `Mcp-Session-Id`, no standalone SSE stream or resumption); origin check; proxies must relay `Mcp-Protocol-Version`, `Mcp-Method`, `Mcp-Name`; `/healthz` [S22, S23].
- **One Gitea host per process** (`GITEA_HOST`/`-H`); no multi-instance support described: for two Gitea servers, **two processes**, one upstream per server [S22, S23].
- **Tools grouped by `method`**: "several tools are consolidated" (`issue_write`, `label_write`, `milestone_read`, etc.); the `method` values are not listed in the README. Observed in the project's session: `issue_read` (get, get_comments, get_labels...) and `issue_write` (create, update, add_comment, add_labels, add_dependency...). Each tool has a scope and a Read/Write access; write tools are hidden in read-only mode (`-r`); filter by scope (`-S`) or by tool name (`-O`) [S22, S23]. MCP annotations: not mentioned in the README. A commit on the main branch renames parameters and methods ("feat!: make tool parameters and method names consistent"); not verified whether it is in 1.11.0: **pin the version** and reread the tool catalog on each upgrade [S22].
- Protocol generation: not verified.

### OAuth

Two paths, the second suited to the gateway:

1. **`-oauth` of gitea-mcp**: the MCP server acts as a protected resource and designates Gitea as the authorization server; manual registration ("Gitea has no dynamic registration") of an OAuth2 application, **non-confidential client** hence PKCE; redirect URL `https://claude.ai/api/mcp/auth_callback` and `http://127.0.0.1/callback` (any port) [S23]. Useful for clients without a gateway; not our case.
2. **Gateway as OAuth client of Gitea** then relay of the token: the access token is passed to gitea-mcp HTTP through `Authorization: Bearer <jeton>` on every request ("credential passthrough", not MCP OAuth); Gitea answers 401 to refused credentials [S23]. To validate in phase 2 that Gitea does accept an OAuth access token (and not only a personal token) through this channel.

Gitea provider details [S24, S25]:

- Single flow: authorization code, with PKCE (`S256` or `plain`) and OIDC; confidential or public clients.
- Redirect URL: `http`/`https` schemes by default, other schemes via `CUSTOM_SCHEMES`; for public clients, loopback `127.0.0.1` accepts any port. The gateway's own domain over HTTPS: expected acceptable (not tested).
- **Scopes**: `read:`/`write:` × activitypub, admin, issue, misc, notification, organization, package, repository, user; OIDC `openid`, `email`, `profile`, `groups`. **Any scope outside OIDC and outside the personal token scopes falls back to full access** [S24]: risk for a "reviewer" role that one would want confined.
- **Tokens**: access 3600 s; renewal 730 h by default (`REFRESH_TOKEN_EXPIRATION_TIME`); `INVALIDATE_REFRESH_TOKENS=false` by default (reuse of an already used renewal not checked); **these values are adjustable by the administrator of each Gitea server**: the actual values of the operator's two servers are to be read [S25]. `resource` (RFC 8707) is not mentioned: not verified.
- Revocation: the user can remove the application in their Gitea settings (not read); RFC 7009 access point: not verified.

### Multiple accounts

Dev and reviewer profiles = two Gitea users (or one user and a personal token): each authorization gives its token. One **OAuth application per Gitea server** (different ids, secrets moot for a public client), one connection per (server × user). Simultaneous tokens and invalidation by new authorization: not verified. Personal tokens (PAT) remain a fallback without OAuth but with scopes chosen at creation, and are excluded from the "OAuth once" model; the choice is to be validated with the operator.

## Notion

### Official MCP server

Yes, hosted by Notion ("Notion MCP", tagged Beta in its metadata) [S26, S27].

- Streamable HTTP `https://mcp.notion.com/mcp` (recommended), SSE `https://mcp.notion.com/sse` as fallback. `/mcp` **stateless**: may return `Mcp-Session-Id`, never validated [S26].
- More than 60 tools (search, pages, databases, views, comments, files, calendar, agent sessions, skills...) [S28]. **Grouping by argument**: `notion-update-page` (`command`), `notion-update-folder` (`command`), `notion-upload-skill` (`action`), `notion-query-data-sources` (`mode`: rows, view, SQL), `notion-search` (`query_type`: content or users), `notion-update-enabled-skills` (`surfaces`) [S28].
- Annotations: only `destructiveHint: true` is mentioned, on `notion-delete-pages`, `notion-delete-databases`, `notion-resolve-suggested-edit`; `readOnlyHint` not mentioned [S28]. A tool hidden from `tools/list` remains callable (`notion-ai-search`): the list is not exhaustive [S28].
- Rate limits: those of the standard API; single retry if the wait is ≤ 2 s, otherwise `rate_limited` error with `retry_after_seconds` [S28].
- Protocol generation: not verified.

### OAuth (observed and documented)

- **DCR (RFC 7591) supported** (`registration_endpoint` = `https://mcp.notion.com/register`), **CIMD supported** (`client_id_metadata_document_supported: true`), client authentication `none`, `client_secret_*` and `private_key_jwt`, **PKCE S256 mandatory** [S26, S29].
- Scope: `default` (and `ema_issuer:manage`); OIDC scopes are not available to unauthorized clients [S26, S29].
- `resource` (RFC 8707): not mentioned by the doc; the server announces `resource=https://mcp.notion.com` (observed). The `/mcp` and `/sse` endpoints are declared as protected resources.
- Redirect URL: only instruction = HTTPS in production and same values at registration, authorization and exchange [S26]. Gateway's own domain: expected acceptable with DCR/CIMD (not tested).
- **Tokens**: access ~8 h ("subject to change", read `expires_in`); renewal: at the earliest of **180 absolute days since the first authorization (not rolling)** and **30 consecutive days without a successful renewal**; **rotation on every renewal**, the previous one remains valid during a grace period (at most two tokens per grant); replaying an already rotated token outside the grace period **revokes the whole grant**; `invalid_grant` is terminal, reauthorization is required [S26].
- **Consequence for the gateway**: serialize renewals per connection, atomically persist the new token before use, durably keep the DCR `client_id`/`client_secret` ("re-registering orphans the previous grants"), and store `user_id`/`workspace_id` from the first response (absent from renewals) [S26].
- Revocation: `revocation_endpoint` announced (`https://mcp.notion.com/token`, observed) but not described by the doc; RFC 7662 introspection available for its own tokens; an MCP token "only works with Notion MCP", not with the REST API [S26, S29].
- Management on the Notion side: workspace owners manage the clients (settings > Notion MCP); restricting the authorized clients requires the Enterprise plan; organization owners can list and revoke members' connections through the admin API [S27].

### Multiple accounts

One client (DCR or CIMD) can obtain a grant per user × workspace pair (the exchange response contains `user_id` and `workspace_id`). The doc does not explicitly address a client holding several accounts: to confirm in phase 2, including the case of one user in several workspaces (one grant per workspace?) [S26]. Operator's account and son's account = two independent grants, each obtained at authorization by the person concerned.

### Minor's account

Notion's Education page sets no minimum age; it reserves the free Education plan for higher-education students verified by school email, excludes primary/secondary pupils (who keep the Free plan) and personal addresses [S30]. Notion's terms of use and privacy policy: text not read (pages inaccessible); a third-party source mentions a minimum age of 13, **not verified** [S31]. Son's age: not known. Identical practical rule: the son signs in himself on the Notion consent page; the gateway never receives a password. Workspace owners can also restrict MCP clients [S27].

## Risks for the connection model

1. **Policy by tool name insufficient: grouped tools hide writes.** Gitea (`method`), Notion (`command`, `mode`, `query_type`) and above all Work IQ (`do_action /me/sendMail`, `create_entity`, `delete_entity`: the verb is the tool, the resource is an argument) mix reading and writing under one name. "Rules on argument value" are out of v1 (brief). Consequences: either the v1 tool catalog excludes these servers for writing, or a minimal argument rule case is needed, or filter upstream (`-r`, `-S`, `-O` for Gitea; tenant policy for Work IQ). Notion marks only `destructiveHint` on three tools, and hides at least one tool from `tools/list`: a filter based on annotations alone is ineffective.
2. **Expirations and rotations that require a human, or that destroy the grant.** Notion: absolute expiration at 180 d then reauthorization; rotation with revocation of the whole grant on replay (two concurrent workers suffice); DCR to keep or the grants are orphaned. Canva: single-use renewal, unknown duration. Gitea: durations set by each administrator. Entra: 90 d rolling but revocations by administrator action, possible conditional access policies. The "connect once" model must therefore provide: serialized and atomic renewal per connection, a visible "reauthorization required" state, an alert before the deadline.
3. **Consent and registration are done not once but per provider, per tenant, per server.** Microsoft: administrator consent and provisioning per client tenant, and Work IQ assumes pay-as-you-go billing and a Global Administrator at each client; Canva: a confidential application whose public/private status is final and whose effects on third-party accounts (the daughter) are not documented; Gitea: one OAuth application per server, out-of-list scopes = full access; Notion: the simplest (DCR). A connection library that assumes one OAuth client per provider is insufficient: provide `(fournisseur, tenant/serveur) → client OAuth`.
4. **One token per person, never shared** (Canva requires it in its usage policy; Notion and Entra do it by construction). Placing a connection in several nodes of the tree is possible as long as it is the same identity; putting it at the service of another person is not. The gateway must offer to "place" a connection only under the authority of its holder.
5. **Security of stored secrets**: Canva `client_secret`, Gitea secrets (if confidential client), renewal tokens of each provider: encryption at rest, restricted access, no presence in logs. A Notion MCP token is unusable on the REST API (limits the extent of a leak), unlike a Gitea or Canva token.
6. **Moving versions and catalogs**: Notion Beta ("subject to change" tokens), Enterprise MCP in preview, per-workload M365 servers deprecated in favor of Work IQ, gitea-mcp with an ongoing breaking change of method names. Pin and check the tool catalogs at each connection (already in the open point "inventory of servers with grouped tools").
7. **Client security policies that apply to the gateway** (conditional access, MCP client restrictions on the Notion Enterprise side, M365 administrator blocking tools): the refusal arrives along the way and looks like an outage; provide distinguishable errors on the web administration side.
8. **Family accounts**: age and terms-of-use questions are resolved by the holder and the provider, not by the gateway; but the compartmentalization of the tree must prevent a "Daughter" or "Son" connection from being placed under a professional client by mistake.

## Preparing phase 2

To be prepared by the operator (list, no runbook):

- **Microsoft**: a volunteer client tenant (or their own trial tenant) with an available administrator; decision between multi-tenant application and per-client application; a trial account in this tenant; specify the targeted scope (Entra directory only, or mail/calendar/Teams via Work IQ with the corresponding pay-as-you-go billing); note the tenant's conditional access policies.
- **Canva**: a developer account and the creation of an application in the portal (public/private choice), Canva MCP activation, secret generated and kept out of the repository; her daughter's agreement to sign in herself during the test session; her age and account type (personal, Education); the redirect URLs of the gateway's domain.
- **Gitea**: the URL, version and `[oauth2]` configuration of the two servers (token durations, custom schemes); two users (dev, reviewer) per server; one OAuth2 application per server, non-confidential client; gitea-mcp version pinned to 1.11.0.
- **Notion**: a workspace for each account (operator, son); the son's agreement to sign in himself; check that the Notion MCP settings of these workspaces allow external clients; age and account type of the son.
- **Gateway**: final public HTTPS domain on the production server (the redirect URLs depend on it) and Traefik route ready; encrypted token storage.
- **To measure in phase 2**: transport and negotiated protocol version (`initialize`), sessions, annotations, `resource` accepted or not, exact form of the accepted redirect URLs, actual access durations, invalidation by new authorization, behavior of two simultaneous grants of one client, revocation, complete Gitea `method` values, exact Work IQ access point, behavior under conditional access.

## Unverified points (summary)

Exact transport, protocol and sessions of Canva and Microsoft; PKCE/`resource`/redirect URL at Microsoft, Canva (except announced PKCE S256) and Gitea; Microsoft/Canva access token duration; Canva renewal duration; CIMD via waiting list at Canva; effects of an unreviewed public Canva application; Canva terms of use (read by summary), Notion's (not read) and Education conditions; invalidation by new authorization everywhere; conditional access; three of the four Work IQ permissions; exact URL of the Work IQ MCP endpoint.

## Sources (read on 2026-10-28)

- S1 https://learn.microsoft.com/en-us/graph/mcp-server/overview
- S2 https://learn.microsoft.com/en-us/microsoft-agent-365/tooling-servers-overview
- S3 https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/work-iq/mcp/overview
- S4 https://learn.microsoft.com/en-us/graph/mcp-server/get-started
- S5 observed: `https://mcp.svc.cloud.microsoft/.well-known/oauth-protected-resource/enterprise` and 401 response of `https://mcp.svc.cloud.microsoft/enterprise` and of `https://workiq.svc.cloud.microsoft/mcp` (anonymous GET/POST)
- S6 observed: `https://login.microsoftonline.com/organizations/v2.0/.well-known/openid-configuration`
- S7 https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/work-iq/permissions
- S8 https://learn.microsoft.com/en-us/entra/identity-platform/refresh-tokens
- S9 https://learn.microsoft.com/en-us/entra/identity-platform/howto-convert-app-to-be-multi-tenant
- S10 https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/work-iq/enable-work-iq
- S11 https://www.canva.dev/docs/apps/mcp/
- S12 https://www.canva.dev/docs/apps/mcp/access/
- S13 https://www.canva.dev/docs/apps/mcp/troubleshooting/
- S14 https://www.canva.dev/docs/apps/mcp/tools/
- S15 https://www.canva.dev/docs/apps/quickstart/?app-surface=Canva%20for%20AI%20assistants
- S16 observed: `https://www.canva.com/.well-known/oauth-authorization-server`, `.../openid-configuration`
- S17 https://www.canva.dev/docs/connect/authentication/
- S18 observed: `https://api.canva.com/.well-known/oauth-protected-resource/connect/v1/mcp` and 401 of `https://api.canva.com/connect/v1/mcp`
- S19 https://www.canva.dev/docs/apps/mcp/usage-policy/
- S20 https://www.canva.com/policies/terms-of-use/ (direct access refused, 403; content known through a web search summary of the same day)
- S21 https://www.canva.com/fr_ca/help/invite-teachers-and-students/ and https://www.canva.com/trust/education/ (web search summaries)
- S22 https://gitea.com/gitea/gitea-mcp (repository page, v1.11.0)
- S23 https://gitea.com/gitea/gitea-mcp/raw/branch/main/README.md
- S24 https://docs.gitea.com/development/oauth2-provider
- S25 https://raw.githubusercontent.com/go-gitea/gitea/main/custom/conf/app.example.ini (section `[oauth2]`, main branch; values of the deployed version not verified)
- S26 https://developers.notion.com/guides/mcp/build-mcp-client
- S27 https://developers.notion.com/guides/mcp/mcp
- S28 https://developers.notion.com/guides/mcp/mcp-supported-tools
- S29 observed: `https://mcp.notion.com/.well-known/oauth-protected-resource`, `.../oauth-authorization-server`, 401 of `https://mcp.notion.com/mcp`
- S30 https://www.notion.com/help/notion-for-education
- S31 web search (third-party summary, not Notion); Notion's terms not read
