<script lang="ts" module>
  import type { TreeNode } from '../lib/api';

  /** The acts of the inspector that go through the screen's dialogs (impact, placement). */
  export interface NodeActs {
    rename: (node: TreeNode) => void;
    move: (node: TreeNode) => void;
    remove: (node: TreeNode) => void;
    sensitive: (node: TreeNode, on: boolean) => void;
    lock: (node: TreeNode, on: boolean) => void;
    unplace: (connection: string, role: string) => void;
    diagnose: (connection: string, role: string) => void;
    select: (node: TreeNode) => void;
  }
</script>

<script lang="ts">
  // SCR-05 (a role) and SCR-06 (any other node). URLs, effective sensitivity, locks and settings
  // are the server's (BR-211); the inspector shows them and offers the acts. On a role (slice 3):
  // the rights preview summary (SCR-08, `POST rights`) with an opening to the full preview, and
  // each placed connection's policy (`GET placements/policy?path=`), editable in place.
  import { message, query, type Client, type Connection, type Token } from '../lib/api';
  import { copy } from '../lib/dom';
  import { levelName, t } from '../lib/i18n';
  import { summary, upstreamNames, type PlacementPolicy, type Rights } from '../lib/policy';
  import { rolesBelow } from '../lib/tree';
  import Dialog from './Dialog.svelte';
  import PlacementPolicyEditor from './PlacementPolicy.svelte';
  import RightsPreview from './RightsPreview.svelte';
  import Settings from './Settings.svelte';

  let {
    client,
    node,
    nodes,
    connections,
    acts,
  }: {
    client: Client;
    node: TreeNode;
    nodes: readonly TreeNode[];
    connections: readonly Connection[];
    acts: NodeActs;
  } = $props();

  let tokens = $state.raw<Token[]>([]);
  let copied = $state('');
  let error = $state('');
  let policies = $state.raw<PlacementPolicy[]>([]);
  let rights = $state.raw<Rights | null>(null);
  let rightsError = $state('');
  let rightsBusy = $state(false);
  let editing = $state('');
  let preview = $state(false);
  let shownPath = '';
  let readKey = '';

  const placed = $derived(connections.filter((c) => c.placements.some((p) => p.role === node.path)));
  const below = $derived(node.level === 'role' ? [] : rolesBelow(node, nodes));
  // a lock makes the node and its subtree read-only (BR-049); the server's `locked_by` says so
  const readOnly = $derived(node.locked_by !== null);
  const counts = $derived(rights ? summary(rights) : null);
  // re-read when the placements shown change (a place or unplace from the tree screen)
  const placedKey = $derived(placed.map((c) => c.name).join('\n'));

  function policyOf(name: string): PlacementPolicy | undefined {
    return policies.find((p) => p.connection === name);
  }

  function replaced(next: PlacementPolicy): void {
    policies = policies.map((p) => (p.connection === next.connection && p.role === next.role ? next : p));
  }

  function readRights(path: string): void {
    if (rightsBusy) return;
    rightsBusy = true;
    rightsError = '';
    client
      .call<Rights>('POST', 'rights', { role: path })
      .then((r) => {
        if (node.path === path) rights = r;
      })
      .catch((e: unknown) => (rightsError = message(e)))
      .finally(() => (rightsBusy = false));
  }

  async function copyUrl(url: string): Promise<void> {
    copied = (await copy(url)) ? t('copied') : t('copy_blocked');
  }

  $effect(() => {
    const path = node.path;
    const role = node.level === 'role';
    copied = '';
    error = '';
    tokens = [];
    // a reload of the tree hands the same node again: the open editor and preview stay open
    if (path !== shownPath) {
      shownPath = path;
      editing = '';
      preview = false;
    }
    if (!role) return;
    client
      .call<{ tokens: Token[] }>('GET', `tokens${query({ path })}`)
      .then((r) => (tokens = r.tokens))
      .catch((e: unknown) => (error = message(e)));
  });

  $effect(() => {
    const path = node.path;
    const key = `${path}\n${placedKey}`;
    if (key === readKey) return; // the same node handed again by a reload: nothing to re-read
    readKey = key;
    policies = [];
    rights = null;
    if (node.level !== 'role') return;
    client
      .call<{ placements: PlacementPolicy[] }>('GET', `placements/policy${query({ path })}`)
      .then((r) => {
        if (node.path === path) policies = r.placements.filter((p) => p.role === path);
      })
      .catch((e: unknown) => (error = message(e)));
    // no preview read here: `POST rights` is an audited fresh listing that may record the
    // catalogue and suspend a pinned tool, never a side effect of selecting a node
  });
</script>

<section
  class="insp"
  aria-labelledby="insp-title"
  data-state={node.level === 'role'
    ? node.sensitive
      ? 'SCR-05.sensitive'
      : placed.length === 0
        ? 'SCR-05.empty'
        : 'SCR-05'
    : 'SCR-06'}
>
  <p class="kind">{levelName(node.level)}</p>
  <h2 id="insp-title">{node.name}</h2>
  <p class="mono path">{node.path}</p>
  <p class="badges">
    {#if node.sensitive}<span class="badge sens">{t('badge.sensitive')}</span>{/if}
    {#if node.locked_by}<span class="badge lock">{t('badge.locked')}</span>{/if}
  </p>

  {#if node.locked_by && !node.locked_here}
    <p class="locked" data-state="SCR-04.locked">{t('lock.by', { path: node.locked_by })}</p>
  {:else if node.locked_here}
    <p class="locked" data-state="SCR-04.locked">{t('lock.here')}</p>
  {/if}

  {#if node.level === 'role'}
    {#if node.sensitive}
      <p class="sensitive" data-state="SCR-05.sensitive">{t('role.sensitive')}</p>
    {/if}
    <h3>{t('role.url')}</h3>
    {#if node.url}
      <div class="url">
        <code class="mono">{node.url}</code>
        <button type="button" class="ghost small" onclick={() => copyUrl(node.url ?? '')}>{t('copy')}</button>
      </div>
      {#if copied}<p class="hint" role="status">{copied}</p>{/if}
    {/if}
    <p class="hint">{t('role.url_hint')}</p>

    <h3>{t('rights.summary_title')}</h3>
    <div class="rights-sum" data-state="SCR-05.rights">
      {#if counts}
        <p>{t('rights.summary', { exposed: String(counts.exposed), hidden: String(counts.hidden) })}</p>
        {#if counts.unknown > 0}
          <p class="unknown">{t('rights.summary_unknown', { n: String(counts.unknown) })}</p>
        {/if}
        <button type="button" class="ghost small" onclick={() => (preview = true)}>{t('rights.open')}</button>
      {:else}
        <p class="hint">{t('rights.not_read')}</p>
      {/if}
      <button type="button" class="ghost small" aria-busy={rightsBusy} onclick={() => readRights(node.path)}
        >{counts ? t('rights.reread') : t('rights.read_summary')}</button
      >
      {#if rightsError}<p class="notice" role="alert">{rightsError}</p>{/if}
    </div>

    <h3>{t('role.placed')}</h3>
    {#if placed.length === 0}
      <p class="hint">{t('role.no_placement')}</p>
    {:else}
      <ul class="plain">
        {#each placed as c (c.name)}
          {@const pol = policyOf(c.name)}
          <li>
            <span class="mono">{c.name}</span>
            <span class="hint">{c.state} · {t('conn.prefix_short', { prefix: c.prefix })}</span>
            {#if pol}
              <span class="hint pol"
                >{t('policy.summary', {
                  mode: pol.mode,
                  origin:
                    pol.mode_origin === 'placement'
                      ? t('policy.origin_placement')
                      : t('policy.origin_inherited', { path: pol.mode_origin }),
                  pin: pol.pin,
                  open: pol.open.join(', ') || '—',
                  deny: pol.deny.join(', ') || '—',
                  allowlist: pol.allowlist.join(', ') || '—',
                })}</span
              >
            {/if}
            <span class="row-acts">
              {#if pol}
                <button
                  type="button"
                  class="ghost small"
                  aria-expanded={editing === c.name}
                  onclick={() => (editing = editing === c.name ? '' : c.name)}
                  >{editing === c.name ? t('policy.hide') : t('policy.edit')}</button
                >
              {/if}
              <button type="button" class="ghost small" onclick={() => acts.diagnose(c.name, node.path)}>
                {t('conn.diagnose')}
              </button>
              {#if !readOnly}
                <button type="button" class="ghost small" onclick={() => acts.unplace(c.name, node.path)}>
                  {t('role.unplace')}
                </button>
              {/if}
            </span>
            {#if pol && editing === c.name}
              <PlacementPolicyEditor
                {client}
                policy={pol}
                names={upstreamNames(rights, c.name)}
                {readOnly}
                lockedBy={node.locked_by}
                onChange={(next) => {
                  replaced(next);
                  readRights(node.path);
                }}
              />
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
  {/if}

  <Settings {client} path={node.path} {readOnly} />

  {#if node.level === 'env'}
    <h3>{t('node.sensitivity')}</h3>
    <label class="check">
      <input
        type="checkbox"
        checked={node.marked}
        disabled={readOnly}
        onchange={(e) => {
          const on = e.currentTarget.checked;
          e.currentTarget.checked = node.marked;
          acts.sensitive(node, on);
        }}
      />
      {t('node.sensitive_check')}
    </label>
    <p class="hint">{t('node.sensitive_hint')}</p>
  {/if}

  {#if node.level !== 'org'}
    <h3>{t('node.lock_title')}</h3>
    {#if node.locked_here}
      <button type="button" class="ghost" onclick={() => acts.lock(node, false)}>{t('node.unlock')}</button>
    {:else}
      <button type="button" class="ghost" disabled={readOnly} onclick={() => acts.lock(node, true)}>
        {t('node.lock')}
      </button>
    {/if}
    <p class="hint">{t('node.lock_hint')}</p>
  {/if}

  {#if node.level !== 'role'}
    <h3>{t('node.roles_below')}</h3>
    {#if below.length === 0}
      <p class="hint">{t('node.no_roles')}</p>
    {:else}
      <ul class="plain">
        {#each below as role (role.id)}
          <li>
            <button type="button" class="link mono" onclick={() => acts.select(role)}>{role.path}</button>
          </li>
        {/each}
      </ul>
    {/if}
  {/if}

  {#if node.level === 'role'}
    <h3>{t('role.tokens')}</h3>
    {#if tokens.length === 0}
      <p class="hint">{t('role.no_tokens')}</p>
    {:else}
      <ul class="plain">
        {#each tokens as tok (tok.id)}
          <li>
            <span>{tok.holder}</span>
            <span class="hint">{tok.type} · {tok.state} · {t('tokens.expires_at', { at: tok.expires_at })}</span>
          </li>
        {/each}
      </ul>
    {/if}
    {#if node.sensitive}<p class="hint">{t('tokens.hint_sensitive')}</p>{/if}
  {/if}

  {#if node.level !== 'org' && !readOnly}
    <div class="node-acts">
      <button type="button" class="ghost small" onclick={() => acts.rename(node)}>{t('node.rename')}</button>
      <button type="button" class="ghost small" onclick={() => acts.move(node)}>{t('node.move')}</button>
      <button type="button" class="danger small" onclick={() => acts.remove(node)}>{t('node.delete')}</button>
    </div>
  {/if}
  {#if error}<p class="notice" role="alert">{error}</p>{/if}
</section>

{#if preview && node.level === 'role'}
  <Dialog title={t('rights.title', { role: node.path })} onClose={() => (preview = false)} wide visual="SCR-08.dialog">
    <RightsPreview {client} role={node.path} {rights} onRead={(r) => (rights = r)} />
  </Dialog>
{/if}

<style>
  .kind {
    margin: 0;
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--faint);
  }
  h2 {
    margin: 0;
    font-size: 1.1rem;
    color: var(--title);
  }
  h3 {
    margin: var(--space-4) 0 var(--space-1);
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--muted);
  }
  .path {
    margin: 0;
    color: var(--muted);
    word-break: break-all;
  }
  .badges {
    margin: var(--space-1) 0 0;
    display: flex;
    gap: var(--space-1);
  }
  .locked,
  .sensitive {
    color: var(--warn);
    background: var(--warn-bg);
    border: 1px solid var(--warn-bd);
    border-radius: var(--radius);
    padding: var(--space-2);
  }
  .url {
    display: flex;
    gap: var(--space-2);
    align-items: center;
  }
  .url code {
    flex: 1;
    min-width: 0;
    word-break: break-all;
    background: var(--surface-2);
    padding: var(--space-1) var(--space-2);
    border-radius: var(--radius);
  }
  .plain li {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1) var(--space-2);
    align-items: baseline;
    padding: var(--space-1) 0;
    border-bottom: 1px solid var(--rule);
  }
  .rights-sum {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1) var(--space-3);
    align-items: center;
  }
  .rights-sum p {
    margin: 0;
  }
  .unknown {
    color: var(--warn);
  }
  .pol {
    flex-basis: 100%;
  }
  .row-acts {
    margin-left: auto;
    display: flex;
    gap: var(--space-1);
  }
  .check {
    display: flex;
    gap: var(--space-2);
    align-items: center;
  }
  .node-acts {
    display: flex;
    gap: var(--space-2);
    flex-wrap: wrap;
    margin-top: var(--space-5);
    padding-top: var(--space-3);
    border-top: 1px solid var(--rule);
  }
</style>
