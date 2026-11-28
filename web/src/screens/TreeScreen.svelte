<script lang="ts">
  // SCR-04: Library | Tree | Inspector, three columns collapsed with `[` and `]` (#18), the List
  // and Map views, and the acts of §3.5: add, rename, move, delete, sensitivity, lock, placement.
  // Every irreversible act goes through BR-214's impact dialog; a placement refused for a share
  // opens SCR-07.share (BR-078 v3). The console decides nothing (BR-211).
  import { onMount, tick } from 'svelte';
  import {
    ApiError,
    message,
    type Application,
    type Client,
    type Connection,
    type Opened,
    type Service,
    type TreeNode,
  } from '../lib/api';
  import { focusInView, typing } from '../lib/dom';
  import type { SetupLanding } from '../lib/routes';
  import { levelName, t } from '../lib/i18n';
  import { afterWrite } from '../lib/notice.svelte';
  import { CHILD_LEVEL, rolesBelow } from '../lib/tree';
  import ConnectDialog from './ConnectDialog.svelte';
  import ConnectionSheet, { type ConnectionActs } from './ConnectionSheet.svelte';
  import DeclareDialog from './DeclareDialog.svelte';
  import Dialog from './Dialog.svelte';
  import DiagnoseDialog from './DiagnoseDialog.svelte';
  import ImpactDialog from './ImpactDialog.svelte';
  import Inspector, { type NodeActs } from './Inspector.svelte';
  import ItemSheet from './ItemSheet.svelte';
  import Library, { type LibraryPick } from './Library.svelte';
  import PlaceDialog from './PlaceDialog.svelte';
  import ShareDialog from './ShareDialog.svelte';
  import TreeMap from './TreeMap.svelte';
  import TreeView, { type TreeEdits } from './TreeView.svelte';

  let { client, landing = null }: { client: Client; landing?: SetupLanding | null } = $props();
  let treeView: { addClient: () => void } | undefined = $state();
  let library: { focusPlacement: () => void } | undefined = $state();
  let firstClientField: HTMLInputElement | undefined = $state();

  type Impact = {
    kind: 'impact';
    act: string;
    params: Record<string, string>;
    title: string;
    confirm: string;
    intro?: string;
    write: (digest: string) => Promise<unknown>;
    after?: () => void;
  };
  type Open =
    | Impact
    | { kind: 'place'; connection: Connection; roles: TreeNode[]; target: TreeNode | null }
    | {
        kind: 'share';
        connection: string;
        role: string;
        warning: string;
        reasons: string[];
        rest: string[];
      }
    | { kind: 'connect'; preset: string; opened: Opened | null }
    | { kind: 'declare'; which: 'service' | 'upstream' | 'application'; service: Service | null }
    | { kind: 'diagnose'; connection: string; role: string }
    | { kind: 'rename'; node: TreeNode }
    | { kind: 'move'; node: TreeNode };

  let nodes = $state.raw<TreeNode[]>([]);
  let connections = $state.raw<Connection[]>([]);
  let services = $state.raw<Service[]>([]);
  let applications = $state.raw<Application[]>([]);
  let webRedirectUri = $state('');
  let loaded = $state(false);
  let error = $state('');
  let done = $state('');

  let selectedId = $state<string | null>(null);
  let picked = $state<LibraryPick | null>(null);
  let hover = $state<string | null>(null);
  let view = $state<'list' | 'map'>('list');
  let libOpen = $state(true);
  let inspOpen = $state(true);
  let narrow = $state(false);
  let dialog = $state<Open | null>(null);

  // rename and move dialogs
  let draftName = $state('');
  let draftSegment = $state('');
  let draftParent = $state('');
  let firstClient = $state('');

  const selected = $derived(nodes.find((n) => n.id === selectedId) ?? null);
  const empty = $derived(loaded && !nodes.some((n) => n.level === 'client'));
  const clients = $derived(nodes.filter((n) => n.level === 'client'));
  const lit = $derived(hover ?? (picked?.kind === 'connection' ? picked.name : null));
  const litConnection = $derived(connections.find((c) => c.name === lit) ?? null);
  const highlight = $derived(new Set(litConnection?.placements.map((p) => p.role) ?? []));
  const pickedName = $derived(picked?.kind === 'connection' ? picked.name : null);
  const pickedId = $derived(picked && picked.kind !== 'connection' ? picked.id : null);
  const pickedConnection = $derived(connections.find((c) => c.name === pickedName) ?? null);
  const pickedService = $derived(
    picked?.kind === 'service' ? (services.find((s) => s.id === pickedId) ?? null) : null,
  );
  const pickedApplication = $derived(
    picked?.kind === 'application' ? (applications.find((a) => a.id === pickedId) ?? null) : null,
  );

  async function load(): Promise<void> {
    try {
      const [n, c, s, a] = await Promise.all([
        client.call<{ nodes: TreeNode[] }>('GET', 'nodes'),
        client.call<{ connections: Connection[] }>('GET', 'connections'),
        client.call<{ services: Service[] }>('GET', 'services'),
        client.call<{ applications: Application[]; web_redirect_uri?: string }>('GET', 'applications'),
      ]);
      nodes = n.nodes;
      connections = c.connections;
      services = s.services;
      applications = a.applications;
      webRedirectUri = a.web_redirect_uri ?? '';
    } catch (e) {
      error = message(e);
    } finally {
      loaded = true;
    }
  }

  /** A direct write (no impact): the server's answer, its notice, then a fresh read. */
  async function write(work: () => Promise<unknown>, ok = ''): Promise<boolean> {
    error = '';
    done = '';
    try {
      afterWrite(await work());
      done = ok;
      await load();
      return true;
    } catch (e) {
      error = message(e);
      return false;
    }
  }

  function impact(open: Omit<Impact, 'kind'>): void {
    dialog = { kind: 'impact', ...open };
  }

  // --- tree acts ----------------------------------------------------------------------------

  function addChild(parent: TreeNode, name: string): void {
    const level = CHILD_LEVEL[parent.level];
    if (!level) return;
    void write(
      () => client.call('POST', 'nodes', { parent: parent.path, level, name }),
      t('tree.added', { type: levelName(level), name }),
    );
  }

  function rename(node: TreeNode, name: string, segment = ''): void {
    const params: Record<string, string> = { path: node.path };
    if (segment) params.segment = segment;
    impact({
      act: 'tree.rename',
      params,
      title: t('impact.rename_title', { name: node.name }),
      confirm: t('node.rename'),
      intro: t('impact.rename_intro', { path: node.path, name }),
      write: (digest) => {
        const body: Record<string, unknown> = { path: node.path, name, impact: digest };
        if (segment) body.segment = segment;
        return client.call('POST', 'nodes/rename', body);
      },
    });
  }

  function move(node: TreeNode, parent: TreeNode): void {
    impact({
      act: 'tree.move',
      params: { path: node.path, parent: parent.path },
      title: t('impact.move_title', { name: node.name }),
      confirm: t('node.move'),
      intro: t('impact.move_intro', { path: node.path, parent: parent.path }),
      write: (digest) =>
        client.call('POST', 'nodes/move', { path: node.path, parent: parent.path, impact: digest }),
    });
  }

  function remove(node: TreeNode): void {
    impact({
      act: 'tree.delete',
      params: { path: node.path },
      title: t('impact.delete_title', { type: levelName(node.level), name: node.name }),
      confirm: t('node.delete'),
      intro: t('impact.delete_intro', { path: node.path }),
      write: (digest) => client.call('POST', 'nodes/delete', { path: node.path, impact: digest }),
      after: () => {
        if (selectedId === node.id) selectedId = null;
      },
    });
  }

  function sensitive(node: TreeNode, on: boolean): void {
    impact({
      act: 'tree.sensitive',
      params: { path: node.path, sensitive: String(on) },
      title: on ? t('impact.sensitive_on', { name: node.name }) : t('impact.sensitive_off', { name: node.name }),
      confirm: t('apply'),
      write: (digest) =>
        client.call('POST', 'nodes/sensitive', { path: node.path, sensitive: on, impact: digest }),
    });
  }

  function lock(node: TreeNode, on: boolean): void {
    void write(
      () => client.call('POST', on ? 'nodes/lock' : 'nodes/unlock', { path: node.path }),
      on ? t('node.locked_done', { path: node.path }) : t('node.unlocked_done', { path: node.path }),
    );
  }

  // --- placements ---------------------------------------------------------------------------

  /** Places a connection on roles one by one; a share refusal asks before going on. */
  async function placeAll(connection: string, roles: string[], confirmShare = false): Promise<void> {
    const [role, ...rest] = roles;
    if (role === undefined) {
      await load();
      return;
    }
    error = '';
    done = '';
    try {
      const body: Record<string, unknown> = { connection, role };
      if (confirmShare) body.confirm_share = true;
      afterWrite(await client.call('POST', 'placements', body));
      done = t('place.done', { name: connection, path: role });
      await placeAll(connection, rest);
    } catch (e) {
      if (e instanceof ApiError && e.code === 'share_confirmation_required') {
        const reasons = Array.isArray(e.body.reasons) ? (e.body.reasons as unknown[]).map(String) : [];
        dialog = { kind: 'share', connection, role, warning: e.message, reasons, rest };
        await load();
        return;
      }
      error = message(e);
      await load();
    }
  }

  function placeIn(c: Connection, target: TreeNode | null = null): void {
    const roles = target ? rolesBelow(target, nodes) : nodes.filter((n) => n.level === 'role');
    dialog = { kind: 'place', connection: c, roles, target };
  }

  function dropped(node: TreeNode, name: string): void {
    const c = connections.find((x) => x.name === name);
    if (!c) return;
    if (node.level === 'role') void placeAll(name, [node.path]);
    else placeIn(c, node);
  }

  function opened(answer: Opened): void {
    if (answer.redirect === 'web' && answer.authorization_url) location.assign(answer.authorization_url);
    else if (answer.redirect === 'paste' && answer.authorization_url)
      dialog = { kind: 'connect', preset: '', opened: answer };
    else void load();
  }

  const edits: TreeEdits = {
    add: addChild,
    rename: (node, name) => rename(node, name),
    remove,
    move,
    drop: dropped,
  };

  const nodeActs: NodeActs = {
    rename: (node) => {
      draftName = node.name;
      draftSegment = '';
      dialog = { kind: 'rename', node };
    },
    move: (node) => {
      draftParent = '';
      dialog = { kind: 'move', node };
    },
    remove,
    sensitive,
    lock,
    unplace: (connection, role) =>
      void write(
        () => client.call('POST', 'placements/remove', { connection, role }),
        t('role.unplaced', { name: connection, path: role }),
      ),
    diagnose: (connection, role) => (dialog = { kind: 'diagnose', connection, role }),
    select: (node) => selectNode(node),
  };

  const connectionActs: ConnectionActs = {
    revoke: (c) =>
      impact({
        act: 'connection.revoke',
        params: { name: c.name },
        title: t('impact.revoke_title', { name: c.name }),
        confirm: t('conn.revoke'),
        intro: t('conn.revoke_body'),
        write: (digest) => client.call('POST', 'connections/revoke', { name: c.name, impact: digest }),
      }),
    remove: (c) =>
      impact({
        act: 'connection.remove',
        params: { name: c.name },
        title: t('impact.remove_title', { name: c.name }),
        confirm: t('conn.remove'),
        intro: t('conn.remove_body'),
        write: (digest) => client.call('POST', 'connections/remove', { name: c.name, impact: digest }),
        after: () => (picked = null),
      }),
    placeIn: (c) => placeIn(c),
    diagnose: (connection, role) => (dialog = { kind: 'diagnose', connection, role }),
    opened,
    changed: () => void load(),
  };

  function removeItem(kind: 'service' | 'application', id: string): void {
    impact({
      act: `${kind}.remove`,
      params: { id },
      title: t(kind === 'service' ? 'impact.service_remove' : 'impact.application_remove', { id }),
      confirm: t('remove'),
      write: (digest) =>
        client.call('POST', kind === 'service' ? 'services/remove' : 'applications/remove', {
          id,
          impact: digest,
        }),
      after: () => (picked = null),
    });
  }

  function selectNode(node: TreeNode): void {
    selectedId = node.id;
    picked = null;
    if (narrow) inspOpen = true;
  }

  function pick(p: LibraryPick): void {
    picked = p;
    selectedId = null;
    if (narrow) inspOpen = true;
  }

  function addFirstClient(event: SubmitEvent): void {
    event.preventDefault();
    const org = nodes.find((n) => n.level === 'org');
    if (org && firstClient.trim()) addChild(org, firstClient.trim());
    firstClient = '';
  }

  const parentLevel = (node: TreeNode): string | undefined =>
    (Object.entries(CHILD_LEVEL).find(([, child]) => child === node.level) ?? [])[0];

  function keys(event: KeyboardEvent): void {
    if (dialog || typing(event) || event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.key === '[') {
      event.preventDefault();
      libOpen = !libOpen;
    } else if (event.key === ']') {
      event.preventDefault();
      inspOpen = !inspOpen;
    }
  }

  $effect(() => {
    const request = landing;
    if (!loaded || !request) return;
    let current = true;
    // Wait for data and rendering, including a reopened Library or the List view.
    void tick().then(async () => {
      if (!current) return;
      dialog = null;
      libOpen = true;
      view = 'list';
      await tick();
      if (!current) return;
      switch (request.target) {
        case 'client':
          if (empty) focusInView(firstClientField);
          else treeView?.addClient();
          break;
        case 'service':
          dialog = { kind: 'declare', which: 'service', service: null };
          break;
        case 'connection':
          dialog = { kind: 'connect', preset: '', opened: null };
          break;
        case 'placement':
          library?.focusPlacement();
          break;
      }
    });
    return () => { current = false; };
  });

  onMount(() => {
    void load();
    const media = window.matchMedia('(max-width: 960px)');
    const fit = (): void => {
      narrow = media.matches;
      libOpen = inspOpen = !narrow;
    };
    fit();
    media.addEventListener('change', fit);
    return () => media.removeEventListener('change', fit);
  });
</script>

<svelte:window onkeydown={keys} />

<div
  class="screen"
  class:lib-shut={!libOpen}
  class:insp-shut={!inspOpen}
  class:narrow
  data-state={narrow ? 'SCR-04.narrow' : empty ? 'SCR-04.empty' : view === 'map' ? 'SCR-04.map' : 'SCR-04'}
>
  {#if libOpen}
    <aside class="col lib" aria-label={t('lib.title')}>
      <button
        type="button"
        class="ghost rail-toggle"
        aria-label={t('shortcut', { label: t('lib.collapse'), key: '[' })}
        title={t('shortcut', { label: t('lib.collapse'), key: '[' })}
        onclick={() => (libOpen = false)}>«</button
      >
      <Library
        bind:this={library}
        {services}
        {applications}
        {connections}
        {picked}
        onPick={pick}
        onHover={(name) => (hover = name)}
        onConnect={() => (dialog = { kind: 'connect', preset: '', opened: null })}
        onDeclare={(which) => (dialog = { kind: 'declare', which, service: null })}
        onPlaceIn={(c) => placeIn(c)}
      />
    </aside>
  {:else}
    <button
      type="button"
      class="rail"
      aria-label={t('shortcut', { label: t('lib.expand'), key: '[' })}
      title={t('shortcut', { label: t('lib.expand'), key: '[' })}
      onclick={() => (libOpen = true)}><span>{t('lib.title')}</span></button
    >
  {/if}

  <section class="col center" aria-labelledby="tree-title">
    <header class="colhead">
      <h2 id="tree-title">{t('tree.title')}</h2>
      <div class="seg" role="group" aria-label={t('tree.view')}>
        <button type="button" aria-pressed={view === 'list'} class:on={view === 'list'} onclick={() => (view = 'list')}>
          {t('tree.view_list')}
        </button>
        <button type="button" aria-pressed={view === 'map'} class:on={view === 'map'} onclick={() => (view = 'map')}>
          {t('tree.view_map')}
        </button>
      </div>
    </header>
    <p class="hint">{view === 'list' ? t('tree.hint_list') : t('tree.hint_map')}</p>
    {#if done}<p class="ok" role="status">{done}</p>{/if}
    {#if error}<p class="notice" role="alert">{error}</p>{/if}

    {#if empty}
      <div class="empty">
        <h3>{t('tree.empty')}</h3>
        <p>{t('tree.empty_detail')}</p>
        <form class="inline" onsubmit={addFirstClient}>
          <label for="first-client">{t('tree.first_client')}</label>
          <input id="first-client" bind:this={firstClientField} bind:value={firstClient} required />
          <button type="submit" class="primary">{t('tree.add_client')}</button>
        </form>
      </div>
    {/if}

    {#if view === 'list'}
      <TreeView
        bind:this={treeView}
        {nodes}
        selected={selectedId}
        {highlight}
        onSelect={selectNode}
        {edits}
      />
    {:else}
      <TreeMap
        {nodes}
        selected={selectedId}
        {highlight}
        legend={litConnection
          ? t('map.legend', { name: litConnection.name, n: String(litConnection.placements.length) })
          : ''}
        onSelect={selectNode}
        onDrop={dropped}
      />
    {/if}
  </section>

  <!-- kept mounted while collapsed (hidden), so that the inspector's open editor and preview
       survive a collapse, or a viewport change crossing the narrow breakpoint -->
  <aside class="col insp" aria-label={t('insp.title')} hidden={!inspOpen}>
    <button
      type="button"
      class="ghost rail-toggle"
      aria-label={t('shortcut', { label: t('insp.collapse'), key: ']' })}
      title={t('shortcut', { label: t('insp.collapse'), key: ']' })}
      onclick={() => (inspOpen = false)}>»</button
    >
    {#if selected}
      <Inspector {client} node={selected} {nodes} {connections} acts={nodeActs} />
    {:else if pickedConnection}
      <ConnectionSheet {client} connection={pickedConnection} acts={connectionActs} />
    {:else if pickedService || pickedApplication}
      <ItemSheet
        {client}
        service={pickedService}
        application={pickedApplication}
        {webRedirectUri}
        onUpstream={(s) => (dialog = { kind: 'declare', which: 'upstream', service: s })}
        onRemove={removeItem}
        onChanged={() => void load()}
      />
    {:else}
      <p class="hint">{t('insp.empty')}</p>
    {/if}
  </aside>
  {#if !inspOpen}
    <button
      type="button"
      class="rail"
      aria-label={t('shortcut', { label: t('insp.expand'), key: ']' })}
      title={t('shortcut', { label: t('insp.expand'), key: ']' })}
      onclick={() => (inspOpen = true)}><span>{t('insp.title')}</span></button
    >
  {/if}
</div>

{#if dialog?.kind === 'impact'}
  {@const d = dialog}
  <ImpactDialog
    {client}
    act={d.act}
    params={d.params}
    title={d.title}
    confirm={d.confirm}
    intro={d.intro}
    write={d.write}
    onDone={(answer) => {
      // `d` follows `dialog`: read it before closing the dialog
      const after = d.after;
      afterWrite(answer);
      dialog = null;
      after?.();
      done = t('done');
      void load();
    }}
    onClose={() => (dialog = null)}
  />
{:else if dialog?.kind === 'place'}
  {@const d = dialog}
  <PlaceDialog
    connection={d.connection.name}
    roles={d.roles}
    target={d.target}
    placed={new Set(d.connection.placements.map((p) => p.role))}
    onPlace={(paths) => {
      const name = d.connection.name;
      dialog = null;
      void placeAll(name, paths);
    }}
    onClose={() => (dialog = null)}
  />
{:else if dialog?.kind === 'share'}
  {@const d = dialog}
  <ShareDialog
    connection={d.connection}
    role={d.role}
    warning={d.warning}
    reasons={d.reasons}
    onConfirm={async () => {
      const { connection, role, rest } = d;
      dialog = null;
      await placeAll(connection, [role], true);
      if (!dialog) await placeAll(connection, rest);
    }}
    onDedicated={() => {
      const service = connections.find((c) => c.name === d.connection)?.service ?? '';
      dialog = { kind: 'connect', preset: service, opened: null };
    }}
    onClose={() => (dialog = null)}
  />
{:else if dialog?.kind === 'connect'}
  <ConnectDialog
    {client}
    {services}
    {applications}
    {clients}
    preset={dialog.preset}
    opened={dialog.opened}
    onDone={() => {
      dialog = null;
      void load();
    }}
    onClose={() => {
      dialog = null;
      void load();
    }}
  />
{:else if dialog?.kind === 'declare'}
  <DeclareDialog
    {client}
    kind={dialog.which}
    {services}
    service={dialog.service}
    onDone={() => {
      dialog = null;
      done = t('done');
      void load();
    }}
    onClose={() => (dialog = null)}
  />
{:else if dialog?.kind === 'diagnose'}
  <DiagnoseDialog
    {client}
    connection={dialog.connection}
    role={dialog.role}
    onClose={() => (dialog = null)}
  />
{:else if dialog?.kind === 'rename'}
  {@const node = dialog.node}
  <Dialog title={t('impact.rename_title', { name: node.name })} onClose={() => (dialog = null)}>
    <form
      class="stack"
      onsubmit={(e) => {
        e.preventDefault();
        rename(node, draftName.trim(), draftSegment.trim());
      }}
    >
      <label>{t('node.name')} <input bind:value={draftName} required data-autofocus /></label>
      <label>{t('node.segment_optional')} <input bind:value={draftSegment} class="mono" /></label>
      <p class="hint">{t('node.segment_hint', { segment: node.segment })}</p>
      <div class="actions">
        <button type="button" class="ghost" onclick={() => (dialog = null)}>{t('cancel')}</button>
        <button type="submit" class="primary">{t('continue')}</button>
      </div>
    </form>
  </Dialog>
{:else if dialog?.kind === 'move'}
  {@const node = dialog.node}
  {@const level = parentLevel(node)}
  <Dialog title={t('impact.move_title', { name: node.name })} onClose={() => (dialog = null)}>
    <form
      class="stack"
      onsubmit={(e) => {
        e.preventDefault();
        const parent = nodes.find((n) => n.path === draftParent);
        if (parent) move(node, parent);
      }}
    >
      <label>
        {t('node.new_parent')}
        <select bind:value={draftParent} required data-autofocus>
          <option value="" disabled>{t('choose')}</option>
          {#each nodes.filter((n) => n.level === level && n.path !== node.path) as n (n.id)}
            <option value={n.path}>{n.path}</option>
          {/each}
        </select>
      </label>
      <div class="actions">
        <button type="button" class="ghost" onclick={() => (dialog = null)}>{t('cancel')}</button>
        <button type="submit" class="primary" disabled={!draftParent}>{t('continue')}</button>
      </div>
    </form>
  </Dialog>
{/if}

<style>
  .screen {
    display: grid;
    grid-template-columns: minmax(15rem, 20rem) minmax(0, 1fr) minmax(17rem, 24rem);
    gap: var(--space-3);
    align-items: start;
  }
  .screen.lib-shut {
    grid-template-columns: 2rem minmax(0, 1fr) minmax(17rem, 24rem);
  }
  .screen.insp-shut {
    grid-template-columns: minmax(15rem, 20rem) minmax(0, 1fr) 2rem;
  }
  .screen.lib-shut.insp-shut {
    grid-template-columns: 2rem minmax(0, 1fr) 2rem;
  }
  .screen.narrow:not(.lib-shut),
  .screen.narrow:not(.insp-shut) {
    grid-template-columns: 2rem minmax(0, 1fr) 2rem;
  }
  .screen.narrow .col.lib,
  .screen.narrow .col.insp {
    position: fixed;
    top: 3.5rem;
    bottom: var(--space-3);
    width: min(22rem, calc(100vw - 2 * var(--space-4)));
    overflow: auto;
    z-index: 20;
    box-shadow: 0 8px 24px rgb(0 0 0 / 35%);
  }
  .screen.narrow .col.lib {
    left: var(--space-3);
  }
  .screen.narrow .col.insp {
    right: var(--space-3);
  }
  .screen > :nth-child(1) {
    grid-column: 1;
  }
  .center {
    grid-column: 2;
  }
  .screen > :nth-child(3) {
    grid-column: 3;
  }
  .col {
    position: relative;
    background: var(--surface-1);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    padding: var(--space-3);
    min-width: 0;
  }
  .col.insp[hidden] {
    display: none;
  }
  .rail-toggle {
    position: absolute;
    top: var(--space-2);
    right: var(--space-2);
    border: none;
    padding: 0 var(--space-1);
    color: var(--muted);
  }
  .lib :global(header) {
    padding-right: 1.5rem;
  }
  .rail {
    align-self: stretch;
    min-height: 12rem;
    background: var(--surface-1);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    color: var(--muted);
    padding: var(--space-2) 0;
    writing-mode: vertical-rl;
    font-size: 0.75rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  .colhead {
    display: flex;
    align-items: center;
    gap: var(--space-3);
  }
  .colhead h2 {
    flex: 1;
    margin: 0;
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
  }
  .seg {
    display: inline-flex;
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    overflow: hidden;
  }
  .seg button {
    background: none;
    border: none;
    padding: 2px var(--space-3);
    color: var(--muted);
  }
  .seg button.on {
    background: var(--hover);
    color: var(--text);
  }
  .empty {
    border: 1px dashed var(--rule);
    border-radius: var(--radius);
    padding: var(--space-4);
    margin-bottom: var(--space-3);
    text-align: center;
    color: var(--muted);
  }
  .empty h3 {
    margin-top: 0;
    color: var(--title);
  }
  .inline {
    display: flex;
    gap: var(--space-2);
    align-items: center;
    justify-content: center;
    flex-wrap: wrap;
  }
  .insp > :global(.hint) {
    margin-top: var(--space-5);
  }
</style>
