<script lang="ts">
  // SCR-10: the queue "to classify" and the pinned definitions with their detected changes, a view
  // on F-015's operations (`tool queue`, `tool classify`, `tool accept`, `tool diff`, `tool
  // validate`) that adds no rule (BR-211). Classes, sources, proposals, states and digests are the
  // server's; the upstream's text (tool names, definition fields) is rendered as text only.
  import { onMount } from 'svelte';
  import { message, query, type Client, type Service, type TreeNode } from '../lib/api';
  import { badges } from '../lib/badges.svelte';
  import { codeText, t } from '../lib/i18n';
  import { afterWrite } from '../lib/notice.svelte';
  import {
    changedConnections,
    CLASSES,
    fieldText,
    validationBody,
    type Change,
    type Classified,
    type Queued,
    type Validated,
  } from '../lib/policy';

  let { client }: { client: Client } = $props();

  let queue = $state.raw<Queued[]>([]);
  let catalogue = $state.raw<Queued[]>([]);
  let services = $state.raw<Service[]>([]);
  let nodes = $state.raw<TreeNode[]>([]);
  let service = $state('');
  let loaded = $state(false);
  let error = $state('');
  let done = $state('');
  let busy = $state(false);
  let queueRead = 0;
  type CatalogueCursor = {after_service: string; after_name: string};

  let path = $state('/');
  let changes = $state.raw<Change[] | null>(null);
  let diffPath = $state('');
  let diffError = $state('');
  let diffDone = $state('');
  let reading = $state(false);

  async function readQueue(): Promise<void> {
    const readId = ++queueRead;
    const selectedService = service;
    try {
      const read = await client.call<{ queue: Queued[] }>('GET', `tools/queue${query({ service: selectedService })}`);
      const classified: Queued[] = [];
      let cursor: CatalogueCursor | null = null;
      do {
        const page: {catalogue: Queued[]; next: CatalogueCursor | null} = await client.call('GET', `tools/catalogue${query({service: selectedService, limit: '500', ...(cursor ?? {})})}`);
        classified.push(...page.catalogue);
        cursor = page.next;
        if (readId !== queueRead) return;
      } while (cursor);
      queue = read.queue;
      catalogue = classified;
      if (!selectedService) badges.tools = read.queue.length;
      error = '';
    } catch (e) {
      if (readId === queueRead) error = message(e);
    } finally {
      if (readId === queueRead) loaded = true;
    }
  }

  onMount(() => {
    void readQueue();
    client
      .call<{ services: Service[] }>('GET', 'services')
      .then((r) => (services = r.services))
      .catch((e: unknown) => (error = message(e)));
    client
      .call<{ nodes: TreeNode[] }>('GET', 'nodes')
      .then((r) => (nodes = r.nodes))
      .catch((e: unknown) => (diffError = message(e)));
  });

  async function act(item: Queued, cls: string | null): Promise<void> {
    // controls stay enabled while a write runs, so that the focus stays put (BR-232)
    if (busy) return;
    busy = true;
    done = '';
    error = '';
    try {
      const answer = afterWrite(
        cls === null
          ? await client.call<Classified>('POST', 'tools/accept', { service: item.service, tool: item.name })
          : await client.call<Classified>('POST', 'tools/classify', {
              service: item.service,
              tool: item.name,
              class: cls,
            }),
      );
      done = t('tools.classified', {
        tool: answer.tool,
        service: answer.service,
        class: answer.class,
        source: answer.source,
      });
      await readQueue();
    } catch (e) {
      error = message(e);
    } finally {
      busy = false;
    }
  }

  async function diff(event?: SubmitEvent): Promise<void> {
    event?.preventDefault();
    if (reading) return;
    reading = true;
    diffError = '';
    diffDone = '';
    // the scope is the node the request leaves with: the select stays enabled while it runs, and
    // a node chosen meanwhile must not become the scope of the changes shown (review 579 B1)
    const asked = path;
    try {
      const read = await client.call<{ node: unknown; changes: Change[] }>('POST', 'tools/diff', {
        path: asked,
      });
      changes = read.changes;
      diffPath = asked;
    } catch (e) {
      diffError = message(e);
    } finally {
      reading = false;
    }
  }

  async function validate(connection: string, row: Change | null): Promise<void> {
    // controls stay enabled while a write runs, so that the focus stays put (BR-232)
    if (reading) return;
    reading = true;
    diffError = '';
    diffDone = '';
    try {
      const answer = afterWrite(
        await client.call<{ validated: Validated[] }>(
          'POST',
          'tools/validate',
          validationBody(diffPath, connection, row),
        ),
      );
      const n = String(answer.validated.length);
      const read = await client.call<{ changes: Change[] }>('POST', 'tools/diff', { path: diffPath });
      changes = read.changes;
      diffDone = t('tools.validated', { n });
    } catch (e) {
      diffError = message(e);
    } finally {
      reading = false;
    }
  }

  const groups = $derived(changes ? changedConnections(changes) : []);
</script>

<div class="screen" data-state={loaded && queue.length === 0 && !error ? 'SCR-10.empty' : 'SCR-10'}>
  <section class="panel" aria-labelledby="queue-title">
    <h2 id="queue-title">{t('tools.title')}</h2>
    <p class="hint intro">{t('tools.intro')}</p>

    <label class="filter">
      {t('tools.service')}
      <select bind:value={service} onchange={() => void readQueue()}>
        <option value="">{t('tools.all_services')}</option>
        {#each services as s (s.id)}<option value={s.id}>{s.id}</option>{/each}
      </select>
    </label>
    {#if done}<p class="ok" role="status">{done}</p>{/if}
    {#if error}<p class="notice" role="alert">{error}</p>{/if}
    {#if !loaded}
      <p class="hint">{t('loading')}</p>
    {:else if queue.length === 0 && !error}
      <p class="empty" data-state="SCR-10.empty">{t('tools.empty')}</p>
    {:else if queue.length > 0}
      <div class="scroll">
        <table>
          <thead>
            <tr>
              <th>{t('tools.col_service')}</th>
              <th>{t('tools.col_name')}</th>
              <th>{t('tools.col_class')}</th>
              <th>{t('tools.col_proposal')}</th>
              <th>{t('tools.col_acts')}</th>
            </tr>
          </thead>
          <tbody>
            {#each queue as item (`${item.service}\u0000${item.name}`)}
              <tr>
                <td class="mono">{item.service}</td>
                <td>
                  <span class="mono">{item.name}</span>
                  {#if item.removed}<span class="badge">{t('tools.removed')}</span>{/if}
                </td>
                <td>
                  {codeText('class', item.class)}
                  <span class="hint">({codeText('source', item.source)})</span>
                </td>
                <td>{item.proposal ? codeText('class', item.proposal) : t('tools.none')}</td>
                <td>
                  <div class="seg" role="group" aria-label={t('tools.classify_group', { name: item.name })}>
                    {#each CLASSES as cls (cls)}
                      <button
                        type="button"
                        class="ghost small"
                        aria-pressed={item.source === 'manual' && item.class === cls}
                        onclick={() => act(item, cls)}>{codeText('class', cls)}</button
                      >
                    {/each}
                    <button
                      type="button"
                      class="ghost small"
                      title={t('tools.clear_hint')}
                      disabled={item.source !== 'manual'}
                      onclick={() => act(item, 'clear')}>{t('tools.clear')}</button
                    >
                    <button
                      type="button"
                      class="primary small"
                      title={t('tools.accept_hint')}
                      onclick={() => act(item, null)}>{t('tools.accept')}</button
                    >
                  </div>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </section>

  <section class="panel" aria-labelledby="catalogue-title" aria-busy={busy}>
    <h2 id="catalogue-title">{t('tools.catalogue')}</h2>
    {#each catalogue as item (`${item.service}\u0000${item.name}`)}
      <div class="change">
        <p><span class="mono">{item.service} / {item.name}</span> — {codeText('class', item.class)} ({codeText('source', item.source)})
          {#if item.removed}<span class="badge">{t('tools.removed')}</span>{/if}</p>
        <div class="seg" role="group" aria-label={t('tools.classify_group', { name: item.name })}>
          {#each CLASSES as cls (cls)}
            <button type="button" class="ghost small" onclick={() => act(item, cls)}>{codeText('class', cls)}</button>
          {/each}
          <button type="button" class="ghost small" disabled={item.source !== 'manual'} onclick={() => act(item, 'clear')}>{t('tools.clear')}</button>
        </div>
      </div>
    {/each}
  </section>

  <section class="panel" aria-labelledby="pinned-title">
    <h2 id="pinned-title">{t('tools.pinned_title')}</h2>
    <p class="hint intro">{t('tools.pinned_intro')}</p>
    <p class="hint">{t('tools.no_baseline')}</p>
    <form class="pick" onsubmit={diff} aria-busy={reading ? 'true' : 'false'}>
      <label>
        {t('tools.node')}
        <select bind:value={path}>
          {#each nodes as n (n.id)}<option value={n.path}>{n.path}</option>{/each}
        </select>
      </label>
      <button type="submit" class="primary">{t('tools.read_changes')}</button>
    </form>
    {#if diffDone}<p class="ok" role="status">{diffDone}</p>{/if}
    {#if diffError}<p class="notice" role="alert">{diffError}</p>{/if}
    {#if changes && changes.length === 0}
      <p class="hint">{t('tools.no_changes', { path: diffPath })}</p>
    {:else if changes}
      <div class="diff" data-state="SCR-10.diff">
        {#each groups as connection (connection)}
          <section class="group" aria-label={connection}>
            <header>
              <span class="mono">{connection}</span>
              <button
                type="button"
                class="primary small"
                onclick={() => validate(connection, null)}
                >{t('tools.validate_connection', { name: connection })}</button
              >
            </header>
            {#each changes.filter((c) => c.connection === connection) as change (`${change.role}\u0000${change.tool}`)}
              <article class="change">
                <p class="head">
                  <span class="mono">{change.tool}</span>
                  <span class="badge st-{change.state}">{codeText('state', change.state)}</span>
                  <span class="hint mono">{change.role}</span>
                  <span class="hint mono"
                    >{t('tools.digest', {
                      before: (change.baseline_sha256 ?? '—').slice(0, 12),
                      after: (change.current_sha256 ?? '—').slice(0, 12),
                    })}</span
                  >
                  <button
                    type="button"
                    class="ghost small"
                    aria-label={`${t('tools.validate_tool')} ${change.tool} (${change.role})`}
                    onclick={() => validate(connection, change)}>{t('tools.validate_tool')}</button
                  >
                </p>
                {#if change.fields.length === 0}
                  <p class="hint">{t('tools.no_fields')}</p>
                {:else}
                  <table class="fields">
                    <thead>
                      <tr><th>{t('tools.field')}</th><th>{t('tools.before')}</th><th>{t('tools.after')}</th></tr>
                    </thead>
                    <tbody>
                      {#each change.fields as f (f.field)}
                        <tr>
                          <td class="mono">{f.field}
                            {#each f.type_marks ?? [] as mark (mark.path)}
                              <p>{t('tools.type_mark', { path: mark.path || '/', old: mark.old, new: mark.new })}</p>
                            {/each}
                          </td>
                          <td><pre class="old">{fieldText(f.old)}</pre></td>
                          <td><pre class="new">{fieldText(f.new)}</pre></td>
                        </tr>
                      {/each}
                    </tbody>
                  </table>
                {/if}
              </article>
            {/each}
          </section>
        {/each}
      </div>
    {/if}
  </section>
</div>

<style>
  .screen {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    max-width: 72rem;
  }
  .panel {
    background: var(--surface-1);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    padding: var(--space-3) var(--space-4) var(--space-4);
  }
  h2 {
    margin: 0 0 var(--space-1);
    font-size: 1.05rem;
    color: var(--title);
  }
  .intro {
    max-width: 52rem;
  }
  .filter,
  .pick label {
    display: inline-flex;
    gap: var(--space-2);
    align-items: center;
    margin: var(--space-2) 0;
  }
  .pick {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-3);
    align-items: center;
  }
  .empty {
    padding: var(--space-4);
    text-align: center;
    color: var(--muted);
    border: 1px dashed var(--rule);
    border-radius: var(--radius);
  }
  .scroll {
    overflow-x: auto;
  }
  table {
    width: 100%;
    border-collapse: collapse;
  }
  th,
  td {
    text-align: left;
    vertical-align: top;
    padding: var(--space-1) var(--space-2);
    border-bottom: 1px solid var(--rule);
  }
  th {
    color: var(--muted);
    font-weight: 500;
  }
  .seg {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1);
  }
  .seg [aria-pressed='true'] {
    background: var(--hover);
    border-color: var(--cyan);
    color: var(--text);
  }
  .group {
    margin-top: var(--space-3);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
  }
  .group header {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    justify-content: space-between;
    padding: var(--space-2) var(--space-3);
    border-bottom: 1px solid var(--rule);
    background: var(--surface-2);
  }
  .change {
    padding: var(--space-2) var(--space-3);
    border-bottom: 1px solid var(--rule);
  }
  .change:last-child {
    border-bottom: none;
  }
  .head {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
    align-items: baseline;
    margin: 0 0 var(--space-2);
  }
  .head button {
    margin-left: auto;
  }
  .st-suspended,
  .st-removed {
    color: var(--warn);
    border-color: var(--warn-bd);
  }
  .st-new {
    color: var(--ok);
    border-color: var(--ok);
  }
  pre {
    margin: 0;
    white-space: pre-wrap;
    word-break: break-word;
    font-family: var(--font-mono);
    font-size: 0.8rem;
    padding: var(--space-1) var(--space-2);
    border-radius: var(--radius);
    max-height: 16rem;
    overflow: auto;
  }
  .old {
    background: var(--warn-bg);
  }
  .new {
    background: var(--info-bg);
  }
  .fields td:first-child {
    width: 10rem;
  }
</style>
