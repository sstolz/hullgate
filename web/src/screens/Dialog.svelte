<script lang="ts">
  // A modal dialog: focus kept inside, Escape closes it, focus returns where it was (BR-232).
  import type { Snippet } from 'svelte';
  import { trap } from '../lib/dom';
  import { t } from '../lib/i18n';

  let {
    title,
    onClose,
    wide = false,
    visual = '',
    children,
  }: {
    title: string;
    onClose: () => void;
    wide?: boolean;
    visual?: string;
    children: Snippet;
  } = $props();

  const id = `dlg-${Math.random().toString(36).slice(2, 9)}`;
</script>

<div class="overlay" role="presentation">
  <div
    class="dialog"
    class:wide
    role="dialog"
    aria-modal="true"
    aria-labelledby={id}
    tabindex="-1"
    data-state={visual || undefined}
    use:trap={onClose}
  >
    <header>
      <h2 {id}>{title}</h2>
      <button type="button" class="ghost close" aria-label={t('close')} onclick={onClose}>×</button>
    </header>
    {@render children()}
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
    z-index: 50;
  }
  .dialog {
    width: min(34rem, 100%);
    max-height: calc(100vh - 2 * var(--space-4));
    overflow: auto;
    background: var(--surface-1);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    padding: var(--space-4) var(--space-5) var(--space-5);
  }
  .dialog.wide {
    width: min(48rem, 100%);
  }
  header {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    margin-bottom: var(--space-3);
  }
  h2 {
    flex: 1;
    margin: 0;
    font-size: 1.05rem;
    color: var(--title);
  }
  .close {
    border: none;
    font-size: 1.2rem;
    line-height: 1;
  }
</style>
