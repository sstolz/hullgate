<script lang="ts">
  // SCR-09: operator tokens and OAuth lineages per node, issue, revoke; OAuth clients (BR-061 v2).
  // An issued value is shown once (BR-053 v3): after "I copied it — hide" it is dropped from memory.
  // The duration ceiling is the server's: its answer says when it capped (BR-052).
  import { onMount, tick } from 'svelte';
  import {
    message,
    query,
    type Client,
    type Issued,
    type OAuthClient,
    type Token,
    type TreeNode,
  } from '../lib/api';
  import { copy, focusInView } from '../lib/dom';
  import type { SetupLanding } from '../lib/routes';
  import { t } from '../lib/i18n';
  import { afterWrite } from '../lib/notice.svelte';
  import ImpactDialog from './ImpactDialog.svelte';

  let { client, landing = null }: { client: Client; landing?: SetupLanding | null } = $props();
  let roleField: HTMLSelectElement | undefined = $state();
  let loaded = $state(false);

  $effect(() => {
    if (!loaded || landing?.target !== 'credential') return;
    let current = true;
    void tick().then(() => { if (current) focusInView(roleField); });
    return () => { current = false; };
  });

  let nodes = $state.raw<TreeNode[]>([]);
  let tokens = $state.raw<Token[]>([]);
  let clients = $state.raw<OAuthClient[]>([]);
  let dcr = $state(false);
  let filter = $state('');
  let error = $state('');
  let done = $state('');
  let busy = $state(false);

  // issue
  let path = $state('');
  let label = $state('');
  let duration = $state('');
  let confirm = $state('');
  let issued = $state.raw<Issued | null>(null);
  let copied = $state('');

  // OAuth client
  let clientId = $state('');
  let clientPath = $state('');
  let redirects = $state('');
  let clientName = $state('');
  let removing = $state<OAuthClient | null>(null);

  async function loadTokens(): Promise<void> {
    const read = await client.call<{ tokens: Token[] }>('GET', `tokens${query({ path: filter })}`);
    tokens = read.tokens;
  }

  async function load(): Promise<void> {
    try {
      const [n, c] = await Promise.all([
        client.call<{ nodes: TreeNode[] }>('GET', 'nodes'),
        client.call<{ clients: OAuthClient[]; dcr: boolean }>('GET', 'clients'),
        loadTokens(),
      ]);
      nodes = n.nodes;
      clients = c.clients;
      dcr = c.dcr;
    } catch (e) {
      error = message(e);
    } finally {
      loaded = true;
    }
  }

  async function run(work: () => Promise<unknown>, ok = ''): Promise<void> {
    busy = true;
    error = '';
    done = '';
    try {
      afterWrite(await work());
      done = ok;
      await load();
    } catch (e) {
      error = message(e);
    } finally {
      busy = false;
    }
  }

  async function issue(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    busy = true;
    error = '';
    done = '';
    const body: Record<string, unknown> = { path, label: label.trim() };
    if (duration.trim()) body.duration = duration.trim();
    if (confirm.trim()) body.confirm = confirm.trim();
    try {
      issued = await client.call<Issued>('POST', 'tokens', body);
      copied = '';
      label = '';
      confirm = '';
      await loadTokens();
    } catch (e) {
      error = message(e);
    } finally {
      busy = false;
    }
  }

  async function copyValue(): Promise<void> {
    if (issued) copied = (await copy(issued.value)) ? t('tokens.copied') : t('copy_blocked');
  }

  function hide(): void {
    // the value leaves memory with the object holding it (BR-053 v3)
    issued = null;
    copied = '';
    done = t('tokens.hidden');
  }

  function revoke(tok: Token): Promise<void> {
    return run(
      () => client.call('POST', 'tokens/revoke', { id: tok.id }),
      t('tokens.revoked', { holder: tok.holder }),
    );
  }

  function addClient(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    const body: Record<string, unknown> = {
      client_id: clientId.trim(),
      path: clientPath,
      redirect_uris: redirects
        .split('\n')
        .map((r) => r.trim())
        .filter(Boolean),
    };
    if (clientName.trim()) body.name = clientName.trim();
    return run(async () => {
      const answer = await client.call('POST', 'clients', body);
      clientId = clientName = redirects = '';
      return answer;
    }, t('clients.added'));
  }

  function setDcr(on: boolean): Promise<void> {
    return run(() => client.call('PUT', 'clients/dcr', { on }), on ? t('clients.dcr_on') : t('clients.dcr_off'));
  }

  onMount(() => void load());
</script>

<div class="screen" data-state={tokens.length === 0 ? 'SCR-09.empty' : 'SCR-09'}>
  <section class="panel" aria-labelledby="tokens-title">
    <h2 id="tokens-title">{t('tokens.title')}</h2>
    <p class="hint">{t('tokens.intro')}</p>

    {#if issued}
      <div class="value" role="region" aria-labelledby="value-title" data-state="SCR-09.value-shown">
        <h3 id="value-title">{t('tokens.issued_title', { label: issued.label })}</h3>
        <p class="hint">{t('tokens.issued_sub', { path: issued.path, at: issued.expires_at })}</p>
        <code class="mono secret">{issued.value}</code>
        <div class="row">
          <button type="button" class="primary" onclick={copyValue} data-autofocus>{t('copy')}</button>
          <button type="button" class="ghost" onclick={hide}>{t('tokens.hide')}</button>
          {#if copied}<span class="hint" role="status">{copied}</span>{/if}
        </div>
        {#if issued.capped}
          <p class="warn" data-state="SCR-09.sensitive-ceiling">{t('tokens.capped', { at: issued.expires_at })}</p>
        {/if}
        {#if issued.uncovered_sensitive.length > 0}
          <p class="warn">{t('tokens.uncovered', { envs: issued.uncovered_sensitive.join(', ') })}</p>
        {/if}
      </div>
    {/if}

    <form class="issue" onsubmit={issue} autocomplete="off" aria-labelledby="issue-title">
      <h3 id="issue-title">{t('tokens.issue_title')}</h3>
      <label>
        {t('tokens.node')}
        <select bind:this={roleField} bind:value={path} required>
          <option value="" disabled>{t('choose')}</option>
          {#each nodes as n (n.id)}<option value={n.path}>{n.path}{n.sensitive ? ` (${t('badge.sensitive')})` : ''}</option>{/each}
        </select>
      </label>
      <label>
        {t('tokens.label')}
        <input bind:value={label} required maxlength="80" placeholder={t('tokens.label_placeholder')} />
      </label>
      <label>
        {t('tokens.duration')}
        <input bind:value={duration} class="mono" placeholder="30d" />
      </label>
      <label>
        {t('tokens.confirm')}
        <input bind:value={confirm} class="mono" />
      </label>
      <button type="submit" class="primary" disabled={busy || !path || !label.trim()}>{t('tokens.issue')}</button>
      <p class="hint full">{t('tokens.duration_hint')} {t('tokens.hint_sensitive')}</p>
    </form>

    {#if done}<p class="ok" role="status">{done}</p>{/if}
    {#if error}<p class="notice" role="alert">{error}</p>{/if}

    <div class="filter">
      <label>
        {t('tokens.filter')}
        <select
          value={filter}
          onchange={(e) => {
            filter = e.currentTarget.value;
            loadTokens().catch((x: unknown) => (error = message(x)));
          }}
        >
          <option value="">{t('tokens.all_nodes')}</option>
          {#each nodes as n (n.id)}<option value={n.path}>{n.path}</option>{/each}
        </select>
      </label>
    </div>
    {#if tokens.length === 0}
      <p class="hint" data-state="SCR-09.empty">{t('tokens.empty')}</p>
    {:else}
      <div class="scroll">
        <table>
          <thead>
            <tr>
              <th>{t('tokens.col_holder')}</th>
              <th>{t('tokens.col_type')}</th>
              <th>{t('tokens.col_node')}</th>
              <th>{t('tokens.col_issued')}</th>
              <th>{t('tokens.col_expires')}</th>
              <th>{t('tokens.col_state')}</th>
              <th><span class="sr">{t('tokens.col_acts')}</span></th>
            </tr>
          </thead>
          <tbody>
            {#each tokens as tok (tok.id)}
              <tr>
                <td>{tok.holder}</td>
                <td>{tok.type === 'oauth' ? t('tokens.type_oauth') : t('tokens.type_operator')}</td>
                <td class="mono">{tok.path}</td>
                <td class="mono">{tok.issued_at}</td>
                <td class="mono">{tok.expires_at}</td>
                <td class="st-{tok.state}">{tok.state}</td>
                <td>
                  {#if tok.state !== 'revoked'}
                    <button type="button" class="ghost small" disabled={busy} onclick={() => revoke(tok)}>
                      {t('tokens.revoke')}
                    </button>
                  {/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </section>

  <section class="panel" aria-labelledby="clients-title">
    <h2 id="clients-title">{t('clients.title')}</h2>
    <p class="hint">{t('clients.intro')}</p>
    <label class="check">
      <input type="checkbox" checked={dcr} disabled={busy} onchange={(e) => setDcr(e.currentTarget.checked)} />
      {t('clients.dcr')}
    </label>
    {#if clients.length === 0}
      <p class="hint">{t('clients.empty')}</p>
    {:else}
      <div class="scroll">
        <table>
          <thead>
            <tr>
              <th>{t('clients.col_id')}</th>
              <th>{t('clients.col_name')}</th>
              <th>{t('clients.col_kind')}</th>
              <th>{t('clients.col_path')}</th>
              <th>{t('clients.col_redirects')}</th>
              <th>{t('clients.col_state')}</th>
              <th><span class="sr">{t('tokens.col_acts')}</span></th>
            </tr>
          </thead>
          <tbody>
            {#each clients as c (c.client_id)}
              <tr>
                <td class="mono">{c.client_id}</td>
                <td>{c.name ?? '—'}</td>
                <td>{c.kind}</td>
                <td class="mono">{c.path}</td>
                <td class="mono">{c.redirect_uris.join(' ')}</td>
                <td>{c.state}</td>
                <td>
                  <button type="button" class="ghost small" onclick={() => (removing = c)}>{t('remove')}</button>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
    <form class="stack add" onsubmit={addClient} autocomplete="off">
      <h3>{t('clients.add_title')}</h3>
      <label>{t('clients.col_id')} <input bind:value={clientId} required class="mono" /></label>
      <label>
        {t('clients.col_path')}
        <select bind:value={clientPath} required>
          <option value="" disabled>{t('choose')}</option>
          {#each nodes as n (n.id)}<option value={n.path}>{n.path}</option>{/each}
        </select>
      </label>
      <label>
        {t('clients.redirects')}
        <textarea bind:value={redirects} required rows="3" class="mono"></textarea>
      </label>
      <label>{t('clients.name_optional')} <input bind:value={clientName} /></label>
      <button type="submit" class="primary" disabled={busy}>{t('clients.add')}</button>
    </form>
  </section>
</div>

{#if removing}
  {@const c = removing}
  <ImpactDialog
    {client}
    act="client.remove"
    params={{ client_id: c.client_id }}
    title={t('impact.client_remove', { id: c.client_id })}
    confirm={t('remove')}
    write={(digest) => client.call('POST', 'clients/remove', { client_id: c.client_id, impact: digest })}
    onDone={(answer) => {
      afterWrite(answer);
      removing = null;
      done = t('done');
      void load();
    }}
    onClose={() => (removing = null)}
  />
{/if}

<style>
  .screen {
    display: grid;
    gap: var(--space-4);
    max-width: 72rem;
    margin: 0 auto;
  }
  .panel {
    background: var(--surface-1);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    padding: var(--space-4);
  }
  h2 {
    margin: 0 0 var(--space-1);
    font-size: 1.05rem;
    color: var(--title);
  }
  h3 {
    margin: 0 0 var(--space-2);
    font-size: 0.95rem;
    color: var(--title);
  }
  .value {
    border: 1px solid var(--cyan);
    border-radius: var(--radius);
    padding: var(--space-3);
    margin: var(--space-3) 0;
  }
  .secret {
    display: block;
    word-break: break-all;
    background: var(--surface-2);
    padding: var(--space-2);
    border-radius: var(--radius);
  }
  .row {
    display: flex;
    gap: var(--space-2);
    align-items: center;
    margin-top: var(--space-2);
  }
  .warn {
    color: var(--warn);
  }
  .issue {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr));
    gap: var(--space-2) var(--space-3);
    align-items: end;
    margin: var(--space-4) 0;
  }
  .issue h3,
  .issue .full {
    grid-column: 1 / -1;
    margin: 0;
  }
  .issue label {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }
  .filter {
    margin: var(--space-3) 0;
  }
  .scroll {
    overflow-x: auto;
  }
  table {
    width: 100%;
    border-collapse: collapse;
  }
  th,
  td {
    text-align: left;
    padding: var(--space-1) var(--space-2);
    border-bottom: 1px solid var(--rule);
    white-space: nowrap;
  }
  th {
    color: var(--muted);
    font-weight: 500;
  }
  .st-active {
    color: var(--ok);
  }
  .st-revoked,
  .st-expired {
    color: var(--faint);
  }
  .check {
    display: flex;
    gap: var(--space-2);
    align-items: center;
    margin: var(--space-2) 0;
  }
  .add {
    max-width: 30rem;
    margin-top: var(--space-4);
  }
</style>
