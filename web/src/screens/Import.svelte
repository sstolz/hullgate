<script lang="ts">
  import { message, type Client } from '../lib/api';
  import { save } from '../lib/dom';
  import { currentExport, importBody, refusalState, type ImportPlan } from '../lib/history';
  import { t } from '../lib/i18n';
  import { afterWrite } from '../lib/notice.svelte';

  let { client }: { client: Client } = $props();
  let document = $state('');
  let plan = $state.raw<ImportPlan | null>(null);
  let deletions = $state(false);
  let shares = $state(false);
  let busy = $state(false);
  let error = $state('');
  let done = $state('');
  let screenState = $state('SCR-12');

  function invalidate(): void {
    plan = null;
    deletions = false;
    shares = false;
    error = '';
    done = '';
    screenState = 'SCR-12';
  }
  async function upload(event: Event): Promise<void> {
    const file = (event.target as HTMLInputElement).files?.[0];
    if (!file) return;
    invalidate();
    busy = true;
    try { document = await file.text(); }
    catch (e) { error = message(e); screenState = 'SCR-12.refused'; }
    finally { busy = false; }
  }
  async function preview(): Promise<void> {
    busy = true;
    invalidate();
    try {
      plan = await client.call<ImportPlan>('POST', 'import/preview', { document });
      screenState = plan.changes.length === 0 ? 'SCR-12.preview-empty' : 'SCR-12';
    } catch (e) { error = message(e); screenState = refusalState(e, 'import'); }
    finally { busy = false; }
  }
  async function apply(): Promise<void> {
    if (!plan) return;
    busy = true;
    error = '';
    try {
      afterWrite(await client.call('POST', 'import/apply', importBody(document, plan, deletions, shares)));
      done = t('import.done');
      plan = null;
      screenState = 'SCR-12';
    } catch (e) {
      error = message(e);
      screenState = refusalState(e, 'import');
      if (screenState === 'SCR-12.stale') plan = null;
    } finally { busy = false; }
  }
  async function download(): Promise<void> {
    try { save(await currentExport(client), 'hullgate.yaml'); }
    catch (e) { error = message(e); }
  }
</script>

<section class="panel" aria-labelledby="import-title" aria-busy={busy} data-state={screenState}>
  <h2 id="import-title">{t('import.title')}</h2>
  <p class="hint">{t('import.intro')}</p>
  <label>{t('import.file')} <input type="file" accept=".yaml,.yml,application/yaml,text/yaml" onchange={upload} disabled={busy} /></label>
  <label>{t('import.document')} <textarea class="mono" bind:value={document} oninput={invalidate} disabled={busy} rows="10"></textarea></label>
  <div class="buttons">
    <button class="primary" disabled={busy} onclick={preview}>{t('import.preview')}</button>
    <button class="ghost" disabled={busy} onclick={download}>{t('import.export')}</button>
  </div>
  {#if error}<p class="notice" role="alert">{error}</p>{/if}
  {#if done}<p role="status">{done}</p>{/if}
  {#if plan}
    <p class="hint">{t('import.reauthorize', { n: String(plan.reauthorize) })}</p>
    {#if plan.changes.length === 0}<p role="status">{t('import.empty')}</p>{/if}
    {#each plan.changes as change (change.kind + ':' + change.path)}
      <article class="change">
        <h3><span class="badge">{change.action}</span> {change.kind} <code>{change.path}</code></h3>
        <div class="comparison">
          <div><h4>{t('audit.before')}</h4><pre>{JSON.stringify(change.before, null, 2)}</pre></div>
          <div><h4>{t('audit.after')}</h4><pre>{JSON.stringify(change.after, null, 2)}</pre></div>
        </div>
        <h4>{t('import.impact')}</h4><pre>{JSON.stringify(change.impact, null, 2)}</pre>
      </article>
    {/each}
    <label class="check"><input type="checkbox" bind:checked={deletions} disabled={busy} />{t('import.deletions')}</label>
    <label class="check"><input type="checkbox" bind:checked={shares} disabled={busy} />{t('import.shares')}</label>
    <div class="buttons">
      <button class="primary" disabled={busy || plan.changes.length === 0} onclick={apply}>{t('import.apply')}</button>
      <button class="ghost" disabled={busy} onclick={download}>{t('import.export')}</button>
    </div>
  {/if}
</section>

<style>
  .panel { max-width: 75rem; margin: auto; padding: var(--space-4); background: var(--surface-1); border: 1px solid var(--rule); border-radius: var(--radius); }
  h2, h3, h4 { color: var(--title); }
  label { display: flex; flex-direction: column; gap: var(--space-1); margin: var(--space-3) 0; }
  .check { flex-direction: row; align-items: center; }
  .buttons { display: flex; gap: var(--space-2); flex-wrap: wrap; }
  .change { border-top: 1px solid var(--rule); margin-top: var(--space-3); }
  .comparison { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space-3); }
  pre { white-space: pre-wrap; overflow-wrap: anywhere; font-size: .8rem; }
  textarea { width: 100%; }
  @media (max-width: 600px) { .comparison { grid-template-columns: 1fr; } }
</style>
