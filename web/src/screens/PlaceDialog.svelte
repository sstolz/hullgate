<script lang="ts">
  // "Place in…" (BR-232): the keyboard path of a drag, and the dialog of a drop on a project or an
  // environment (§3.5). The roles are listed; the server decides each placement.
  import type { TreeNode } from '../lib/api';
  import { t } from '../lib/i18n';
  import Dialog from './Dialog.svelte';

  let {
    connection,
    roles,
    placed,
    target = null,
    onPlace,
    onClose,
  }: {
    connection: string;
    roles: TreeNode[];
    placed: ReadonlySet<string>;
    target?: TreeNode | null;
    onPlace: (paths: string[]) => void;
    onClose: () => void;
  } = $props();

  let picked = $state<string[]>([]);
  const firstFree = $derived(roles.find((r) => !placed.has(r.path))?.path);

  function submit(event: SubmitEvent): void {
    event.preventDefault();
    if (picked.length > 0) onPlace([...picked]);
  }
</script>

<Dialog title={t('place.title', { name: connection })} {onClose} visual="place">
  <p class="hint">
    {target ? t('place.dropped', { path: target.path }) : t('place.keyboard')}
  </p>
  <form onsubmit={submit}>
    {#if roles.length === 0}
      <p class="hint">{t('place.no_roles')}</p>
    {:else}
      <fieldset>
        <legend class="sr">{t('place.roles')}</legend>
        {#each roles as role (role.id)}
          <label class="row">
            <input
              type="checkbox"
              value={role.path}
              bind:group={picked}
              disabled={placed.has(role.path)}
              data-autofocus={role.path === firstFree ? '' : undefined}
            />
            <span class="mono">{role.path}</span>
            {#if role.sensitive}<span class="badge sens">{t('badge.sensitive')}</span>{/if}
            {#if role.locked_by}<span class="badge lock">{t('badge.locked')}</span>{/if}
            {#if placed.has(role.path)}<span class="hint">{t('place.already')}</span>{/if}
          </label>
        {/each}
      </fieldset>
    {/if}
    <div class="actions">
      <button type="button" class="ghost" onclick={onClose}>{t('cancel')}</button>
      <button type="submit" class="primary" disabled={picked.length === 0}>
        {t('place.go', { n: String(picked.length) })}
      </button>
    </div>
  </form>
</Dialog>

<style>
  fieldset {
    border: none;
    margin: 0;
    padding: 0;
    max-height: 50vh;
    overflow: auto;
  }
  .row {
    display: flex;
    gap: var(--space-2);
    align-items: center;
    padding: var(--space-1) 0;
  }
</style>
