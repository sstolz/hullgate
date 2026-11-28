<script lang="ts">
  // SCR-11: the journal, newest first, filtered and paged by the server (`before` = `next`), and
  // the export of the current filter as JSON Lines (BR-218). History writes call the shared
  // undo operation; reads leave the domain state unchanged.
  import { onMount } from 'svelte';
  import { ApiError, message, query, type AuditEntry, type Client } from '../lib/api';
  import { auditFilter, exportName, type AuditForm } from '../lib/audit';
  import { save, trap } from '../lib/dom';
  import { undoBody, refusalState, type Change } from '../lib/history';
  import { afterWrite } from '../lib/notice.svelte';
  import { codeText, t } from '../lib/i18n';

  let { client }: { client: Client } = $props();

  let form = $state<AuditForm>({ since: '', until: '', actor: '', path: '', action: '', result: '' });
  let applied = $state.raw<Record<string, string>>({});
  let entries = $state.raw<AuditEntry[]>([]);
  let next = $state<number | string | null>(null);
  let loaded = $state(false);
  let error = $state('');
  let busy = $state(false);
  let change = $state.raw<Change | null>(null);
  let undoState = $state('SCR-11.undo-confirm');
  let undoError = $state('');
  let shareRequired = $state(false);
  let confirmShare = $state(false);

  async function readChange(seq: number): Promise<void> {
    busy = true;
    undoError = '';
    shareRequired = false;
    confirmShare = false;
    try {
      change = await client.call<Change>('GET', `changes${query({ seq })}`);
      undoState = change.stale ? 'SCR-11.undo-stale' : 'SCR-11.undo-confirm';
    } catch (e) { error = message(e); }
    finally { busy = false; }
  }
  async function undo(): Promise<void> {
    if (!change) return;
    busy = true;
    undoError = '';
    try {
      afterWrite(await client.call('POST', 'changes/undo', undoBody(change, confirmShare)));
      change = null;
      await page(null);
    } catch (e) {
      undoError = message(e);
      undoState = refusalState(e, 'undo');
      if (e instanceof ApiError && e.code === 'share_confirmation_required') shareRequired = true;
      if (change && e instanceof ApiError && e.code === 'undo_stale') {
        change = { ...change, stale: true, current: e.body.current, expected: e.body.expected };
      }
      if (change && e instanceof ApiError && e.code === 'impact_changed') {
        // A fresh read is a new confirmation; never silently submit a replacement digest.
        change = { ...change, stale: true };
      }
    } finally { busy = false; }
  }


  async function page(before: number | string | null): Promise<void> {
    busy = true;
    error = '';
    try {
      const read = await client.call<{ entries: AuditEntry[]; next: number | string | null }>(
        'GET',
        `audit${query({ ...applied, before })}`,
      );
      entries = before === null ? read.entries : [...entries, ...read.entries];
      next = read.next ?? null;
    } catch (e) {
      error = message(e);
    } finally {
      busy = false;
      loaded = true;
    }
  }

  function apply(event?: SubmitEvent): Promise<void> {
    event?.preventDefault();
    applied = auditFilter(form);
    return page(null);
  }

  function reset(): Promise<void> {
    form = { since: '', until: '', actor: '', path: '', action: '', result: '' };
    return apply();
  }

  async function exportAll(): Promise<void> {
    busy = true;
    error = '';
    try {
      const blob = await client.download('audit/export', applied, 'application/x-ndjson');
      save(blob, exportName(new Date()));
    } catch (e) {
      error = message(e);
    } finally {
      busy = false;
    }
  }

  function json(value: unknown): string {
    if (typeof value === 'string') {
      try { value = JSON.parse(value) as unknown; } catch { /* Historical plain text stays text. */ }
    }
    return value === null || value === undefined ? '—' : JSON.stringify(value, null, 2);
  }

  onMount(() => void apply());
</script>

<section
  class="panel"
  aria-labelledby="audit-title"
  data-state={loaded && entries.length === 0 ? 'SCR-11.empty' : 'SCR-11'}
>
  <h2 id="audit-title">{t('audit.title')}</h2>
  <p class="hint">{t('audit.intro')}</p>

  <form class="filters" onsubmit={apply}>
    <label>{t('audit.since')} <input type="datetime-local" step="1" bind:value={form.since} /></label>
    <label>{t('audit.until')} <input type="datetime-local" step="1" bind:value={form.until} /></label>
    <label>{t('audit.actor')} <input bind:value={form.actor} class="mono" placeholder="admin:…" /></label>
    <label>{t('audit.path')} <input bind:value={form.path} class="mono" /></label>
    <label>{t('audit.action')} <input bind:value={form.action} class="mono" placeholder="tree." /></label>
    <label>
      {t('audit.result')}
      <input bind:value={form.result} class="mono" list="audit-results" />
      <datalist id="audit-results">
        <option value="ok"></option>
        <option value="refused"></option>
      </datalist>
    </label>
    <div class="buttons">
      <button type="submit" class="primary" disabled={busy}>{t('audit.apply')}</button>
      <button type="button" class="ghost" disabled={busy} onclick={reset}>{t('audit.reset')}</button>
      <button type="button" class="ghost" disabled={busy} onclick={exportAll}>{t('audit.export')}</button>
    </div>
  </form>
  <p class="hint">{t('audit.time_hint')} {t('audit.export_hint')}</p>
  {#if error}<p class="notice" role="alert">{error}</p>{/if}

  {#if loaded && entries.length === 0}
    <p class="hint" data-state="SCR-11.empty">{t('audit.empty')}</p>
  {:else}
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th>{t('audit.col_seq')}</th>
            <th>{t('audit.col_at')}</th>
            <th>{t('audit.actor')}</th>
            <th>{t('audit.path')}</th>
            <th>{t('audit.action')}</th>
            <th>{t('audit.result')}</th>
            <th>{t('audit.col_reason')}</th>
          </tr>
        </thead>
        <tbody>
          {#each entries as e (e.seq)}
            <tr class="r-{e.result}">
              <td class="mono">{e.seq}</td>
              <td class="mono">{e.at}</td>
              <td class="mono">{e.actor}</td>
              <td class="mono">{e.path ?? '—'}</td>
              <td class="mono">{e.action}</td>
              <td class="res">{e.result}</td>
              <td>
                {e.reason ?? ''}
                {#if e.undoable}
                  <button class="ghost" disabled={busy} onclick={() => readChange(e.seq)}>{t('history.undo')}</button>
                {:else if e.undo_reason}
                  <span class="hint">{codeText('undo', e.undo_reason)}</span>
                {/if}
                {#if e.before !== null || e.after !== null || e.detail !== null}
                  <details>
                    <summary>{t('audit.details')}</summary>
                    <dl>
                      <dt>{t('audit.before')}</dt>
                      <dd><pre class="mono">{json(e.before)}</pre></dd>
                      <dt>{t('audit.after')}</dt>
                      <dd><pre class="mono">{json(e.after)}</pre></dd>
                      <dt>{t('audit.detail')}</dt>
                      <dd><pre class="mono">{json(e.detail)}</pre></dd>
                    </dl>
                  </details>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    {#if next !== null}
      <button type="button" class="ghost more" disabled={busy} onclick={() => page(next)}>{t('audit.more')}</button>
    {/if}
  {/if}
</section>

{#if change}
  <div class="veil">
    <div class="undo-dialog" role="dialog" aria-modal="true" aria-labelledby="undo-title" tabindex="-1" use:trap={() => { if (!busy) change = null; }} data-state={undoState}>
      <h2 id="undo-title">{t('history.title')} #{change.seq}</h2>
      <p>{change.actor} · {change.at} · {change.action} · {change.path}</p>
      <h3>{t('audit.before')}</h3><pre>{json(change.before)}</pre>
      <h3>{t('audit.after')}</h3><pre>{json(change.after)}</pre>
      {#if undoState === 'SCR-11.undo-stale'}
        <p>{t('history.stale')}</p>
        <h3>{t('history.current')}</h3><pre>{json(change.current)}</pre>
        <h3>{t('history.expected')}</h3><pre>{json(change.expected)}</pre>
      {/if}
      {#if change.impact}<p>{change.impact.notice}</p><pre>{json(change.impact.values)}</pre>{/if}
      {#if undoError}<p class="notice" role="alert">{undoError}</p>{/if}
      {#if shareRequired}<label><input type="checkbox" bind:checked={confirmShare} /> {t('history.share')}</label>{/if}
      <div class="buttons">
        <button class="primary" disabled={busy || change.stale || !change.undoable || (shareRequired && !confirmShare)} onclick={undo}>{t('history.confirm')}</button>
        <button class="ghost" disabled={busy} onclick={() => { if (change) void readChange(change.seq); }}>{t('history.refresh')}</button>
        <button class="ghost" disabled={busy} onclick={() => change = null}>{t('history.cancel')}</button>
      </div>
    </div>
  </div>
{/if}

<style>
  .veil { position: fixed; inset: 0; z-index: 40; background: #0009; display: grid; place-items: center; }
  .undo-dialog { max-width: 48rem; width: min(90vw, 48rem); max-height: 85vh; overflow: auto; background: var(--surface-1); border: 1px solid var(--rule); border-radius: var(--radius); padding: var(--space-4); }

  .panel {
    max-width: 90rem;
    margin: 0 auto;
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
  .filters {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr));
    gap: var(--space-2) var(--space-3);
    align-items: end;
    margin: var(--space-3) 0 var(--space-1);
  }
  .filters label {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }
  .buttons {
    display: flex;
    gap: var(--space-2);
    flex-wrap: wrap;
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
    vertical-align: top;
  }
  th {
    color: var(--muted);
    font-weight: 500;
    white-space: nowrap;
  }
  td.mono {
    white-space: nowrap;
  }
  .r-ok .res {
    color: var(--ok);
  }
  .r-refused .res {
    color: var(--danger);
  }
  pre {
    margin: 0;
    white-space: pre-wrap;
    word-break: break-all;
    font-size: 0.8rem;
  }
  dl {
    margin: var(--space-1) 0 0;
  }
  dt {
    color: var(--muted);
  }
  dd {
    margin: 0 0 var(--space-1);
  }
  .more {
    margin-top: var(--space-3);
  }
</style>
