// Display helpers over the server's flat node list (`GET nodes`, depth first). They arrange what the
// server sent for display; levels, paths, locks, sensitivity and URLs stay the server's (BR-211).

import type { Level, TreeNode } from './api';

/** The data type a drag from the Library carries: a connection's name (§3.5). */
export const CONNECTION_DRAG = 'application/x-hullgate-connection';

/** The level a child of `level` has (BR-044), or null under a role. */
export const CHILD_LEVEL: Record<Level, Level | null> = {
  org: 'client',
  client: 'project',
  project: 'env',
  env: 'role',
  role: null,
};

interface Index {
  parent: Map<TreeNode, TreeNode>;
  kids: Map<TreeNode, TreeNode[]>;
}

const indexes = new WeakMap<readonly TreeNode[], Index>();

/** Parent and children of every node, built once per list (2 000 nodes are held, §9). */
function index(nodes: readonly TreeNode[]): Index {
  const known = indexes.get(nodes);
  if (known) return known;
  const byKey = new Map<string, TreeNode>();
  for (const n of nodes) {
    byKey.set(`id:${n.id}`, n);
    byKey.set(`path:${n.path}`, n);
  }
  const built: Index = { parent: new Map(), kids: new Map(nodes.map((n) => [n, []])) };
  for (const n of nodes) {
    if (n.parent === null) continue;
    // the server names the parent by id or by path; both are accepted
    const p = byKey.get(`id:${n.parent}`) ?? byKey.get(`path:${n.parent}`);
    if (!p || p === n) continue;
    built.parent.set(n, p);
    built.kids.get(p)?.push(n);
  }
  indexes.set(nodes, built);
  return built;
}

export function parentOf(node: TreeNode, nodes: readonly TreeNode[]): TreeNode | undefined {
  return index(nodes).parent.get(node);
}

export function childrenOf(node: TreeNode, nodes: readonly TreeNode[]): TreeNode[] {
  return index(nodes).kids.get(node) ?? [];
}

export function depthOf(node: TreeNode, nodes: readonly TreeNode[]): number {
  let depth = 0;
  for (let p = parentOf(node, nodes); p; p = parentOf(p, nodes)) depth++;
  return depth;
}

/** The node and everything under it, in the server's order. */
export function subtree(node: TreeNode, nodes: readonly TreeNode[]): TreeNode[] {
  const out: TreeNode[] = [node];
  for (const kid of childrenOf(node, nodes)) out.push(...subtree(kid, nodes));
  return out;
}

export function rolesBelow(node: TreeNode, nodes: readonly TreeNode[]): TreeNode[] {
  return subtree(node, nodes).filter((n) => n.level === 'role');
}

/** Rows of the List view: the nodes whose ancestors are all expanded, with their depth. */
export function visibleRows(
  nodes: readonly TreeNode[],
  collapsed: ReadonlySet<string>,
): { node: TreeNode; depth: number; kids: number }[] {
  const out: { node: TreeNode; depth: number; kids: number }[] = [];
  const walk = (node: TreeNode, depth: number): void => {
    const kids = childrenOf(node, nodes);
    out.push({ node, depth, kids: kids.length });
    if (!collapsed.has(node.id)) for (const k of kids) walk(k, depth + 1);
  };
  for (const root of nodes.filter((n) => parentOf(n, nodes) === undefined)) walk(root, 0);
  return out;
}

/** §3.17's decoration of a node: a level, a length and a short text, all computed by the server. */
export interface Decoration {
  level: number;
  length: number;
  text: string;
}

export const DECORATION_LEVELS = 4;

/** The colour class of a decoration level: a mapping, nothing more (BR-211). */
export function levelClass(level: number): string {
  const n = Number.isFinite(level) ? Math.trunc(level) : 0;
  return `deco-l${Math.min(Math.max(n, 0), DECORATION_LEVELS - 1)}`;
}

/** The width of a decoration length, a fraction of the row's bar; set through CSSOM (BR-212). */
export function lengthWidth(length: number): string {
  const f = Number.isFinite(length) ? Math.min(Math.max(length, 0), 1) : 0;
  return `${Math.round(f * 1000) / 10}%`;
}

export type RowAct = 'select' | 'rename' | 'remove' | 'expand' | 'collapse' | 'next' | 'previous';

/**
 * What a key pressed on a row of the tree does (BR-232, the keyboard paths of #18). In read-only
 * mode (§3.17) only moving and selecting remain.
 */
export function rowKey(key: string, readOnly: boolean): RowAct | null {
  switch (key) {
    case 'Enter':
    case ' ':
      return 'select';
    case 'ArrowDown':
      return 'next';
    case 'ArrowUp':
      return 'previous';
    case 'ArrowRight':
      return 'expand';
    case 'ArrowLeft':
      return 'collapse';
    case 'F2':
      return readOnly ? null : 'rename';
    case 'Delete':
      return readOnly ? null : 'remove';
    default:
      return null;
  }
}

/**
 * Runs what a key pressed on a row does; true when the key was the tree's. The view's selection
 * event is `select`; in read-only mode `rename` and `remove` are never called (§3.17).
 */
export function dispatchKey(
  key: string,
  readOnly: boolean,
  handlers: Record<RowAct, () => void>,
): boolean {
  const act = rowKey(key, readOnly);
  if (!act) return false;
  handlers[act]();
  return true;
}

/** The map's layout: one column per depth, leaves stacked, a parent centred on its children. */
export interface Spot {
  node: TreeNode;
  x: number;
  y: number;
}

export const MAP = { col: 190, row: 44, pad: 16, w: 160, h: 30 } as const;

export function mapLayout(nodes: readonly TreeNode[]): {
  spots: Spot[];
  links: string;
  width: number;
  height: number;
} {
  const spots = new Map<TreeNode, Spot>();
  let leaf = 0;
  let deepest = 0;
  const lay = (node: TreeNode, depth: number): number => {
    deepest = Math.max(deepest, depth);
    const kids = childrenOf(node, nodes);
    let y: number;
    if (kids.length === 0) {
      y = MAP.pad + leaf * MAP.row;
      leaf++;
    } else {
      const ys = kids.map((k) => lay(k, depth + 1));
      y = (Math.min(...ys) + Math.max(...ys)) / 2;
    }
    spots.set(node, { node, x: MAP.pad + depth * MAP.col, y });
    return y;
  };
  for (const root of nodes.filter((n) => parentOf(n, nodes) === undefined)) lay(root, 0);
  const segments: string[] = [];
  for (const spot of spots.values()) {
    const parent = parentOf(spot.node, nodes);
    const from = parent ? spots.get(parent) : undefined;
    if (!from) continue;
    const x1 = from.x + MAP.w;
    const y1 = from.y + MAP.h / 2;
    const x2 = spot.x;
    const y2 = spot.y + MAP.h / 2;
    const mid = (x1 + x2) / 2;
    segments.push(`M${x1} ${y1}C${mid} ${y1} ${mid} ${y2} ${x2} ${y2}`);
  }
  return {
    spots: [...spots.values()],
    links: segments.join(''),
    width: MAP.pad * 2 + deepest * MAP.col + MAP.w,
    height: MAP.pad * 2 + Math.max(leaf, 1) * MAP.row,
  };
}

export interface View {
  left: number;
  top: number;
  width: number;
  height: number;
}

/**
 * Where the map's viewport scrolls so the highlighted nodes can be read (SCR-04.map, §3.5): null
 * when none is highlighted or all are already in view; otherwise the scroll position that centres
 * their bounding box, clamped at the origin.
 */
export function reveal(
  spots: readonly Spot[],
  highlight: ReadonlySet<string>,
  zoom: number,
  view: View,
): { left: number; top: number } | null {
  const lit = spots.filter((s) => highlight.has(s.node.path));
  if (lit.length === 0) return null;
  const x0 = Math.min(...lit.map((s) => s.x)) * zoom;
  const y0 = Math.min(...lit.map((s) => s.y)) * zoom;
  const x1 = Math.max(...lit.map((s) => s.x + MAP.w)) * zoom;
  const y1 = Math.max(...lit.map((s) => s.y + MAP.h)) * zoom;
  const inside =
    x0 >= view.left && y0 >= view.top && x1 <= view.left + view.width && y1 <= view.top + view.height;
  if (inside) return null;
  return {
    left: Math.max(0, Math.round((x0 + x1) / 2 - view.width / 2)),
    top: Math.max(0, Math.round((y0 + y1) / 2 - view.height / 2)),
  };
}
