<script lang="ts">
  // SCR-08: the rights preview of a role, or of a token on a role (BR-217): `POST rights`, the
  // relay's own decision. Rows are the server's, exposed first then hidden, each hidden one with
  // the reasons hiding it (codes shown beside their words); a connection the server could not list
  // is marked unknown, never absent. The token is picked from `GET tokens`, its value never typed.
  import { onMount } from 'svelte';
  import { message, type Client, type Token } from '../lib/api';
  import { codeText, t } from '../lib/i18n';
  import {
    groupedPairs,
    partition,
    rightsState,
    type Rights,
    type RightsRow,
  } from '../lib/policy';

  let {
    client,
    role,
    rights = null,
    onRead = () => {},
  }: {
    client: Client;
    role: string;
    rights?: Rights | null;
    onRead?: (r: Rights) => void;
  } = $props();

  // the preview shown: the one handed in until this screen reads its own
  let own = $state.raw<Rights | null>(null);
  const shown = $derived(own ?? rights);
  let tokens = $state.raw<Token[]>([]);
  let token = $state('');
  let error = $state('');
  let busy = $state(false);

  const rows = $derived(shown ? partition(shown) : { exposed: [], hidden: [] });
  const visual = $derived(shown ? rightsState(shown) : 'SCR-08');

  async function read(withToken: boolean): Promise<void> {
    // the buttons stay enabled while a read runs, so that the focus stays in the dialog (BR-232)
    if (busy) return;
    busy = true;
    error = '';
    try {
      const body: Record<string, string> = { role };
      if (withToken && token) body.token = token;
      own = await client.call<Rights>('POST', 'rights', body);
      onRead(own);
    } catch (e) {
      error = message(e);
    } finally {
      busy = false;
    }
  }

  onMount(() => {
    if (!rights) void read(false);
    client
      .call<{ tokens: Token[] }>('GET', 'tokens')
      .then((r) => (tokens = r.tokens))
      .catch((e: unknown) => (error = message(e)));
  });

  function test(event: SubmitEvent): void {
    event.preventDefault();
    void read(true);
  }
</script>

{#snippet details(row: RightsRow)}
  <span class="details">
    {#if row.opened}<span class="badge open">{t('rights.opened')}</span>{/if}
    {#if row.pinned}<span class="badge">{t('rights.pinned')}</span>{/if}
    {#if row.sensitive_via.length > 0}
      <span class="badge sens">{t('rights.sensitive_via', { roles: row.sensitive_via.join(', ') })}</span>
    {/if}
    {#each groupedPairs(row.grouped) as [argument, values] (argument)}
      <span class="hint">{t('rights.grouped', { argument, values })}</span>
    {/each}
    {#if row.argument_rule}
      <span class="hint"
        >{t('rights.argument', {
          argument: row.argument_rule.argument,
          values: row.argument_rule.values.join(', '),
        })}</span
      >
    {/if}
  </span>
{/snippet}

{#snippet table(list: RightsRow[], hidden: boolean)}
  <div class="scroll">
    <table>
      <thead>
        <tr>
          <th>{t('rights.col_name')}</th>
          <th>{t('rights.col_connection')}</th>
          <th>{t('rights.col_class')}</th>
          {#if hidden}<th>{t('rights.col_why')}</th>{/if}
          <th>{t('rights.col_details')}</th>
        </tr>
      </thead>
      <tbody>
        {#each list as row (`${row.connection}\u0000${row.upstream_name}`)}
          <tr class:hidden-row={hidden}>
            <td class="mono">{row.public_name}</td>
            <td class="mono">{row.connection}</td>
            <td>{codeText('class', row.class)} <span class="hint">({codeText('source', row.source)})</span></td>
            {#if hidden}
              <td>
                <ul class="reasons">
                  {#each row.reasons as code (code)}
                    <li>{codeText('reason', code)} <code class="hint">{code}</code></li>
                  {/each}
                </ul>
              </td>
            {/if}
            <td>{@render details(row)}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
{/snippet}

<div class="rights" data-state={visual}>
  <p class="hint">{t('rights.intro')}</p>

  <form class="token" onsubmit={test} aria-labelledby="token-title">
    <h3 id="token-title">{t('rights.token_title')}</h3>
    <label>
      {t('rights.token')}
      <select bind:value={token}>
        <option value="">{t('rights.token_none')}</option>
        {#each tokens as tok (tok.id)}
          <option value={tok.id}>{tok.holder} · {tok.type} · {tok.path} · {tok.state}</option>
        {/each}
      </select>
    </label>
    <button type="submit" class="ghost" aria-busy={busy} disabled={!token}>{t('rights.token_test')}</button>
    <button type="button" class="ghost" aria-busy={busy} onclick={() => read(false)}>{t('rights.reread')}</button>
    <p class="hint full">{t('rights.token_hint')}</p>
  </form>

  {#if error}<p class="notice" role="alert">{error}</p>{/if}
  {#if !shown}
    <p class="hint">{t('loading')}</p>
  {:else}
    {#if shown.token}
      {#if shown.token.reaches}
        <p class="ok" role="status">{t('rights.reaches', { step: String(shown.token.step ?? '—') })}</p>
      {:else}
        <p class="refused" role="status" data-state="SCR-08.out-of-scope">
          {t('rights.out_of_scope', {
            reason: codeText('reach', shown.token.reason ?? ''),
            step: String(shown.token.step ?? '—'),
          })}
          <code>{shown.token.reason}</code>
        </p>
      {/if}
      <p class="hint">{t('rights.role_follows')}</p>
    {/if}

    {#if shown.unavailable.length > 0}
      <ul class="unknown" data-state="SCR-08.unknown">
        {#each shown.unavailable as u (u.connection)}
          <li>
            <span class="badge unk">{t('rights.unknown_row')}</span>
            {t('rights.unknown', { connection: u.connection, code: u.code })}
          </li>
        {/each}
      </ul>
    {/if}
    {#each shown.policy_errors as name (name)}
      <p class="notice">{t('rights.policy_error', { connection: name })}</p>
    {/each}

    {#if visual === 'SCR-08.empty' || (shown.tools.length === 0 && shown.unavailable.length === 0)}
      <p class="empty" data-state="SCR-08.empty">{t('rights.empty')}</p>
    {:else}
      <h3>{t('rights.exposed_title', { n: String(rows.exposed.length) })}</h3>
      {#if rows.exposed.length === 0}
        <p class="hint">{t('rights.none_exposed')}</p>
      {:else}
        {@render table(rows.exposed, false)}
      {/if}
      {#if rows.hidden.length > 0}
        <h3>{t('rights.hidden_title', { n: String(rows.hidden.length) })}</h3>
        {@render table(rows.hidden, true)}
      {/if}
    {/if}
  {/if}
</div>

<style>
  h3 {
    margin: var(--space-3) 0 var(--space-1);
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--muted);
  }
  .token {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
    align-items: center;
    padding: var(--space-2) var(--space-3);
    margin: var(--space-2) 0 var(--space-3);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
  }
  .token h3,
  .token .full {
    flex-basis: 100%;
    margin: 0;
  }
  .token label {
    display: flex;
    gap: var(--space-2);
    align-items: center;
    min-width: 0;
  }
  .token select {
    max-width: 28rem;
  }
  .refused {
    color: var(--warn);
    background: var(--warn-bg);
    border: 1px solid var(--warn-bd);
    border-radius: var(--radius);
    padding: var(--space-2);
  }
  .unknown {
    list-style: none;
    margin: 0 0 var(--space-2);
    padding: var(--space-2);
    border: 1px dashed var(--warn-bd);
    border-radius: var(--radius);
  }
  .unknown li {
    padding: var(--space-1) 0;
  }
  .badge.unk {
    color: var(--warn);
    border-color: var(--warn-bd);
  }
  .badge.open {
    color: var(--cyan);
    border-color: var(--cyan);
  }
  .empty {
    padding: var(--space-4);
    text-align: center;
    color: var(--muted);
    border: 1px dashed var(--rule);
    border-radius: var(--radius);
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
    vertical-align: top;
    padding: var(--space-1) var(--space-2);
    border-bottom: 1px solid var(--rule);
  }
  th {
    color: var(--muted);
    font-weight: 500;
  }
  .hidden-row td:first-child {
    color: var(--muted);
  }
  .reasons {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .details {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1);
  }
</style>
