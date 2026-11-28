<script lang="ts">
  // Inherited settings of a node (BR-048): value and origin as the server computes them; a value
  // set here can be restored to inheritance (SCR-06.overridden). Refusals are the server's.
  import { message, query, type Client, type Setting } from '../lib/api';
  import { t } from '../lib/i18n';
  import { afterWrite } from '../lib/notice.svelte';

  let { client, path, readOnly = false }: { client: Client; path: string; readOnly?: boolean } =
    $props();

  let settings = $state.raw<Setting[]>([]);
  let editing = $state<string | null>(null);
  let draft = $state('');
  let error = $state('');
  let busy = $state(false);

  async function load(): Promise<void> {
    try {
      const read = await client.call<{ settings: Setting[] }>('GET', `settings${query({ path })}`);
      settings = read.settings;
    } catch (e) {
      error = message(e);
    }
  }

  async function run(work: () => Promise<unknown>): Promise<void> {
    busy = true;
    error = '';
    try {
      afterWrite(await work());
      editing = null;
      await load();
    } catch (e) {
      error = message(e);
    } finally {
      busy = false;
    }
  }

  function save(event: SubmitEvent, key: string): Promise<void> {
    event.preventDefault();
    return run(() => client.call('PUT', 'settings', { path, key, value: draft }));
  }

  function unset(key: string): Promise<void> {
    return run(() => client.call('POST', 'settings/unset', { path, key }));
  }

  $effect(() => {
    void path;
    editing = null;
    error = '';
    void load();
  });
</script>

<section class="settings" aria-labelledby="settings-{path}">
  <h3 id="settings-{path}">{t('settings.title')}</h3>
  <p class="hint">{t('settings.hint')}</p>
  <table>
    <tbody>
      {#each settings as s (s.key)}
        <tr data-state={s.here ? 'SCR-06.overridden' : undefined}>
          <th scope="row" class="mono">{s.key}</th>
          <td>
            {#if editing === s.key}
              <form class="edit" onsubmit={(e) => save(e, s.key)}>
                <input aria-label={t('settings.value_of', { key: s.key })} bind:value={draft} data-autofocus />
                <button type="submit" class="primary small" disabled={busy}>{t('save')}</button>
                <button type="button" class="ghost small" onclick={() => (editing = null)}>{t('cancel')}</button>
              </form>
            {:else}
              <span class="mono">{s.value}</span>
            {/if}
          </td>
          <td class="origin">
            {#if s.here}
              <span class="badge">{t('settings.here')}</span>
            {:else}
              {t('settings.from', { origin: s.origin })}
            {/if}
          </td>
          <td class="acts">
            {#if !readOnly && editing !== s.key}
              <div class="stack">
                <button
                  type="button"
                  class="ghost small"
                  onclick={() => ((editing = s.key), (draft = s.value))}>{t('settings.override')}</button
                >
                {#if s.here}
                  <button type="button" class="ghost small" disabled={busy} onclick={() => unset(s.key)}>
                    {t('settings.restore')}
                  </button>
                {/if}
              </div>
            {/if}
          </td>
        </tr>
      {/each}
    </tbody>
  </table>
  {#if error}<p class="notice" role="alert">{error}</p>{/if}
</section>

<style>
  table {
    width: 100%;
    border-collapse: collapse;
    /* the inspector is narrow (§3.6): every column fits its panel, long words wrap */
    table-layout: fixed;
  }
  th,
  td {
    text-align: left;
    padding: var(--space-1) var(--space-1);
    border-bottom: 1px solid var(--rule);
    vertical-align: top;
  }
  th {
    font-weight: 400;
    color: var(--muted);
    width: 38%;
    font-size: 0.85rem;
  }
  th,
  td {
    overflow-wrap: anywhere;
  }
  .origin {
    color: var(--faint);
    font-size: 0.8rem;
  }
  .acts {
    width: 30%;
  }
  .stack {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: var(--space-1);
  }
  .stack button {
    max-width: 100%;
    white-space: normal;
    text-align: right;
  }
  .edit {
    display: flex;
    gap: var(--space-1);
    flex-wrap: wrap;
  }
  .edit input {
    width: 8rem;
  }
</style>
