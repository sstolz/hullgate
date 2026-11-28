import { describe, expect, it } from 'vitest';
import Audit from '../screens/Audit.svelte';
import Tools from '../screens/Tools.svelte';
import { queueCount } from './policy';
import { place, TABS, type Tab } from './routes';

describe('route table (§3.17, AC-37)', () => {
  it('recognises every server-owned setup target and its ordinary screen (BR-229)', () => {
    // Contract values also asserted against GET setup by test_br_229_guide.py.
    const targets = [
      '/admin/tree?setup=client', '/admin/tree?setup=service',
      '/admin/tree?setup=connection', '/admin/tree?setup=placement',
      '/admin/tokens?setup=credential',
    ];
    expect(new Set(targets).size).toBe(5);
    for (const target of ['client', 'service', 'connection', 'placement', 'credential']) {
      const url = new URL(targets.find(url => url.endsWith(`=${target}`))!, 'https://console.test');
      expect(place(url.pathname, TABS, url.search)).toEqual({
        kind: 'tab', tab: target === 'credential' ? 'tokens' : 'tree', landing: { target },
      });
    }
  });

  it('ignores a setup request for a different screen or an unknown act', () => {
    expect(place('/admin/tree', TABS, '?setup=credential')).toEqual({ kind: 'tab', tab: 'tree' });
    expect(place('/admin/tokens', TABS, '?setup=client')).toEqual({ kind: 'tab', tab: 'tokens' });
    expect(place('/admin/tree', TABS, '?setup=delete')).toEqual({ kind: 'tab', tab: 'tree' });
  });
  it('ships Tree, Tokens, Tools and Audit: no empty tab before its screen', () => {
    expect(TABS.map((t) => t.id)).toEqual(['tree', 'tokens', 'tools', 'audit', 'import', 'organisation']);
  });

  it('adds Tools with one entry, which carries its screen and its badge (slice 3)', () => {
    const tools = TABS.filter((t) => t.id === 'tools');
    expect(tools).toHaveLength(1);
    expect(tools[0]?.screen).toBe(Tools);
    expect(tools[0]?.badge).toBe(queueCount);
    expect(TABS.filter((t) => t.badge).map((t) => t.id)).toEqual(['tools']);
    expect(place('/admin/tools')).toEqual({ kind: 'tab', tab: 'tools' });
  });

  it('adds a tab with one entry', () => {
    const more: Tab[] = [...TABS, { id: 'stats', label: 'tab.tree', screen: Audit }];
    expect(place('/admin/stats', more)).toEqual({ kind: 'tab', tab: 'stats' });
    expect(place('/admin/stats')).toEqual({ kind: 'unknown' });
  });

  it('reads an enrolment link from the path (BR-203)', () => {
    expect(place('/admin/enrol/AbC-_9')).toEqual({ kind: 'enrol', token: 'AbC-_9' });
    expect(place('/admin/enrol')).toEqual({ kind: 'unknown' });
  });

  it('opens the first tab at the root', () => {
    expect(place('/admin')).toEqual({ kind: 'tab', tab: 'tree' });
    expect(place('/admin/')).toEqual({ kind: 'tab', tab: 'tree' });
    expect(place('/admin/tokens')).toEqual({ kind: 'tab', tab: 'tokens' });
    expect(place('/elsewhere')).toEqual({ kind: 'unknown' });
  });

  it("reads a provider's return to /admin/oauth/callback (BR-216)", () => {
    expect(place('/admin/oauth/callback', TABS, '?state=s1&code=c1')).toEqual({
      kind: 'callback',
      callback: { state: 's1', code: 'c1', error: null },
    });
    expect(place('/admin/oauth/callback', TABS, '?state=s1&error=access_denied')).toEqual({
      kind: 'callback',
      callback: { state: 's1', code: null, error: 'access_denied' },
    });
    // incomplete: still the callback route, so the address bar is cleared; the server judges it
    expect(place('/admin/oauth/callback', TABS, '?code=c1')).toEqual({
      kind: 'callback',
      callback: { state: '', code: 'c1', error: null },
    });
  });
});
