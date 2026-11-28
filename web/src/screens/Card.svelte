<script lang="ts">
  // The centred card of the sign-in and enrolment screens, under the wordmark.
  import type { Snippet } from 'svelte';
  import { t } from '../lib/i18n';

  let { title, children }: { title: string; children: Snippet } = $props();
</script>

<main class="page">
  <div class="card">
    <p class="wordmark">{t('wordmark')}</p>
    <h1>{title}</h1>
    {@render children()}
  </div>
</main>

<style>
  .page {
    min-height: 100vh;
    display: grid;
    place-items: center;
    padding: var(--space-4);
  }
  .card {
    width: min(26rem, 100%);
    background: var(--surface-1);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    padding: var(--space-6);
  }
  .wordmark {
    font-family: var(--font-mono);
    letter-spacing: 0.2em;
    color: var(--cyan);
    margin: 0 0 var(--space-2);
  }
  h1 {
    font-size: 1.25rem;
    margin: 0 0 var(--space-4);
    color: var(--title);
  }
</style>
