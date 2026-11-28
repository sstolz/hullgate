<script lang="ts">
  // SCR-07, the other items of the Library: a service (with its MCP upstream, BR-106) and an
  // outbound application (with its web return flag, BR-216). Values are the server's.
  import { message, type Application, type Client, type Service } from '../lib/api';
  import { copy } from '../lib/dom';
  import { t } from '../lib/i18n';
  import { afterWrite } from '../lib/notice.svelte';

  let {
    client,
    service = null,
    application = null,
    webRedirectUri = '',
    onUpstream,
    onRemove,
    onChanged,
  }: {
    client: Client;
    service?: Service | null;
    application?: Application | null;
    webRedirectUri?: string;
    onUpstream: (s: Service) => void;
    onRemove: (kind: 'service' | 'application', id: string) => void;
    onChanged: () => void;
  } = $props();

  let error = $state('');
  let copied = $state('');
  let busy = $state(false);

  function scopes(s: Service): string {
    return Array.isArray(s.scopes) ? s.scopes.join(' ') : (s.scopes ?? '');
  }

  async function webReturn(a: Application, registered: boolean): Promise<void> {
    busy = true;
    error = '';
    try {
      afterWrite(await client.call('PUT', 'applications/web-return', { id: a.id, registered }));
      onChanged();
    } catch (e) {
      error = message(e);
    } finally {
      busy = false;
    }
  }

  async function copyUri(): Promise<void> {
    copied = (await copy(webRedirectUri)) ? t('copied') : t('copy_blocked');
  }
</script>

{#if service}
  <section class="sheet" aria-labelledby="item-title" data-state="SCR-07">
    <p class="kind">{t('svc.kind')}</p>
    <h2 id="item-title" class="mono">{service.id}</h2>
    <dl>
      <dt>{t('svc.model')}</dt>
      <dd>{service.model}</dd>
      <dt>{t('svc.url')}</dt>
      <dd class="mono">{service.url}</dd>
      <dt>{t('svc.scopes')}</dt>
      <dd class="mono">{scopes(service) || '—'}</dd>
      <dt>{t('svc.internal_host')}</dt>
      <dd>{service.internal_host ? t('yes') : t('no')}</dd>
      {#if service.resource}
        <dt>{t('svc.resource')}</dt>
        <dd class="mono">{service.resource}</dd>
      {/if}
      <dt>{t('svc.upstream')}</dt>
      <dd class="mono">{service.upstream ?? '—'}</dd>
    </dl>
    <div class="acts">
      <button type="button" class="ghost small" onclick={() => service && onUpstream(service)}>{t('svc.upstream_edit')}</button>
      <button type="button" class="danger small" onclick={() => service && onRemove('service', service.id)}>
        {t('svc.remove')}
      </button>
    </div>
    {#if error}<p class="notice" role="alert">{error}</p>{/if}
  </section>
{:else if application}
  <section class="sheet" aria-labelledby="item-title" data-state="SCR-07">
    <p class="kind">{t('app.kind')}</p>
    <h2 id="item-title" class="mono">{application.id}</h2>
    <dl>
      <dt>{t('app.service')}</dt>
      <dd class="mono">{application.service}</dd>
      <dt>{t('app.mode')}</dt>
      <dd>{application.mode}</dd>
      <dt>{t('app.client_id')}</dt>
      <dd class="mono">{application.client_id ?? '—'}</dd>
      <dt>{t('app.redirect_uri')}</dt>
      <dd class="mono">{application.redirect_uri ?? '—'}</dd>
      <dt>{t('app.secret')}</dt>
      <dd>{application.secret ? (typeof application.secret === 'string' ? application.secret : t('yes')) : t('no')}</dd>
      {#if application.secret_expires_at}
        <dt>{t('app.secret_expires')}</dt>
        <dd class="mono">{application.secret_expires_at}</dd>
      {/if}
      {#if application.method}
        <dt>{t('app.method')}</dt>
        <dd class="mono">{application.method}</dd>
      {/if}
    </dl>

    <h3>{t('app.web_return')}</h3>
    <p class="hint">{t('app.web_return_hint')}</p>
    {#if webRedirectUri}
      <div class="uri">
        <code class="mono">{webRedirectUri}</code>
        <button type="button" class="ghost small" onclick={copyUri}>{t('copy')}</button>
      </div>
      {#if copied}<p class="hint" role="status">{copied}</p>{/if}
    {/if}
    <label class="check">
      <input
        type="checkbox"
        checked={application.web_return}
        disabled={busy}
        onchange={(e) => application && webReturn(application, e.currentTarget.checked)}
      />
      {t('app.web_return_check')}
    </label>
    <div class="acts">
      <button
        type="button"
        class="danger small"
        onclick={() => application && onRemove('application', application.id)}>{t('app.remove')}</button
      >
    </div>
    {#if error}<p class="notice" role="alert">{error}</p>{/if}
  </section>
{/if}

<style>
  .kind {
    margin: 0;
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--faint);
  }
  h2 {
    margin: 0 0 var(--space-2);
    font-size: 1.05rem;
    color: var(--title);
  }
  h3 {
    margin: var(--space-4) 0 var(--space-1);
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--muted);
  }
  dl {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: var(--space-1) var(--space-3);
    margin: 0;
  }
  dt {
    color: var(--muted);
  }
  dd {
    margin: 0;
    word-break: break-all;
  }
  .uri {
    display: flex;
    gap: var(--space-2);
    align-items: center;
    margin-bottom: var(--space-2);
  }
  .uri code {
    flex: 1;
    min-width: 0;
    word-break: break-all;
    background: var(--surface-2);
    padding: var(--space-1) var(--space-2);
    border-radius: var(--radius);
  }
  .check {
    display: flex;
    gap: var(--space-2);
    align-items: center;
  }
  .acts {
    display: flex;
    gap: var(--space-2);
    margin-top: var(--space-4);
  }
</style>
