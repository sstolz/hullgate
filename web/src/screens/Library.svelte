<script lang="ts" module>
  export type LibraryPick =
    | { kind: 'connection'; name: string }
    | { kind: 'service'; id: string }
    | { kind: 'application'; id: string };
</script>

<script lang="ts">
  // SCR-04's left column: services with their connections, applications. A connection is dragged
  // onto a role, or placed with "Place in…" (BR-232). States are the server's labels.
  import type { Application, Connection, Service } from '../lib/api';
  import { t } from '../lib/i18n';
  import { CONNECTION_DRAG } from '../lib/tree';
  import { focusInView } from '../lib/dom';

  let root: HTMLElement | undefined = $state();
  let connectButton: HTMLButtonElement | undefined = $state();
  export function focusPlacement(): void {
    focusInView(root?.querySelector<HTMLButtonElement>('[data-place-in]') ?? connectButton);
  }

  let {
    services,
    applications,
    connections,
    picked,
    onPick,
    onHover,
    onConnect,
    onDeclare,
    onPlaceIn,
  }: {
    services: readonly Service[];
    applications: readonly Application[];
    connections: readonly Connection[];
    picked: LibraryPick | null;
    onPick: (pick: LibraryPick) => void;
    onHover: (name: string | null) => void;
    onConnect: () => void;
    onDeclare: (kind: 'service' | 'application') => void;
    onPlaceIn: (connection: Connection) => void;
  } = $props();

  const byService = $derived(
    services.map((s) => ({ service: s, list: connections.filter((c) => c.service === s.id) })),
  );
  const orphans = $derived(connections.filter((c) => !services.some((s) => s.id === c.service)));

  function isPicked(kind: LibraryPick['kind'], key: string): boolean {
    if (!picked || picked.kind !== kind) return false;
    return picked.kind === 'connection' ? picked.name === key : picked.id === key;
  }

  function dragStart(event: DragEvent, c: Connection): void {
    event.dataTransfer?.setData(CONNECTION_DRAG, c.name);
    event.dataTransfer?.setData('text/plain', c.name);
    if (event.dataTransfer) event.dataTransfer.effectAllowed = 'copy';
  }

  /** The visual state a connection's server state names (§3.7). */
  function visual(c: Connection): string {
    if (c.stored_state === 'pending') return 'SCR-07.pending';
    if (c.stored_state === 'reauth_required') return 'SCR-07.reauth-required';
    return 'SCR-07';
  }
</script>

{#snippet item(c: Connection)}
  <li
    class="conn"
    class:picked={isPicked('connection', c.name)}
    draggable="true"
    data-state={visual(c)}
    ondragstart={(e) => dragStart(e, c)}
    onmouseenter={() => onHover(c.name)}
    onmouseleave={() => onHover(null)}
  >
    <button
      type="button"
      class="pick"
      onclick={() => onPick({ kind: 'connection', name: c.name })}
      onfocus={() => onHover(c.name)}
      onblur={() => onHover(null)}
      aria-label={t('lib.inspect', { name: c.name })}
    >
      <span class="mono name">{c.name}</span>
      <span class="state st-{c.stored_state}">{c.state}</span>
      <span class="hint">{t('lib.placements', { n: String(c.placements.length) })}</span>
    </button>
    <button type="button" class="ghost small" data-place-in onclick={() => onPlaceIn(c)}>{t('lib.place_in')}</button>
  </li>
{/snippet}

<section class="lib" aria-labelledby="lib-title" bind:this={root}>
  <header>
    <h2 id="lib-title">{t('lib.title')}</h2>
    <button type="button" class="primary small" bind:this={connectButton} onclick={onConnect}>{t('lib.connect')}</button>
  </header>
  <p class="hint">{t('lib.hint')}</p>

  {#if services.length === 0 && connections.length === 0}
    <p class="empty" data-state="SCR-07.empty">{t('lib.empty')}</p>
  {/if}

  {#each byService as group (group.service.id)}
    <div class="group">
      <button
        type="button"
        class="svc"
        class:picked={isPicked('service', group.service.id)}
        onclick={() => onPick({ kind: 'service', id: group.service.id })}
      >
        <span class="mono">{group.service.id}</span>
        <span class="hint">{group.service.model}</span>
      </button>
      {#if group.list.length > 0}
        <ul>
          {#each group.list as c (c.name)}{@render item(c)}{/each}
        </ul>
      {/if}
    </div>
  {/each}
  {#if orphans.length > 0}
    <ul>
      {#each orphans as c (c.name)}{@render item(c)}{/each}
    </ul>
  {/if}

  <h3>{t('lib.applications')}</h3>
  {#if applications.length === 0}
    <p class="hint">{t('lib.no_applications')}</p>
  {:else}
    <ul>
      {#each applications as a (a.id)}
        <li>
          <button
            type="button"
            class="svc"
            class:picked={isPicked('application', a.id)}
            onclick={() => onPick({ kind: 'application', id: a.id })}
          >
            <span class="mono">{a.id}</span>
            <span class="hint">{a.service} · {a.mode}</span>
            {#if a.web_return}<span class="badge">{t('app.web_return_on')}</span>{/if}
          </button>
        </li>
      {/each}
    </ul>
  {/if}
  <div class="declare">
    <button type="button" class="ghost small" onclick={() => onDeclare('service')}>{t('lib.declare_service')}</button>
    <button type="button" class="ghost small" onclick={() => onDeclare('application')}>
      {t('lib.declare_application')}
    </button>
  </div>
</section>

<style>
  header {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }
  h2 {
    flex: 1;
    margin: 0;
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
  }
  h3 {
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
    margin: var(--space-4) 0 var(--space-1);
  }
  ul {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .group {
    margin-bottom: var(--space-2);
  }
  .svc,
  .pick {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1) var(--space-2);
    align-items: baseline;
    width: 100%;
    background: none;
    border: 1px solid transparent;
    border-radius: var(--radius);
    padding: var(--space-1) var(--space-2);
    text-align: left;
  }
  .svc:hover,
  .pick:hover {
    background: var(--hover);
  }
  .svc.picked,
  .conn.picked .pick {
    border-color: var(--primary);
  }
  .conn {
    display: flex;
    align-items: center;
    gap: var(--space-1);
    padding-left: var(--space-3);
    cursor: grab;
  }
  .conn .pick {
    flex: 1;
    min-width: 0;
  }
  .name {
    color: var(--text);
  }
  .state {
    font-size: 0.75rem;
    color: var(--muted);
  }
  .st-active {
    color: var(--ok);
  }
  .st-pending {
    color: var(--warn);
  }
  .st-reauth_required {
    color: var(--danger);
  }
  .small {
    font-size: 0.8rem;
    padding: 2px var(--space-2);
  }
  .empty {
    color: var(--muted);
  }
  .declare {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
    margin-top: var(--space-3);
  }
</style>
