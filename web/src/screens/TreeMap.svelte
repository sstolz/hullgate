<script lang="ts">
  // SCR-04.map: the tree drawn left to right. Hovering or selecting a connection in the Library
  // highlights its placements (CSS classes); a connection can be dropped on a node here too.
  // Positions and zoom go through CSSOM (BR-212).
  import type { TreeNode } from '../lib/api';
  import { css } from '../lib/dom';
  import { levelName, t } from '../lib/i18n';
  import { CONNECTION_DRAG, MAP, mapLayout, reveal } from '../lib/tree';

  let {
    nodes,
    selected = null,
    highlight = new Set<string>(),
    legend = '',
    onSelect,
    onDrop,
  }: {
    nodes: readonly TreeNode[];
    selected?: string | null;
    highlight?: ReadonlySet<string>;
    legend?: string;
    onSelect: (node: TreeNode) => void;
    onDrop?: (node: TreeNode, connection: string) => void;
  } = $props();

  let zoom = $state(1);
  let over = $state<string | null>(null);
  let viewport = $state<HTMLDivElement | null>(null);
  const layout = $derived(mapLayout(nodes));

  // highlighted placements off the viewport are brought into it, so they can be read (§3.5)
  $effect(() => {
    if (!viewport) return;
    const view = {
      left: viewport.scrollLeft,
      top: viewport.scrollTop,
      width: viewport.clientWidth,
      height: viewport.clientHeight,
    };
    const to = reveal(layout.spots, highlight, zoom, view);
    if (to) viewport.scrollTo({ left: to.left, top: to.top, behavior: 'instant' });
  });

  function dragOver(event: DragEvent, node: TreeNode): void {
    if (!onDrop || !(event.dataTransfer?.types ?? []).includes(CONNECTION_DRAG)) return;
    event.preventDefault();
    over = node.id;
  }

  function drop(event: DragEvent, node: TreeNode): void {
    event.preventDefault();
    over = null;
    const name = event.dataTransfer?.getData(CONNECTION_DRAG);
    if (name) onDrop?.(node, name);
  }
</script>

<div class="map" data-state="SCR-04.map">
  <div class="ctl">
    <button type="button" class="ghost" aria-label={t('map.zoom_out')} onclick={() => (zoom = Math.max(0.4, zoom / 1.2))}>−</button>
    <button type="button" class="ghost" aria-label={t('map.zoom_in')} onclick={() => (zoom = Math.min(2.5, zoom * 1.2))}>+</button>
    <button type="button" class="ghost" onclick={() => (zoom = 1)}>{t('map.reset')}</button>
  </div>
  <div class="viewport" bind:this={viewport}>
    <div
      class="inner"
      use:css={{
        width: `${layout.width * zoom}px`,
        height: `${layout.height * zoom}px`,
      }}
    >
      <div
        class="plane"
        use:css={{
          width: `${layout.width}px`,
          height: `${layout.height}px`,
          transform: `scale(${zoom})`,
        }}
      >
        <svg class="links" width={layout.width} height={layout.height} aria-hidden="true">
          <path d={layout.links} />
        </svg>
        {#each layout.spots as spot (spot.node.id)}
          <button
            type="button"
            class="node lvl-{spot.node.level}"
            class:selected={spot.node.id === selected}
            class:hl={highlight.has(spot.node.path)}
            class:over={over === spot.node.id}
            class:sens={spot.node.sensitive}
            data-path={spot.node.path}
            use:css={{
              left: `${spot.x}px`,
              top: `${spot.y}px`,
              width: `${MAP.w}px`,
              height: `${MAP.h}px`,
            }}
            title={`${levelName(spot.node.level)} ${spot.node.path}`}
            onclick={() => onSelect(spot.node)}
            ondragover={(e) => dragOver(e, spot.node)}
            ondragleave={() => (over = null)}
            ondrop={(e) => drop(e, spot.node)}
          >
            {spot.node.name}{#if spot.node.locked_by}<span class="lock" aria-label={t('badge.locked')}>·</span>{/if}
          </button>
        {/each}
      </div>
    </div>
  </div>
  <p class="legend" role="status">{legend || t('map.legend_none')}</p>
</div>

<style>
  .map {
    position: relative;
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }
  .ctl {
    display: flex;
    gap: var(--space-1);
    justify-content: flex-end;
  }
  .viewport {
    overflow: auto;
    min-height: 420px;
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    background-image: radial-gradient(var(--rule) 1px, transparent 1px);
    background-size: 20px 20px;
  }
  .inner {
    position: relative;
  }
  .plane {
    position: absolute;
    left: 0;
    top: 0;
    transform-origin: 0 0;
  }
  .links {
    position: absolute;
    left: 0;
    top: 0;
    overflow: visible;
    pointer-events: none;
  }
  .links path {
    fill: none;
    stroke: var(--rule);
    stroke-width: 1.5;
  }
  .node {
    position: absolute;
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
    text-align: left;
    padding: 0 var(--space-2);
    background: var(--surface-1);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    font-size: 0.85rem;
  }
  .node.lvl-role {
    border-style: dashed;
  }
  .node.sens {
    border-color: var(--warn-bd);
  }
  .node.selected {
    border-color: var(--primary);
    border-style: solid;
  }
  .node.hl {
    border-color: var(--cyan);
    border-style: solid;
    box-shadow: 0 0 0 2px var(--cyan);
  }
  .node.over {
    border-style: dashed;
    border-color: var(--cyan);
  }
  .lock {
    margin-left: var(--space-1);
    color: var(--warn);
  }
  .legend {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }
</style>
