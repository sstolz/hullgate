<script lang="ts">
  // `connection tools` diagnostics for one placement (F-008 §3.2): the server lists the upstream's
  // tools as the role would expose them, and the names it found invalid.
  import { onMount } from 'svelte';
  import { message, type Client, type Diagnosis } from '../lib/api';
  import { t } from '../lib/i18n';
  import { afterWrite } from '../lib/notice.svelte';
  import Dialog from './Dialog.svelte';

  let {
    client,
    connection,
    role,
    onClose,
  }: { client: Client; connection: string; role: string; onClose: () => void } = $props();

  let result = $state.raw<Diagnosis | null>(null);
  let error = $state('');

  onMount(() => {
    client
      .call<Diagnosis>('POST', 'connections/tools', { name: connection, role })
      .then((r) => (result = afterWrite(r)))
      .catch((e: unknown) => (error = message(e)));
  });
</script>

<Dialog title={t('diag.title', { name: connection })} {onClose} visual="SCR-07.diagnose" wide>
  <p class="mono hint">{role}</p>
  {#if result}
    <p>{t('diag.version', { version: String(result.version ?? '—') })}</p>
    {#if result.tools.length === 0}
      <p class="hint">{t('diag.none')}</p>
    {:else}
      <table>
        <thead>
          <tr><th>{t('diag.name')}</th><th>{t('diag.upstream_name')}</th></tr>
        </thead>
        <tbody>
          {#each result.tools as tool (tool.name)}
            <tr><td class="mono">{tool.name}</td><td class="mono">{tool.upstream_name}</td></tr>
          {/each}
        </tbody>
      </table>
    {/if}
    {#if result.invalid_names.length > 0}
      <p class="notice">{t('diag.invalid', { names: result.invalid_names.join(', ') })}</p>
    {/if}
    {#if result.purged_names.length > 0}
      <p class="hint">{t('diag.purged', { names: result.purged_names.join(', ') })}</p>
    {/if}
  {:else if !error}
    <p class="hint">{t('loading')}</p>
  {/if}
  {#if error}<p class="notice" role="alert">{error}</p>{/if}
  <div class="actions">
    <button type="button" class="primary" onclick={onClose}>{t('close')}</button>
  </div>
</Dialog>

<style>
  table {
    width: 100%;
    border-collapse: collapse;
  }
  th,
  td {
    text-align: left;
    padding: var(--space-1) var(--space-2);
    border-bottom: 1px solid var(--rule);
  }
  th {
    color: var(--muted);
    font-weight: 500;
  }
</style>
