<script lang="ts">
  // SCR-07.share (BR-078 v3, BR-079): the server refused a placement that would share a connection.
  // The warning is the server's; a dedicated connection is recommended; placing anyway needs a
  // checkbox that starts unticked.
  import { reasonText, t } from '../lib/i18n';
  import Dialog from './Dialog.svelte';

  let {
    connection,
    role,
    warning,
    reasons,
    onConfirm,
    onDedicated,
    onClose,
  }: {
    connection: string;
    role: string;
    warning: string;
    reasons: string[];
    onConfirm: () => Promise<void>;
    onDedicated: () => void;
    onClose: () => void;
  } = $props();

  let ticked = $state(false);
  let busy = $state(false);

  async function place(): Promise<void> {
    busy = true;
    try {
      await onConfirm();
    } finally {
      busy = false;
    }
  }
</script>

<Dialog title={t('share.title', { name: connection })} {onClose} visual="SCR-07.share">
  <p class="mono target">{role}</p>
  <p class="warning" role="alert">{warning}</p>
  {#if reasons.length > 0}
    <ul>
      {#each reasons as reason (reason)}<li>{reasonText(reason)}</li>{/each}
    </ul>
  {/if}
  <p class="hint">{t('share.neighbours')}</p>
  <section class="reco">
    <h3>{t('share.reco_title')}</h3>
    <p>{t('share.reco')}</p>
    <button type="button" class="primary" onclick={onDedicated}>{t('share.dedicated')}</button>
  </section>
  <label class="check">
    <input type="checkbox" bind:checked={ticked} />
    {t('share.ack')}
  </label>
  <div class="actions">
    <button type="button" class="ghost" onclick={onClose}>{t('cancel')}</button>
    <button type="button" class="danger" disabled={!ticked || busy} onclick={place}>
      {t('share.place_anyway')}
    </button>
  </div>
</Dialog>

<style>
  .target {
    color: var(--muted);
    margin-top: 0;
  }
  .warning {
    color: var(--warn);
  }
  .reco {
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    padding: var(--space-3);
    margin: var(--space-3) 0;
  }
  .reco h3 {
    margin: 0 0 var(--space-1);
    font-size: 0.95rem;
  }
  .check {
    display: flex;
    gap: var(--space-2);
    align-items: flex-start;
  }
</style>
