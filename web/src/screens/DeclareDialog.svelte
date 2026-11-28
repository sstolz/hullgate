<script lang="ts">
  // Declaring a service, its MCP upstream, or an outbound application (§3.7). An application secret
  // travels in the POST body only and is never shown back (BR-086, BR-213).
  import { message, type Client, type Service } from '../lib/api';
  import { t } from '../lib/i18n';
  import { afterWrite } from '../lib/notice.svelte';
  import Dialog from './Dialog.svelte';

  let {
    client,
    kind,
    services,
    service = null,
    onDone,
    onClose,
  }: {
    client: Client;
    kind: 'service' | 'upstream' | 'application';
    services: readonly Service[];
    service?: Service | null;
    onDone: () => void;
    onClose: () => void;
  } = $props();

  // service
  let id = $state('');
  let server = $state('');
  let model = $state('');
  let scopes = $state('');
  let internal = $state(false);
  // upstream
  let upType = $state('mcp-public');
  let mcpUrl = $state('');
  let mcpInternal = $state(false);
  let confirm = $state('');
  // application
  let appService = $state('');
  let mode = $state<'declared' | 'dynamic'>('declared');
  let clientId = $state('');
  let secret = $state('');

  let error = $state('');
  let busy = $state(false);

  const titles = {
    service: 'declare.service_title',
    upstream: 'declare.upstream_title',
    application: 'declare.application_title',
  } as const;

  function body(): Record<string, unknown> {
    if (kind === 'service') {
      const b: Record<string, unknown> = { id: id.trim(), server: server.trim() };
      if (model) b.model = model;
      if (scopes.trim()) b.scopes = scopes.trim();
      if (internal) b.internal_host = true;
      return b;
    }
    if (kind === 'upstream') {
      const b: Record<string, unknown> = { id: service?.id ?? '', type: upType, mcp_url: mcpUrl.trim() };
      if (mcpInternal) b.mcp_internal_host = true;
      if (confirm.trim()) b.confirm = confirm.trim();
      return b;
    }
    if (mode === 'dynamic') return { id: id.trim(), service: appService, mode };
    const b: Record<string, unknown> = { id: id.trim(), service: appService, mode, client_id: clientId.trim() };
    if (secret) b.secret = secret;
    return b;
  }

  const routes = { service: 'services', upstream: 'services/upstream', application: 'applications' };

  async function send(payload: Record<string, unknown>): Promise<void> {
    busy = true;
    error = '';
    try {
      afterWrite(await client.call('POST', routes[kind], payload));
      secret = '';
      onDone();
    } catch (e) {
      error = message(e);
    } finally {
      busy = false;
    }
  }

  function submit(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    return send(body());
  }

  function removeUpstream(): Promise<void> {
    return send({ id: service?.id ?? '', remove: true, confirm: confirm.trim() });
  }
</script>

<Dialog title={t(titles[kind], { id: service?.id ?? '' })} {onClose} visual="SCR-07.declare">
  <form class="stack" onsubmit={submit} autocomplete="off">
    {#if kind === 'service'}
      <label>{t('declare.id')} <input bind:value={id} required class="mono" data-autofocus /></label>
      <label>{t('declare.server')} <input bind:value={server} required type="url" class="mono" /></label>
      <label>
        {t('declare.model')}
        <select bind:value={model}>
          <option value="">{t('declare.model_default')}</option>
          <option value="gitea">gitea</option>
          <option value="generic">generic</option>
        </select>
      </label>
      <label>{t('declare.scopes')} <input bind:value={scopes} class="mono" /></label>
      <label class="check"><input type="checkbox" bind:checked={internal} /> {t('declare.internal_host')}</label>
    {:else if kind === 'upstream'}
      <p class="hint">{t('declare.upstream_current', { upstream: service?.upstream ?? '—' })}</p>
      <label>
        {t('declare.upstream_type')}
        <select bind:value={upType} data-autofocus>
          <option value="mcp-public">mcp-public</option>
          <option value="mcp-internal">mcp-internal</option>
        </select>
      </label>
      <label>{t('declare.mcp_url')} <input bind:value={mcpUrl} required type="url" class="mono" /></label>
      <label class="check">
        <input type="checkbox" bind:checked={mcpInternal} />
        {t('declare.mcp_internal_host')}
      </label>
      <label>
        {t('declare.confirm')}
        <input bind:value={confirm} class="mono" />
      </label>
      <p class="hint">{t('declare.confirm_hint')}</p>
    {:else}
      <label>{t('declare.id')} <input bind:value={id} required class="mono" data-autofocus /></label>
      <label>
        {t('app.service')}
        <select bind:value={appService} required>
          <option value="" disabled>{t('choose')}</option>
          {#each services as s (s.id)}<option value={s.id}>{s.id}</option>{/each}
        </select>
      </label>
      <fieldset>
        <legend>{t('app.mode')}</legend>
        <label class="check"><input type="radio" value="declared" bind:group={mode} /> {t('declare.mode_declared')}</label>
        <label class="check"><input type="radio" value="dynamic" bind:group={mode} /> {t('declare.mode_dynamic')}</label>
      </fieldset>
      {#if mode === 'declared'}
        <label>{t('app.client_id')} <input bind:value={clientId} required class="mono" /></label>
        <label>
          {t('declare.secret')}
          <input type="password" bind:value={secret} autocomplete="new-password" />
        </label>
        <p class="hint">{t('conn.secret_hint')}</p>
      {/if}
    {/if}
    {#if error}<p class="notice" role="alert">{error}</p>{/if}
    <div class="actions">
      {#if kind === 'upstream' && service?.upstream}
        <button type="button" class="danger" disabled={busy} onclick={removeUpstream}>{t('declare.upstream_remove')}</button>
      {/if}
      <button type="button" class="ghost" onclick={onClose}>{t('cancel')}</button>
      <button type="submit" class="primary" disabled={busy}>{t('declare.go')}</button>
    </div>
  </form>
</Dialog>
