<script lang="ts">
  // SCR-07.connect and SCR-07.connect-paste (§3.7, BR-216). The server says where the browser goes:
  // `redirect: "web"` sends it to the provider, which comes back to /admin/oauth/callback;
  // `redirect: "paste"` shows the authorisation URL and a field for the return URL (F-006).
  import {
    message,
    type Application,
    type Authorized,
    type Client,
    type Opened,
    type Service,
    type TreeNode,
  } from '../lib/api';
  import { copy } from '../lib/dom';
  import { t } from '../lib/i18n';
  import { afterWrite } from '../lib/notice.svelte';
  import Dialog from './Dialog.svelte';

  let {
    client,
    services,
    applications,
    clients,
    preset = '',
    opened = null,
    onDone,
    onClose,
  }: {
    client: Client;
    services: readonly Service[];
    applications: readonly Application[];
    clients: readonly TreeNode[];
    preset?: string;
    opened?: Opened | null;
    onDone: () => void;
    onClose: () => void;
  } = $props();

  // svelte-ignore state_referenced_locally
  let service = $state(preset);
  let via = $state<'application' | 'provided'>('application');
  let application = $state('');
  let owner = $state('');
  let name = $state('');
  let prefix = $state('');
  let value = $state('');
  let account = $state('');
  let expiry = $state('');
  // svelte-ignore state_referenced_locally
  let paste = $state<Opened | null>(opened?.redirect === 'paste' ? opened : null);
  let callback = $state('');
  let confirmAccount = $state(false);
  let result = $state.raw<Authorized | null>(null);
  let copied = $state('');
  let error = $state('');
  let busy = $state(false);

  const forService = $derived(applications.filter((a) => a.service === service));

  /** Follows the server's answer: web return, paste path, or nothing more to do. */
  function follow(answer: Opened): void {
    if (answer.redirect === 'web' && answer.authorization_url) {
      location.assign(answer.authorization_url);
      return;
    }
    if (answer.redirect === 'paste' && answer.authorization_url) {
      paste = answer;
      return;
    }
    onDone();
  }

  async function submit(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    busy = true;
    error = '';
    const body: Record<string, unknown> = {
      name: `${owner}/${name.trim()}`,
      service,
      prefix: prefix.trim(),
    };
    if (via === 'application') body.application = application;
    else {
      const provided: { value: string; account?: string; expires_at?: string } = { value };
      if (account.trim()) provided.account = account.trim();
    if (expiry) provided.expires_at = expiry;
      body.provided = provided;
    }
    try {
      const answer = afterWrite(await client.call<Opened>('POST', 'connections', body));
      value = '';
      follow(answer);
    } catch (e) {
      error = message(e);
    } finally {
      busy = false;
    }
  }

  async function authorize(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    if (!paste) return;
    busy = true;
    error = '';
    const body: Record<string, unknown> = { name: paste.name, callback: callback.trim() };
    if (confirmAccount) body.confirm_account = true;
    try {
      result = afterWrite(await client.call<Authorized>('POST', 'connections/authorize', body));
      callback = '';
    } catch (e) {
      error = message(e);
    } finally {
      busy = false;
    }
  }

  async function copyUrl(url: string): Promise<void> {
    copied = (await copy(url)) ? t('copied') : t('copy_blocked');
  }
</script>

{#if result}
  <Dialog title={t('connect.done_title')} onClose={onDone} visual="SCR-07">
    <dl>
      <dt>{t('conn.name')}</dt>
      <dd class="mono">{result.name}</dd>
      <dt>{t('conn.account')}</dt>
      <dd class="mono">{result.account}</dd>
      <dt>{t('conn.scopes')}</dt>
      <dd class="mono">{Array.isArray(result.scopes) ? result.scopes.join(' ') : result.scopes}</dd>
      <dt>{t('conn.expiry')}</dt>
      <dd class="mono">{result.expires_at ?? '—'}</dd>
    </dl>
    <div class="actions">
      <button type="button" class="primary" onclick={onDone} data-autofocus>{t('close')}</button>
    </div>
  </Dialog>
{:else if paste}
  <Dialog title={t('connect.paste_title', { name: paste.name })} {onClose} visual="SCR-07.connect-paste" wide>
    <p>{t('connect.paste_intro')}</p>
    <div class="url">
      <code class="mono">{paste.authorization_url}</code>
    </div>
    <div class="row">
      <a class="ghost button" href={paste.authorization_url} target="_blank" rel="noopener noreferrer">
        {t('connect.open')}
      </a>
      <button type="button" class="ghost" onclick={() => copyUrl(paste?.authorization_url ?? '')}>{t('copy')}</button>
      {#if copied}<span class="hint" role="status">{copied}</span>{/if}
    </div>
    <form class="stack" onsubmit={authorize} autocomplete="off">
      <label>
        {t('connect.paste_field')}
        <input bind:value={callback} required class="mono" data-autofocus />
      </label>
      <label class="check">
        <input type="checkbox" bind:checked={confirmAccount} />
        {t('connect.confirm_account')}
      </label>
      {#if error}<p class="notice" role="alert">{error}</p>{/if}
      <div class="actions">
        <button type="button" class="ghost" onclick={onClose}>{t('cancel')}</button>
        <button type="submit" class="primary" disabled={busy || !callback.trim()}>{t('connect.authorize')}</button>
      </div>
    </form>
  </Dialog>
{:else}
  <Dialog title={t('connect.title')} {onClose} visual="SCR-07.connect">
    <form class="stack" onsubmit={submit} autocomplete="off">
      <label>
        {t('app.service')}
        <select bind:value={service} required data-autofocus>
          <option value="" disabled>{t('choose')}</option>
          {#each services as s (s.id)}<option value={s.id}>{s.id}</option>{/each}
        </select>
      </label>
      <fieldset>
        <legend>{t('connect.via')}</legend>
        <label class="check"><input type="radio" value="application" bind:group={via} /> {t('connect.via_application')}</label>
        <label class="check"><input type="radio" value="provided" bind:group={via} /> {t('connect.via_provided')}</label>
      </fieldset>
      {#if via === 'application'}
        <label>
          {t('conn.application')}
          <select bind:value={application} required>
            <option value="" disabled>{t('choose')}</option>
            {#each forService as a (a.id)}
              <option value={a.id}>{a.id} ({a.mode}{a.web_return ? `, ${t('app.web_return_on')}` : ''})</option>
            {/each}
          </select>
        </label>
      {:else}
        <label>
          {t('conn.token_value')}
          <input type="password" bind:value={value} required autocomplete="new-password" />
        </label>
        <label>{t('conn.account_optional')} <input bind:value={account} /></label>
        <label>{t('conn.expiry_optional')} <input bind:value={expiry} placeholder="2026-12-01T00:00:00Z" /></label>
        <p class="hint">{t('conn.secret_hint')}</p>
      {/if}
      <div class="name">
        <label>
          {t('connect.client')}
          <select bind:value={owner} required>
            <option value="" disabled>{t('choose')}</option>
            {#each clients as c (c.id)}<option value={c.segment}>{c.segment}</option>{/each}
          </select>
        </label>
        <span class="slash" aria-hidden="true">/</span>
        <label>
          {t('connect.name')}
          <input bind:value={name} required class="mono" />
        </label>
      </div>
      <label>
        {t('conn.prefix')}
        <input bind:value={prefix} required maxlength="8" class="mono" />
      </label>
      <p class="hint">{t('conn.prefix_hint')}</p>
      {#if error}<p class="notice" role="alert">{error}</p>{/if}
      <div class="actions">
        <button type="button" class="ghost" onclick={onClose}>{t('cancel')}</button>
        <button type="submit" class="primary" disabled={busy}>{t('connect.go')}</button>
      </div>
    </form>
  </Dialog>
{/if}

<style>
  .url code {
    display: block;
    word-break: break-all;
    background: var(--surface-2);
    padding: var(--space-2);
    border-radius: var(--radius);
  }
  .row {
    display: flex;
    gap: var(--space-2);
    align-items: center;
    margin: var(--space-2) 0 var(--space-4);
  }
  .name {
    display: flex;
    align-items: flex-end;
    gap: var(--space-2);
  }
  .name label {
    flex: 1;
  }
  .slash {
    padding-bottom: var(--space-2);
    color: var(--muted);
  }
  dl {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: var(--space-1) var(--space-3);
  }
  dt {
    color: var(--muted);
  }
  dd {
    margin: 0;
    word-break: break-all;
  }
</style>
