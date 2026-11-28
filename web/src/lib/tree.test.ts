import { describe, expect, it } from 'vitest';
import type { TreeNode } from './api';
import { NODES } from './tree.fixtures';
import {
  childrenOf,
  dispatchKey,
  lengthWidth,
  levelClass,
  mapLayout,
  parentOf,
  reveal,
  rolesBelow,
  rowKey,
  visibleRows,
  type RowAct,
} from './tree';

describe('tree helpers', () => {
  it('reads the parent by id or by path, as the server names it', () => {
    const byPath = NODES.map((n) => ({ ...n, parent: parentOf(n, NODES)?.path ?? null }));
    const ops = byPath.find((n) => n.id === 'ops');
    expect(ops && parentOf(ops, byPath)?.id).toBe('prod');
    expect(childrenOf(NODES[3] as TreeNode, NODES).map((n) => n.id)).toEqual(['ops', 'dev']);
  });

  it('lists the roles below a node', () => {
    expect(rolesBelow(NODES[1] as TreeNode, NODES).map((n) => n.path)).toEqual([
      'acme/web/prod/ops',
      'acme/web/prod/dev',
    ]);
  });

  it('hides the rows under a collapsed node', () => {
    expect(visibleRows(NODES, new Set()).map((r) => r.depth)).toEqual([0, 1, 2, 3, 4, 4]);
    expect(visibleRows(NODES, new Set(['web'])).map((r) => r.node.id)).toEqual([
      'org',
      'acme',
      'web',
    ]);
  });

  it('lays the map out one column per depth', () => {
    const layout = mapLayout(NODES);
    expect(layout.spots).toHaveLength(NODES.length);
    const xs = new Map(layout.spots.map((s) => [s.node.id, s.x]));
    expect((xs.get('acme') ?? 0) < (xs.get('web') ?? 0)).toBe(true);
    expect(layout.links.match(/M/g)).toHaveLength(NODES.length - 1);
  });
});

describe('decoration (§3.17)', () => {
  it('maps a level to a colour class and a length to a width, nothing more', () => {
    expect(levelClass(0)).toBe('deco-l0');
    expect(levelClass(2)).toBe('deco-l2');
    expect(levelClass(99)).toBe('deco-l3');
    expect(levelClass(-1)).toBe('deco-l0');
    expect(lengthWidth(0.5)).toBe('50%');
    expect(lengthWidth(2)).toBe('100%');
    expect(lengthWidth(Number.NaN)).toBe('0%');
  });
});

describe('keyboard (BR-232)', () => {
  it('offers F2 and Delete only when the view is editable', () => {
    expect(rowKey('F2', false)).toBe('rename');
    expect(rowKey('Delete', false)).toBe('remove');
    expect(rowKey('F2', true)).toBeNull();
    expect(rowKey('Delete', true)).toBeNull();
    expect(rowKey('Enter', true)).toBe('select');
    expect(rowKey('x', false)).toBeNull();
  });

  it('emits the selection event in read-only mode, never a rename or a delete (AC-37)', () => {
    const seen: RowAct[] = [];
    const handlers = Object.fromEntries(
      (['select', 'rename', 'remove', 'expand', 'collapse', 'next', 'previous'] as RowAct[]).map(
        (a) => [a, (): void => void seen.push(a)],
      ),
    ) as Record<RowAct, () => void>;
    for (const key of ['Enter', 'F2', 'Delete', 'ArrowDown']) dispatchKey(key, true, handlers);
    expect(seen).toEqual(['select', 'next']);
  });
});

describe('map reveal (SCR-04.map, §3.5)', () => {
  const layout = mapLayout(NODES);
  const roles = new Set(NODES.filter((n) => n.level === 'role').map((n) => n.path));

  it('brings highlighted nodes off the viewport into it', () => {
    const to = reveal(layout.spots, roles, 1, { left: 0, top: 0, width: 300, height: 200 });
    expect(to).not.toBeNull();
    const lit = layout.spots.filter((s) => roles.has(s.node.path));
    for (const s of lit) expect(s.x).toBeGreaterThanOrEqual((to?.left ?? 0) - 300);
    expect(to?.left).toBeGreaterThan(0);
  });

  it('leaves the view alone when nothing is highlighted or all is in view', () => {
    const all = { left: 0, top: 0, width: layout.width, height: layout.height };
    expect(reveal(layout.spots, new Set(), 1, { ...all, width: 10, height: 10 })).toBeNull();
    expect(reveal(layout.spots, roles, 1, all)).toBeNull();
  });
});
