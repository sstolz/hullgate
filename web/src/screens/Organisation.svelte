<script lang="ts">
  import { onMount } from 'svelte';
  import { ApiError, message, type Administrator, type Application, type Client } from '../lib/api';
  import { copy } from '../lib/dom';
  import { t } from '../lib/i18n';
  import { get, type RequestOptionsJSON } from '../lib/webauthn';
  import Passkeys from './Passkeys.svelte';
  import SetupGuide from './SetupGuide.svelte';
  import ImpactDialog from './ImpactDialog.svelte';

  let { client }: { client: Client } = $props();
  interface Organisation {
    name: string; administrator: Administrator; passkeys: number; last_sign_in: string | null;
    public_url: string; admin_url: string; rp_id: string; connection_expiry_days: number;
    applications: Application[]; web_redirect_uri: string;
  }
  let org = $state<Organisation | null>(null);
  let name = $state('');
  let days = $state('7');
  let cidrs = $state('');
  let address = $state('');
  let error = $state('');
  let done = $state('');
  let busy = $state(false);
  let visual = $state('SCR-13');
  let keys = $state(false);
  let guide = $state(false);
  let rename = $state<string | null>(null);
  let pendingCidrs = $state<string[] | null>(null);

  async function load(): Promise<void> {
    const [read, list] = await Promise.all([
      client.call<Organisation>('GET', 'organisation'),
      client.call<{cidrs: string[]; client_address: string}>('GET', 'allowlist'),
    ]);
    org = read; name = read.name; days = String(read.connection_expiry_days);
    cidrs = list.cidrs.join('\n'); address = list.client_address;
  }
  onMount(() => { void load().catch(e => error = message(e)); });
  async function run(work: () => Promise<unknown>): Promise<void> {
    if (busy) return;
    busy = true; error = ''; done = '';
    try { await work(); await load(); done = t('done'); visual = 'SCR-13'; }
    catch (e) { error = message(e); if (e instanceof ApiError && e.code === 'allowlist_self') visual = 'SCR-13.allowlist-self'; }
    finally { busy = false; }
  }
  async function allowlist(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    const wanted = cidrs.split('\n').map(s => s.trim()).filter(Boolean);
    await run(async () => {
      try { await client.call('PUT', 'allowlist', {cidrs: wanted}); }
      catch (e) {
        if (!(e instanceof ApiError) || e.code !== 'fresh_auth_required') throw e;
        pendingCidrs = wanted;
        throw e;
      }
    });
    if (pendingCidrs) visual = 'SCR-13.fresh-required';
  }
  async function confirm(): Promise<void> {
    const wanted = pendingCidrs;
    await run(async () => {
      const options = await client.call<{publicKey: RequestOptionsJSON}>('POST', 'session/fresh/options');
      await client.call('POST', 'session/fresh', {credential: await get(options.publicKey)});
      pendingCidrs = null;
      await client.call('PUT', 'allowlist', {cidrs: wanted});
    });
  }
  async function copyUri(uri: string): Promise<void> { done = await copy(uri) ? t('copied') : t('copy_blocked'); }
</script>

<div class="organisation" data-state={visual} aria-busy={busy}>
  <h2>{t('org.title')}</h2>
  {#if error}<p class="notice" role="alert">{error}</p>{/if}
  {#if done}<p class="ok" role="status">{done}</p>{/if}
  {#if org}
    <section>
      <form onsubmit={e => { e.preventDefault(); rename = name; }}>
        <label>{t('org.name')}<input bind:value={name} /></label>
        <button class="primary">{t('save')}</button>
      </form>
      <h3>{t('org.account')}</h3>
      <p>{org.administrator.name} — {org.administrator.email}</p>
      <p>{t('org.keys', {n: String(org.passkeys)})} · {t('org.last')}: {org.last_sign_in ?? '—'}</p>
      <button class="ghost" onclick={() => keys = true}>{t('shell.passkeys')}</button>
      <p class="hint">{t('org.recovery')}</p>
    </section>
    <section>
      <h3>{t('org.urls')}</h3>
      <dl><dt>{t('org.public')}</dt><dd><code>{org.public_url}</code></dd>
        <dt>{t('org.admin')}</dt><dd><code>{org.admin_url}</code></dd>
        <dt>{t('org.rp')}</dt><dd><code>{org.rp_id}</code></dd></dl>
    </section>
    <section>
      <form onsubmit={allowlist}>
        <label>{t('org.allowlist')}<textarea rows="4" bind:value={cidrs}></textarea></label>
        <p class="hint">{t('org.address', {address})}. {t('org.allowlist_hint')}</p>
        <button class="primary">{t('save')}</button>
      </form>
      {#if pendingCidrs}
        <p>{t('org.fresh')}</p><button class="primary" onclick={confirm}>{t('org.confirm')}</button>
        <button class="ghost" onclick={() => {pendingCidrs = null; visual = 'SCR-13'; error = '';}}>{t('cancel')}</button>
      {/if}
    </section>
    <section>
      <form onsubmit={e => {e.preventDefault(); void run(() => client.call('PUT', 'settings', {path: '/', key: 'connection_expiry_days', value: days}));}}>
        <label>{t('org.expiry')}<input inputmode="numeric" bind:value={days} /></label>
        <button class="primary">{t('save')}</button>
      </form>
    </section>
    <section>
      <h3>{t('org.apps')}</h3>
      <p>{t('org.web_redirect')}: <code>{org.web_redirect_uri}</code> <button class="ghost small" onclick={() => copyUri(org!.web_redirect_uri)}>{t('copy')}</button></p>
      {#each org.applications as app (app.id)}
        <p><strong>{app.id}</strong> — {app.service}</p>
        <p>{t('org.redirect')}: <code>{app.redirect_uri}</code> <button class="ghost small" disabled={!app.redirect_uri} onclick={() => copyUri(app.redirect_uri ?? '')}>{t('copy')}</button></p>
      {/each}
    </section>
    <button class="ghost" onclick={() => guide = !guide}>{t('setup.title')}</button>
    {#if guide}<SetupGuide {client} />{/if}
  {:else}<p>{t('loading')}</p>{/if}
</div>
{#if keys}<Passkeys {client} onClose={() => {keys = false; void load().catch(e => error = message(e));}} />{/if}
{#if rename !== null}
  <ImpactDialog {client} title={t('org.name')} act="tree.rename" params={{path: '/', segment: ''}}
    confirm={t('save')} write={digest => client.call('POST', 'nodes/rename', {path: '/', name: rename, impact: digest})} onClose={() => rename = null}
    onDone={() => {rename = null; void load().catch(e => error = message(e));}} />
{/if}

<style>
  .organisation { max-width: 60rem; }
  section { padding: var(--space-4); background: var(--surface-1); border: 1px solid var(--rule); border-radius: var(--radius); margin-bottom: var(--space-3); }
  h2, h3 { color: var(--title); }
  h3 { margin-top: 0; }
  label { display: flex; flex-direction: column; gap: var(--space-2); margin-bottom: var(--space-2); }
  input, textarea { max-width: 34rem; }
  code { overflow-wrap: anywhere; }
  dd { margin-bottom: var(--space-2); }
</style>
