<script lang="ts" module>
  import type { Connection, Opened } from '../lib/api';

  export interface ConnectionActs {
    revoke: (c: Connection) => void;
    remove: (c: Connection) => void;
    placeIn: (c: Connection) => void;
    diagnose: (connection: string, role: string) => void;
    opened: (answer: Opened) => void;
    changed: () => void;
  }
</script>

<script lang="ts">
  // SCR-07: a connection's sheet. Account, scopes, state and expiry are the server's (BR-080);
  // revoking keeps name, prefix and placements, removing is a separate act (BR-095). A provided
  // token travels in a POST body and is never shown again (BR-086, BR-213).
  import { message, type Client } from '../lib/api';
  import { t } from '../lib/i18n';
  import { afterWrite } from '../lib/notice.svelte';

  let { client, connection, acts }: { client: Client; connection: Connection; acts: ConnectionActs } =
    $props();

  let prefix = $state('');
  let secret = $state('');
  let account = $state('');
  let expiry = $state('');
  let error = $state('');
  let done = $state('');
  let busy = $state(false);

  $effect(() => {
    prefix = connection.prefix;
    secret = '';
    account = '';
    expiry = '';
    error = '';
    done = '';
  });

  async function run(work: () => Promise<unknown>, ok = ''): Promise<void> {
    busy = true;
    error = '';
    done = '';
    try {
      afterWrite(await work());
      done = ok;
      acts.changed();
    } catch (e) {
      error = message(e);
    } finally {
      busy = false;
    }
  }

  function savePrefix(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    return run(
      () => client.call('PUT', 'connections/prefix', { name: connection.name, prefix }),
      t('conn.prefix_saved'),
    );
  }

  function replace(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    const provided: { value: string; account?: string; expires_at?: string } = { value: secret };
    if (account.trim()) provided.account = account.trim();
    if (expiry) provided.expires_at = expiry;
    secret = '';
    return run(
      () => client.call('POST', 'connections/reauthorize', { name: connection.name, provided }),
      t('conn.replaced'),
    );
  }

  async function reauthorize(): Promise<void> {
    busy = true;
    error = '';
    try {
      const answer = await client.call<Opened>('POST', 'connections/reauthorize', {
        name: connection.name,
      });
      afterWrite(answer);
      acts.changed();
      acts.opened(answer);
    } catch (e) {
      error = message(e);
    } finally {
      busy = false;
    }
  }

  function renew(): Promise<void> {
    return run(
      () => client.call('POST', 'connections/renew', { name: connection.name }),
      t('conn.renewed'),
    );
  }

  const visual = $derived(
    connection.stored_state === 'pending'
      ? 'SCR-07.pending'
      : connection.stored_state === 'reauth_required'
        ? 'SCR-07.reauth-required'
        : connection.expiring ? 'SCR-07.expiring' : 'SCR-07',
  );
</script>

<section class="sheet" aria-labelledby="sheet-title" data-state={visual}>
  <p class="kind">{t('conn.kind', { service: connection.service })}</p>
  <h2 id="sheet-title" class="mono">{connection.name}</h2>

  {#if connection.stored_state === 'reauth_required'}
    <p class="warn" role="status">{t('conn.reauth_required')}</p>
  {:else if connection.stored_state === 'pending'}
    <p class="warn" role="status">{t('conn.pending')}</p>
  {/if}
  {#if connection.expiring}
    <p class="warn" role="status">{t('conn.expiring', { date: connection.access_ends_at ?? '—' })}</p>
  {/if}
  {#if connection.key}<p class="notice" role="status">{connection.key}</p>{/if}
  {#if connection.application_to_redeclare}
    <p class="warn" role="status">{t('conn.redeclare', { id: connection.application_to_redeclare })}</p>
  {/if}

  <dl>
    <dt>{t('conn.state')}</dt>
    <dd><span class="state st-{connection.stored_state}">{connection.state}</span></dd>
    <dt>{t('conn.type')}</dt>
    <dd>{connection.type}</dd>
    {#if connection.application}
      <dt>{t('conn.application')}</dt>
      <dd class="mono">{connection.application}</dd>
    {/if}
    <dt>{t('conn.account')}</dt>
    <dd class="mono">{connection.account}</dd>
    <dt>{t('conn.scopes')}</dt>
    <dd class="mono">{connection.scopes.length ? connection.scopes.join(' ') : '—'}</dd>
    <dt>{t('conn.expiry')}</dt>
    <dd class="mono">
      {connection.expires_at ?? '—'}{#if connection.expiry_estimated}
        <span class="hint">({t('conn.estimated')})</span>{/if}
    </dd>
    <dt>{t('conn.upstream')}</dt>
    <dd class="mono">{connection.upstream}</dd>
    {#if connection.refusal}
      <dt>{t('conn.refusal')}</dt>
      <dd class="mono">{connection.refusal}</dd>
    {/if}
    {#if connection.last_outcome}
      <dt>{t('conn.last_outcome')}</dt>
      <dd class="mono">{connection.last_outcome}</dd>
    {/if}
  </dl>

  <h3>{t('conn.where_used')}</h3>
  {#if connection.placements.length === 0}
    <p class="hint">{t('conn.nowhere')}</p>
  {:else}
    <ul class="plain">
      {#each connection.placements as p (p.role)}
        <li>
          <span class="mono">{p.role}</span>
          {#if p.sensitive}<span class="badge sens">{t('badge.sensitive')}</span>{/if}
          <button type="button" class="ghost small push" onclick={() => acts.diagnose(connection.name, p.role)}>
            {t('conn.diagnose')}
          </button>
        </li>
      {/each}
    </ul>
  {/if}
  <button type="button" class="ghost small" onclick={() => acts.placeIn(connection)}>{t('lib.place_in')}</button>

  <h3>{t('conn.prefix')}</h3>
  <form class="inline" onsubmit={savePrefix}>
    <input
      aria-label={t('conn.prefix')}
      bind:value={prefix}
      maxlength="8"
      disabled={connection.placements.length > 0}
      class="mono"
    />
    <button
      type="submit"
      class="ghost small"
      disabled={busy || connection.placements.length > 0 || prefix === connection.prefix}>{t('save')}</button
    >
  </form>
  <p class="hint">
    {connection.placements.length > 0 ? t('conn.prefix_placed') : t('conn.prefix_hint')}
  </p>

  <h3>{t('conn.acts')}</h3>
  <div class="acts">
    {#if connection.type === 'oauth'}
      <button type="button" class="primary small" disabled={busy} onclick={reauthorize}>{t('conn.reauth')}</button>
      <button type="button" class="ghost small" disabled={busy} onclick={renew}>{t('conn.renew')}</button>
    {/if}
    <button type="button" class="ghost small" onclick={() => acts.revoke(connection)}>{t('conn.revoke')}</button>
    <button type="button" class="danger small" onclick={() => acts.remove(connection)}>{t('conn.remove')}</button>
  </div>
  <p class="hint">{t('conn.revoke_body')}</p>

  {#if connection.type !== 'oauth'}
    <h3>{t('conn.replace_title')}</h3>
    <form class="stack" onsubmit={replace} autocomplete="off">
      <label>
        {t('conn.token_value')}
        <input type="password" bind:value={secret} required autocomplete="new-password" />
      </label>
      <label>
        {t('conn.account_optional')}
        <input bind:value={account} />
      </label>
      <label>{t('conn.expiry_optional')} <input bind:value={expiry} placeholder="2026-12-01T00:00:00Z" /></label>
      <button type="submit" class="primary small" disabled={busy || !secret}>{t('conn.replace')}</button>
    </form>
        <p class="hint">{t('conn.secret_hint')}</p>
  {/if}

  {#if done}<p class="ok" role="status">{done}</p>{/if}
  {#if error}<p class="notice" role="alert">{error}</p>{/if}
</section>

<style>
  .kind {
    margin: 0;
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--faint);
  }
  h2 {
    margin: 0 0 var(--space-2);
    font-size: 1.05rem;
    color: var(--title);
    word-break: break-all;
  }
  h3 {
    margin: var(--space-4) 0 var(--space-1);
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--muted);
  }
  dl {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: var(--space-1) var(--space-3);
    margin: 0;
  }
  dt {
    color: var(--muted);
  }
  dd {
    margin: 0;
    word-break: break-all;
  }
  .warn {
    color: var(--warn);
    background: var(--warn-bg);
    border: 1px solid var(--warn-bd);
    border-radius: var(--radius);
    padding: var(--space-2);
  }
  .st-active {
    color: var(--ok);
  }
  .st-pending {
    color: var(--warn);
  }
  .st-reauth_required {
    color: var(--danger);
  }
  .plain li {
    display: flex;
    gap: var(--space-2);
    align-items: center;
    padding: var(--space-1) 0;
    border-bottom: 1px solid var(--rule);
  }
  .push {
    margin-left: auto;
  }
  .inline {
    display: flex;
    gap: var(--space-2);
  }
  .inline input {
    width: 8rem;
  }
  .acts {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }
</style>
