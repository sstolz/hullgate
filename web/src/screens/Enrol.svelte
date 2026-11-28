<script lang="ts">
  // SCR-02: opened from an enrolment link; shows the account's e-mail, asks for a passkey label and
  // runs the registration ceremony. On success the session opens (BR-201, BR-203).
  import { onMount } from 'svelte';
  import { ApiError, type Client, type Administrator, type SignedIn } from '../lib/api';
  import { t } from '../lib/i18n';
  import { create, supported, type CreationOptionsJSON } from '../lib/webauthn';
  import Card from './Card.svelte';

  let {
    client,
    token,
    onSignedIn,
  }: { client: Client; token: string; onSignedIn: (a: Administrator) => void } = $props();

  interface Options {
    email: string;
    name: string;
    publicKey: CreationOptionsJSON;
  }

  type Phase = 'loading' | 'nominal' | 'expired' | 'unsupported' | 'failed' | 'limited';
  let phase = $state<Phase>(supported() ? 'loading' : 'unsupported');
  let email = $state('');
  let label = $state('');
  let busy = $state(false);

  async function options(): Promise<Options | null> {
    try {
      return await client.call<Options>('POST', 'enrol/options', { link: token });
    } catch (error) {
      phase = error instanceof ApiError && error.status === 429 ? 'limited' : 'expired';
      return null;
    }
  }

  onMount(async () => {
    if (phase === 'unsupported') return;
    const first = await options();
    if (first) {
      email = first.email;
      phase = 'nominal';
    }
  });

  async function enrol(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    busy = true;
    try {
      const fresh = await options(); // a new challenge for this attempt
      if (!fresh) return;
      const credential = await create(fresh.publicKey);
      const result = await client.call<SignedIn>('POST', 'enrol/verify', {
        link: token,
        label,
        credential,
      });
      onSignedIn(client.signedIn(result));
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) phase = 'expired';
      else if (error instanceof ApiError && error.status === 429) phase = 'limited';
      else phase = 'failed';
    } finally {
      busy = false;
    }
  }
</script>

<Card title={t('enrol.title')}>
  {#if phase === 'unsupported'}
    <p class="notice" role="alert" data-state="SCR-02.unsupported">{t('enrol.unsupported')}</p>
  {:else if phase === 'expired'}
    <p class="notice" role="alert" data-state="SCR-02.expired">{t('enrol.expired')}</p>
  {:else if phase === 'limited'}
    <p class="notice" role="alert">{t('signin.limited')}</p>
  {:else if phase !== 'loading'}
    <form onsubmit={enrol}>
      <p class="field"><span class="key">{t('enrol.account')}</span> <span class="mono">{email}</span></p>
      <label for="label">{t('enrol.label')}</label>
      <input id="label" bind:value={label} required maxlength="64" autocomplete="off" />
      <p class="hint">{t('enrol.label_hint')}</p>
      {#if phase === 'failed'}
        <p class="notice" role="alert">{t('enrol.failed')}</p>
      {/if}
      <button class="primary" type="submit" disabled={busy || !label.trim()}>{t('enrol.button')}</button>
    </form>
  {/if}
</Card>

<style>
  .field {
    margin: 0 0 var(--space-4);
  }
  .key {
    color: var(--muted);
  }
  label {
    display: block;
    margin-bottom: var(--space-1);
  }
  input {
    width: 100%;
  }
  .hint {
    color: var(--faint);
    font-size: 0.85rem;
    margin: var(--space-1) 0 var(--space-4);
  }
</style>
