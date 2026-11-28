<script lang="ts">
  // BR-216: the provider's return to /admin/oauth/callback. The address bar was cleared before any
  // call (App); `state` and `code` go by POST under the session, the outcome is the server's.
  import { onMount } from 'svelte';
  import { message, type Authorized, type Client } from '../lib/api';
  import { t } from '../lib/i18n';
  import { afterWrite } from '../lib/notice.svelte';
  import type { Callback } from '../lib/routes';

  let {
    client,
    callback,
    onDone,
  }: { client: Client; callback: Callback; onDone: () => void } = $props();

  let result = $state.raw<Authorized | null>(null);
  let error = $state('');
  let continueButton = $state<HTMLButtonElement | null>(null);

  onMount(() => {
    const body: Record<string, string> = { state: callback.state };
    if (callback.code !== null) body.code = callback.code;
    if (callback.error !== null) body.error = callback.error;
    client
      .call<Authorized>('POST', 'connections/callback', body)
      .then((r) => (result = afterWrite(r)))
      .catch((e: unknown) => (error = message(e)))
      .finally(() => queueMicrotask(() => continueButton?.focus()));
  });
</script>

<section class="panel" aria-labelledby="callback-title" data-state="SCR-07.callback">
  <h2 id="callback-title">{t('callback.title')}</h2>
  {#if result}
    <p class="ok" role="status">{t('callback.done', { name: result.name })}</p>
    <dl>
      <dt>{t('conn.account')}</dt>
      <dd class="mono">{result.account}</dd>
      <dt>{t('conn.scopes')}</dt>
      <dd class="mono">{Array.isArray(result.scopes) ? result.scopes.join(' ') : result.scopes}</dd>
      <dt>{t('conn.expiry')}</dt>
      <dd class="mono">{result.expires_at ?? '—'}</dd>
    </dl>
  {:else if error}
    <p class="notice" role="alert">{error}</p>
  {:else}
    <p class="hint">{t('callback.working')}</p>
  {/if}
  <button type="button" class="primary" bind:this={continueButton} disabled={!result && !error} onclick={onDone}>
    {t('callback.continue')}
  </button>
</section>

<style>
  .panel {
    max-width: 36rem;
    margin: var(--space-6) auto;
    background: var(--surface-1);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    padding: var(--space-5);
  }
  h2 {
    margin-top: 0;
    color: var(--title);
  }
  dl {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: var(--space-1) var(--space-3);
  }
  dt {
    color: var(--muted);
  }
  dd {
    margin: 0;
    word-break: break-all;
  }
</style>
