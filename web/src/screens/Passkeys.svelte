<script lang="ts">
  // The account's passkeys: list, add, delete. Adding and deleting ask for the passkey again when
  // the server answers `fresh_auth_required` (BR-210); the server refuses deleting the last one.
  import { onMount } from 'svelte';
  import { ApiError, type Client, type Passkey } from '../lib/api';
  import { t } from '../lib/i18n';
  import { create, get, type CreationOptionsJSON, type RequestOptionsJSON } from '../lib/webauthn';

  let { client, onClose }: { client: Client; onClose: () => void } = $props();

  let keys = $state<Passkey[]>([]);
  let label = $state('');
  let error = $state('');
  let busy = $state(false);

  async function load(): Promise<void> {
    keys = (await client.call<{ passkeys: Passkey[] }>('GET', 'passkeys')).passkeys;
  }

  async function confirm(): Promise<void> {
    const options = await client.call<{ publicKey: RequestOptionsJSON }>('POST', 'session/fresh/options');
    const credential = await get(options.publicKey);
    await client.call('POST', 'session/fresh', { credential });
  }

  /** Runs `act`; on `fresh_auth_required`, confirms with the passkey and runs it once more. */
  async function fresh<T>(act: () => Promise<T>): Promise<T> {
    try {
      return await act();
    } catch (e) {
      if (!(e instanceof ApiError) || e.code !== 'fresh_auth_required') throw e;
      await confirm();
      return await act();
    }
  }

  async function run(work: () => Promise<void>): Promise<void> {
    busy = true;
    error = '';
    try {
      await work();
      await load();
    } catch (e) {
      error = e instanceof ApiError ? e.message : String(e);
    } finally {
      busy = false;
    }
  }

  function add(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    return run(async () => {
      const options = await fresh(() =>
        client.call<{ publicKey: CreationOptionsJSON }>('POST', 'passkeys/options'),
      );
      const credential = await create(options.publicKey);
      await client.call('POST', 'passkeys', { label, credential });
      label = '';
    });
  }

  function remove(id: string): Promise<void> {
    return run(() => fresh(() => client.call('DELETE', `passkeys/${encodeURIComponent(id)}`)));
  }

  onMount(() => void load());
</script>

<div class="overlay" role="presentation">
  <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="passkeys-title">
    <h2 id="passkeys-title">{t('passkeys.title')}</h2>
    {#if keys.length === 1}
      <p class="hint">{t('passkeys.second_hint')}</p>
    {/if}
    <table>
      <thead>
        <tr><th>{t('passkeys.label')}</th><th>{t('passkeys.created')}</th><th>{t('passkeys.last_used')}</th><th></th></tr>
      </thead>
      <tbody>
        {#each keys as key (key.id)}
          <tr>
            <td>{key.label}</td>
            <td class="mono">{key.created_at}</td>
            <td class="mono">{key.last_used_at ?? t('passkeys.never')}</td>
            <td>
              <button type="button" class="ghost" disabled={busy} onclick={() => remove(key.id)}>
                {t('passkeys.delete')}
              </button>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
    <form onsubmit={add}>
      <label for="new-label">{t('passkeys.label')}</label>
      <input id="new-label" bind:value={label} maxlength="64" required autocomplete="off" />
      <button class="primary" type="submit" disabled={busy || !label.trim()}>{t('passkeys.add')}</button>
    </form>
    <p class="hint">{t('passkeys.fresh_hint')}</p>
    {#if error}
      <p class="notice" role="alert">{error}</p>
    {/if}
    <button type="button" class="ghost" onclick={onClose}>{t('close')}</button>
  </div>
</div>

<style>
  .overlay {
    position: fixed;
    inset: 0;
    background: var(--scrim);
    display: grid;
    place-items: center;
    padding: var(--space-4);
  }
  .dialog {
    width: min(40rem, 100%);
    background: var(--surface-1);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    padding: var(--space-5);
  }
  h2 {
    margin-top: 0;
    color: var(--title);
  }
  table {
    width: 100%;
    border-collapse: collapse;
    margin-bottom: var(--space-4);
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
  form {
    display: flex;
    gap: var(--space-2);
    align-items: center;
    flex-wrap: wrap;
  }
  .hint {
    color: var(--faint);
    font-size: 0.85rem;
  }
</style>
