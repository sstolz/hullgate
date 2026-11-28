<script lang="ts">
  // BR-214's dialog: the impact the server computes for an act, shown as it comes (numbers and
  // share signals, BR-079), then the write quoting its digest. On 409 `impact_changed` the new
  // impact is shown and asked again; nothing was done.
  import { onMount } from 'svelte';
  import { ApiError, message, query, type Client, type ImpactRead } from '../lib/api';
  import { impactKey, reasonText, t } from '../lib/i18n';
  import Dialog from './Dialog.svelte';

  let {
    client,
    act,
    params,
    title,
    confirm,
    intro = '',
    write,
    onDone,
    onClose,
  }: {
    client: Client;
    act: string;
    params: Record<string, string>;
    title: string;
    confirm: string;
    intro?: string;
    write: (digest: string) => Promise<unknown>;
    onDone: (answer: unknown) => void;
    onClose: () => void;
  } = $props();

  let read = $state.raw<ImpactRead | null>(null);
  let changed = $state(false);
  let error = $state('');
  let busy = $state(false);

  // while the write runs, neither Cancel, Escape nor × closes the dialog: its answer lands here
  function close(): void {
    if (!busy) onClose();
  }

  async function load(): Promise<void> {
    read = await client.call<ImpactRead>('GET', `impacts${query({ act, ...params })}`);
  }

  function shown(value: unknown): string {
    if (Array.isArray(value)) return String(value.length);
    if (typeof value === 'object' && value !== null) return JSON.stringify(value);
    return String(value);
  }

  async function go(): Promise<void> {
    if (!read) return;
    busy = true;
    error = '';
    try {
      onDone(await write(read.digest));
    } catch (e) {
      if (e instanceof ApiError && e.code === 'impact_changed') {
        changed = true;
        const fresh = e.body.impact as Partial<ImpactRead> | undefined;
        if (fresh && typeof fresh.digest === 'string' && fresh.impact) read = fresh as ImpactRead;
        else await load().catch((x: unknown) => (error = message(x)));
      } else {
        error = message(e);
      }
    } finally {
      busy = false;
    }
  }

  onMount(() => {
    load().catch((e: unknown) => (error = message(e)));
  });
</script>

<Dialog {title} onClose={close} visual="impact">
  {#if intro}<p>{intro}</p>{/if}
  {#if changed}
    <p class="notice" role="alert" data-state="impact-changed">{t('impact.changed')}</p>
  {/if}
  {#if read}
    <p class="lead">{t('impact.lead')}</p>
    <dl class="numbers">
      {#each Object.entries(read.impact) as [key, value] (key)}
        {@const label = impactKey(key)}
        <dt>{label ? t(label) : key}</dt>
        <dd class="mono">{shown(value)}</dd>
        {#if Array.isArray(value) && value.length > 0}
          <dd class="list">
            <ul>
              {#each value as item, i (i)}<li class="mono">{shown(item)}</li>{/each}
            </ul>
          </dd>
        {/if}
      {/each}
    </dl>
    {#if read.shares.length > 0}
      <section class="shares" data-state="impact-shares">
        <h3>{t('impact.shares')}</h3>
        <ul>
          {#each read.shares as share (share.connection)}
            <li>
              <span class="mono">{share.connection}</span>:
              {share.reasons.map(reasonText).join(', ')}
              <ul>
                {#each share.roles as role (role.role)}
                  <li class="mono">
                    {role.role}{#if role.sensitive}<span class="badge sens">{t('badge.sensitive')}</span>{/if}
                  </li>
                {/each}
              </ul>
            </li>
          {/each}
        </ul>
        <p class="hint">{t('share.neighbours')}</p>
      </section>
    {/if}
  {:else if !error}
    <p class="hint">{t('loading')}</p>
  {/if}
  {#if error}<p class="notice" role="alert">{error}</p>{/if}
  <div class="actions">
    <button type="button" class="ghost" disabled={busy} onclick={close}>{t('cancel')}</button>
    <button type="button" class="danger" disabled={busy || !read} onclick={go}>{confirm}</button>
  </div>
</Dialog>

<style>
  .lead {
    color: var(--muted);
  }
  .numbers {
    display: grid;
    grid-template-columns: 1fr auto;
    gap: var(--space-1) var(--space-4);
    margin: 0 0 var(--space-4);
  }
  .numbers dt {
    color: var(--text);
  }
  .numbers dd {
    margin: 0;
    text-align: right;
  }
  .numbers dd.list {
    grid-column: 1 / -1;
    text-align: left;
    color: var(--muted);
  }
  .numbers ul {
    margin: 0;
    padding-left: var(--space-5);
  }
  .shares h3 {
    font-size: 0.95rem;
    margin: 0 0 var(--space-2);
    color: var(--warn);
  }
  .shares ul {
    margin: 0 0 var(--space-2);
    padding-left: var(--space-5);
  }
</style>
