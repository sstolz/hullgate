<script lang="ts">
  import { getContext, onMount } from 'svelte';
  import { SETUP_NAVIGATION, type SetupNavigation } from '../lib/routes';
  import { message, type Client } from '../lib/api';
  import { copy } from '../lib/dom';
  import { t, type Key } from '../lib/i18n';

  let { client, automatic = false }: { client: Client; automatic?: boolean } = $props();
  const navigate = getContext<SetupNavigation | undefined>(SETUP_NAVIGATION);
  function follow(event: MouseEvent, url: string): void {
    // Preserve the anchor's new-tab, copy-link and modified-click behaviour.
    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    if (navigate?.(url)) event.preventDefault();
  }
  interface Setup {
    show: boolean;
    dismissed: boolean;
    steps: { id: string; done: boolean; url: string }[];
    redirect_uris: string[];
    services: { id: string; kind: string; scopes: string[] }[];
  }
  const labels: Record<string, Key> = { client: 'setup.client', service: 'setup.service', connection: 'setup.connection', placement: 'setup.placement', credential: 'setup.credential' };
  let guideState = $state<Setup | null>(null);
  let error = $state('');
  let copied = $state('');
  function refresh(): void {
    client.call<Setup>('GET', 'setup').then(r => guideState = r).catch((e: unknown) => error = message(e));
  }
  onMount(refresh);
  async function dismiss(): Promise<void> {
    try {
      await client.call('POST', 'setup/dismiss');
      guideState = await client.call<Setup>('GET', 'setup');
    } catch (e) { error = message(e); }
  }
  async function copyUri(uri: string): Promise<void> {
    copied = await copy(uri) ? t('copied') : t('copy_blocked');
  }
</script>

{#if error}<p class="notice" role="alert">{error}</p>{/if}
{#if guideState && (!automatic || guideState.show)}
  <section class="guide" data-state={guideState.dismissed ? 'SCR-14.done' : 'SCR-14'} aria-label={t('setup.title')}>
    <h2>{t('setup.title')}</h2>
    <p class="hint">{t('setup.intro')}</p>
    <button class="ghost" onclick={refresh}>{t('setup.refresh')}</button>
    {#if guideState.dismissed}<p>{t('setup.dismissed')}</p>{/if}
    <ol>
      {#each guideState.steps as step (step.id)}
        <li><a href={step.url} onclick={(event) => follow(event, step.url)}>{t(labels[step.id] ?? 'setup.title')}</a> — {step.done ? t('setup.complete') : t('setup.pending')}</li>
      {/each}
    </ol>
    <p>{t('setup.oauth')}</p>
    {#each guideState.redirect_uris as uri (uri)}
      <p><code>{uri}</code> <button class="ghost small" onclick={() => copyUri(uri)}>{t('copy')}</button></p>
    {/each}
    <p class="hint">{t('setup.scope_hint')}</p>
    {#each guideState.services as service (service.id)}
      <p>{service.id} ({service.kind}) — {t('setup.scopes')}: <code>{service.scopes.join(' ') || '—'}</code></p>
    {/each}
    {#if copied}<p role="status">{copied}</p>{/if}
    {#if !guideState.dismissed}<button class="ghost" onclick={dismiss}>{t('dismiss')}</button>{/if}
  </section>
{/if}

<style>
  .guide { padding: var(--space-4); border: 1px solid var(--rule); border-radius: var(--radius); background: var(--surface-1); margin-bottom: var(--space-4); }
  h2 { margin-top: 0; color: var(--title); }
  li { margin: var(--space-2) 0; }
  code { overflow-wrap: anywhere; }
</style>
