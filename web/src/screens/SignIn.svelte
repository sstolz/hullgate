<script lang="ts">
  // SCR-01: one button, a discoverable credential, so the page reveals no account (BR-202).
  import { ApiError, type Client, type Administrator, type SignedIn } from '../lib/api';
  import { t } from '../lib/i18n';
  import { get, supported, type RequestOptionsJSON } from '../lib/webauthn';
  import Card from './Card.svelte';

  let { client, onSignedIn }: { client: Client; onSignedIn: (a: Administrator) => void } =
    $props();

  type Phase = 'nominal' | 'refused' | 'limited' | 'blocked' | 'unsupported';
  let phase = $state<Phase>(supported() ? 'nominal' : 'unsupported');
  let busy = $state(false);

  const messages: Partial<Record<Phase, string>> = {
    refused: t('signin.refused'),
    limited: t('signin.limited'),
    blocked: t('signin.blocked'),
  };

  async function signIn(): Promise<void> {
    busy = true;
    try {
      const options = await client.call<{ publicKey: RequestOptionsJSON }>('POST', 'auth/options');
      const credential = await get(options.publicKey);
      onSignedIn(client.signedIn(await client.call<SignedIn>('POST', 'auth/assert', { credential })));
    } catch (error) {
      if (error instanceof ApiError && error.status === 429) phase = 'limited';
      else if (error instanceof ApiError && error.status === 403) phase = 'blocked';
      else phase = 'refused';
    } finally {
      busy = false;
    }
  }
</script>

<Card title={t('signin.title')}>
  {#if phase === 'unsupported'}
    <p class="notice" role="alert" data-state="SCR-01.unsupported">{t('signin.unsupported')}</p>
  {:else}
    <p class="hint">{t('signin.hint')}</p>
    {#if messages[phase]}
      <p class="notice" role="alert" data-state={`SCR-01.${phase}`}>{messages[phase]}</p>
    {/if}
    <button class="primary" type="button" onclick={signIn} disabled={busy || phase === 'blocked'}>
      {t('signin.button')}
    </button>
  {/if}
</Card>

<style>
  .hint {
    color: var(--muted);
    margin: 0 0 var(--space-4);
  }
</style>
