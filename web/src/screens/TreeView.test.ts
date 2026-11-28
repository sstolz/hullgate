// AC-37 (§3.17): the tree view F-017 builds on. Rendered on the server side (no DOM in the unit
// tests): the markup shows what each mode offers; the selection event is the keyboard dispatch
// the view runs (`dispatchKey`, tested in lib/tree.test.ts) and its click handler.
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import { NODES } from '../lib/tree.fixtures';
import type { TreeEdits } from './TreeView.svelte';
import TreeView from './TreeView.svelte';

const noop = (): void => {};
const edits: TreeEdits = { add: noop, rename: noop, remove: noop, move: noop, drop: noop };

describe('TreeView (§3.17, AC-37)', () => {
  it('offers drag, add and delete when editable', () => {
    const { body } = render(TreeView, { props: { nodes: NODES, edits } });
    expect(body).toContain('draggable="true"');
    expect(body).toContain('Delete ops');
    expect(body).toContain('Add a role under prod');
  });

  it('offers no drag, rename, add or delete in read-only mode', () => {
    const { body } = render(TreeView, { props: { nodes: NODES, edits, readOnly: true } });
    expect(body).not.toContain('draggable');
    expect(body).not.toContain('Delete ops');
    expect(body).not.toContain('Add a ');
    expect(body).not.toContain('to rename');
    expect(body).toContain('data-readonly="true"');
    expect(body.match(/role="treeitem"/g)).toHaveLength(NODES.length);
  });

  it('marks the selected node', () => {
    const { body } = render(TreeView, { props: { nodes: NODES, readOnly: true, selected: 'ops' } });
    expect(body).toMatch(/aria-selected="true"[^>]*data-path="acme\/web\/prod\/ops"/);
  });

  it('renders a level, a length and a text passed to it, by path or by id', () => {
    const { body } = render(TreeView, {
      props: {
        nodes: NODES,
        readOnly: true,
        decorations: {
          'acme/web/prod/ops': { level: 2, length: 0.4, text: '6.1% · 1 240' },
          dev: { level: 0, length: 0.1, text: '0.2%' },
        },
      },
    });
    expect(body).toMatch(/class="deco deco-l2[^"]*" data-length="0.4"/);
    expect(body).toContain('6.1% · 1 240');
    expect(body).toMatch(/class="deco deco-l0[^"]*" data-length="0.1"/);
    expect(body).toContain('0.2%');
    // no inline style: widths go through CSSOM at mount (BR-212)
    expect(body).not.toContain('style=');
  });
});
