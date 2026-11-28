<script lang="ts" module>
  import type { TreeNode } from '../lib/api';

  const NODE_DRAG = 'application/x-hullgate-node';

  /** In-place edits; absent or `readOnly`, the view offers none (§3.17). */
  export interface TreeEdits {
    add: (parent: TreeNode, name: string) => void;
    rename: (node: TreeNode, name: string) => void;
    remove: (node: TreeNode) => void;
    move: (node: TreeNode, parent: TreeNode) => void;
    drop: (node: TreeNode, connection: string) => void;
  }
</script>

<script lang="ts">
  // SCR-04's List view, and §3.17's hook for F-017: a read-only mode (no drag, rename, delete or
  // impact dialog), a selection event, and an optional per-node decoration {level, length, text}
  // computed by the server, drawn as a colour class by level and a width by length (CSSOM, BR-212).
  import { SvelteSet } from 'svelte/reactivity';
  import { tick } from 'svelte';
  import { css, focusInView } from '../lib/dom';
  import { levelName, t } from '../lib/i18n';
  import {
    CHILD_LEVEL,
    CONNECTION_DRAG,
    dispatchKey,
    lengthWidth,
    levelClass,
    visibleRows,
    type Decoration,
  } from '../lib/tree';

  let {
    nodes,
    selected = null,
    readOnly = false,
    decorations = {},
    highlight = new Set<string>(),
    onSelect = () => {},
    edits,
  }: {
    nodes: readonly TreeNode[];
    selected?: string | null;
    readOnly?: boolean;
    decorations?: Readonly<Record<string, Decoration>>;
    highlight?: ReadonlySet<string>;
    onSelect?: (node: TreeNode) => void;
    edits?: TreeEdits;
  } = $props();

  const collapsed = new SvelteSet<string>();
  let renaming = $state<string | null>(null);
  let adding = $state<string | null>(null);
  let draft = $state('');
  let over = $state<string | null>(null);
  let list = $state<HTMLElement | null>(null);

  const rows = $derived(visibleRows(nodes, collapsed));
  const editable = $derived(!readOnly && edits !== undefined);
  const focusId = $derived(
    rows.some((r) => r.node.id === selected) ? selected : (rows[0]?.node.id ?? null),
  );

  function decoration(node: TreeNode): Decoration | undefined {
    return decorations[node.id] ?? decorations[node.path];
  }

  function toggle(node: TreeNode, open?: boolean): void {
    const shut = open === undefined ? !collapsed.has(node.id) : !open;
    if (shut) collapsed.add(node.id);
    else collapsed.delete(node.id);
  }

  function focusRow(index: number): void {
    const items = list?.querySelectorAll<HTMLElement>('[role="treeitem"]');
    items?.[Math.max(0, Math.min(index, items.length - 1))]?.focus();
  }

  function startRename(node: TreeNode): void {
    if (!editable || node.locked_by || node.level === 'org') return;
    renaming = node.id;
    draft = node.name;
  }

  function startAdd(node: TreeNode): void {
    if (!editable || node.locked_by || !CHILD_LEVEL[node.level]) return;
    adding = node.id;
    draft = '';
    toggle(node, true);
  }

  export function addClient(): void {
    const org = nodes.find((node) => node.level === 'org');
    if (!org) return;
    startAdd(org);
    void tick().then(() => focusInView(list?.querySelector<HTMLInputElement>('.adding input')));
  }

  function onKey(event: KeyboardEvent, node: TreeNode, index: number, kids: number): void {
    if (event.target !== event.currentTarget) return;
    const handled = dispatchKey(event.key, !editable, {
      select: () => onSelect(node),
      next: () => focusRow(index + 1),
      previous: () => focusRow(index - 1),
      expand: () => kids > 0 && toggle(node, true),
      collapse: () => kids > 0 && toggle(node, false),
      rename: () => startRename(node),
      remove: () => {
        if (!node.locked_by && node.level !== 'org') edits?.remove(node);
      },
    });
    if (handled) event.preventDefault();
  }

  function commit(event: KeyboardEvent | FocusEvent, node: TreeNode, kind: 'rename' | 'add'): void {
    if ((kind === 'rename' ? renaming : adding) !== node.id) return;
    if (event instanceof KeyboardEvent) {
      if (event.key === 'Escape') {
        event.preventDefault();
        renaming = adding = null;
        return;
      }
      if (event.key !== 'Enter') return;
      event.preventDefault();
    }
    const name = draft.trim();
    renaming = adding = null;
    if (!name) return;
    if (kind === 'rename' && name !== node.name) edits?.rename(node, name);
    if (kind === 'add') edits?.add(node, name);
  }

  function dragStart(event: DragEvent, node: TreeNode): void {
    event.dataTransfer?.setData(NODE_DRAG, node.id);
    if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move';
  }

  function dragOver(event: DragEvent, node: TreeNode): void {
    const types = event.dataTransfer?.types ?? [];
    if (!types.includes(CONNECTION_DRAG) && !types.includes(NODE_DRAG)) return;
    event.preventDefault();
    over = node.id;
  }

  function drop(event: DragEvent, node: TreeNode): void {
    event.preventDefault();
    over = null;
    const connection = event.dataTransfer?.getData(CONNECTION_DRAG);
    if (connection) {
      edits?.drop(node, connection);
      return;
    }
    const moved = nodes.find((n) => n.id === event.dataTransfer?.getData(NODE_DRAG));
    if (moved && moved !== node) edits?.move(moved, node);
  }

  /** The focus follows the row the operator is renaming or adding under. */
  function autofocus(el: HTMLInputElement): void {
    el.focus();
    el.select();
  }
</script>

<div class="tree" role="tree" aria-label={t('tree.title')} bind:this={list} data-readonly={readOnly}>
  {#each rows as row, index (row.node.id)}
    {@const node = row.node}
    {@const deco = decoration(node)}
    <div
      role="treeitem"
      aria-level={row.depth + 1}
      aria-selected={node.id === selected}
      aria-expanded={row.kids > 0 ? !collapsed.has(node.id) : undefined}
      tabindex={node.id === focusId ? 0 : -1}
      class="row depth-{Math.min(row.depth, 5)}"
      class:selected={node.id === selected}
      class:hl={highlight.has(node.path)}
      class:over={over === node.id}
      class:locked={node.locked_by !== null}
      data-path={node.path}
      data-level={node.level}
      draggable={editable && !node.locked_by && node.level !== 'org' ? 'true' : undefined}
      onclick={() => onSelect(node)}
      ondblclick={() => startRename(node)}
      onkeydown={(e) => onKey(e, node, index, row.kids)}
      ondragstart={editable ? (e) => dragStart(e, node) : undefined}
      ondragover={editable ? (e) => dragOver(e, node) : undefined}
      ondragleave={editable ? () => (over = null) : undefined}
      ondrop={editable ? (e) => drop(e, node) : undefined}
    >
      {#if row.kids > 0}
        <button
          type="button"
          class="twist"
          tabindex="-1"
          aria-label={collapsed.has(node.id)
            ? t('tree.expand', { name: node.name })
            : t('tree.collapse', { name: node.name })}
          onclick={(e) => (e.stopPropagation(), toggle(node))}>{collapsed.has(node.id) ? '▸' : '▾'}</button
        >
      {:else}
        <span class="twist" aria-hidden="true"></span>
      {/if}
      <span class="kind">{levelName(node.level)}</span>
      {#if renaming === node.id}
        <input
          class="inline"
          aria-label={t('tree.rename_aria')}
          bind:value={draft}
          use:autofocus
          onkeydown={(e) => commit(e, node, 'rename')}
          onblur={(e) => commit(e, node, 'rename')}
          onclick={(e) => e.stopPropagation()}
        />
      {:else}
        <span class="name" title={editable ? t('tree.rename_hint') : undefined}>{node.name}</span>
      {/if}
      <span class="seg mono">{node.level === 'org' ? '/' : node.segment}</span>
      {#if node.sensitive}<span class="badge sens">{t('badge.sensitive')}</span>{/if}
      {#if node.locked_here}
        <span class="badge lock">{t('badge.locked')}</span>
      {:else if node.locked_by}
        <span class="badge lock" title={t('tree.locked_by', { path: node.locked_by })}>
          {t('badge.locked')}
        </span>
      {/if}
      {#if deco}
        <span class="deco {levelClass(deco.level)}" data-length={deco.length}>
          <span class="bar" use:css={{ width: lengthWidth(deco.length) }}></span>
          <span class="text">{deco.text}</span>
        </span>
      {/if}
      {#if editable}
        <span class="acts">
          {#if CHILD_LEVEL[node.level] && !node.locked_by}
            <button
              type="button"
              class="icon"
              tabindex="-1"
              aria-label={t('tree.add_child', {
                type: levelName(CHILD_LEVEL[node.level] ?? 'role'),
                name: node.name,
              })}
              title={t('tree.add_child', {
                type: levelName(CHILD_LEVEL[node.level] ?? 'role'),
                name: node.name,
              })}
              onclick={(e) => (e.stopPropagation(), startAdd(node))}>+</button
            >
          {/if}
          {#if node.level !== 'org' && !node.locked_by}
            <button
              type="button"
              class="icon"
              tabindex="-1"
              aria-label={t('tree.delete', { name: node.name })}
              title={t('tree.delete', { name: node.name })}
              onclick={(e) => (e.stopPropagation(), edits?.remove(node))}>×</button
            >
          {/if}
        </span>
      {/if}
    </div>
    {#if adding === node.id}
      <div class="row adding depth-{Math.min(row.depth + 1, 5)}">
        <span class="twist" aria-hidden="true"></span>
        <span class="kind">{levelName(CHILD_LEVEL[node.level] ?? 'role')}</span>
        <input
          class="inline"
          aria-label={t('tree.add_aria', { type: levelName(CHILD_LEVEL[node.level] ?? 'role') })}
          placeholder={t('tree.add_placeholder')}
          bind:value={draft}
          use:autofocus
          onkeydown={(e) => commit(e, node, 'add')}
          onblur={(e) => commit(e, node, 'add')}
        />
      </div>
    {/if}
  {/each}
</div>

<style>
  .tree {
    display: flex;
    flex-direction: column;
    gap: 1px;
  }
  .row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: 3px var(--space-2);
    border-radius: var(--radius);
    border: 1px solid transparent;
    cursor: default;
    min-height: 30px;
  }
  .row:hover {
    background: var(--hover);
  }
  .row.selected {
    background: var(--hover);
    border-color: var(--primary);
  }
  .row.hl {
    border-color: var(--cyan);
    box-shadow: inset 3px 0 0 var(--cyan);
  }
  .row.over {
    border-style: dashed;
    border-color: var(--cyan);
  }
  .depth-1 {
    padding-left: calc(var(--space-2) + 1.25rem);
  }
  .depth-2 {
    padding-left: calc(var(--space-2) + 2.5rem);
  }
  .depth-3 {
    padding-left: calc(var(--space-2) + 3.75rem);
  }
  .depth-4 {
    padding-left: calc(var(--space-2) + 5rem);
  }
  .depth-5 {
    padding-left: calc(var(--space-2) + 6.25rem);
  }
  .twist {
    width: 1rem;
    flex: none;
    background: none;
    border: none;
    padding: 0;
    color: var(--muted);
  }
  .kind {
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--faint);
    min-width: 5.5rem;
  }
  .name {
    color: var(--text);
  }
  .seg {
    color: var(--faint);
    font-size: 0.8rem;
  }
  .inline {
    padding: 1px var(--space-2);
    min-width: 10rem;
  }
  .acts {
    margin-left: auto;
    display: flex;
    gap: var(--space-1);
    opacity: 0;
  }
  .row:hover .acts,
  .row:focus-within .acts,
  .row.selected .acts {
    opacity: 1;
  }
  .icon {
    background: none;
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    width: 1.5rem;
    height: 1.5rem;
    padding: 0;
    line-height: 1;
  }
  .deco {
    position: relative;
    margin-left: auto;
    width: 9rem;
    height: 1.1rem;
    border-radius: 3px;
    background: var(--surface-2);
    overflow: hidden;
    font-size: 0.75rem;
  }
  .deco .bar {
    position: absolute;
    inset: 0 auto 0 0;
  }
  .deco .text {
    position: relative;
    padding: 0 var(--space-1);
  }
  .deco-l0 .bar {
    background: var(--ok-bg);
  }
  .deco-l1 .bar {
    background: var(--warn-bg);
  }
  .deco-l2 .bar {
    background: var(--danger-bg);
  }
  .deco-l3 .bar {
    background: var(--danger);
  }
</style>
