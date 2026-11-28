// AC-37 (§3.17): a tab is added by one entry of the route table. Rendered on the server side (no
// DOM in the unit tests): one entry added to the table shows its tab and, selected, its screen;
// the shell names no screen of its own.
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import type { Administrator, Client } from '../lib/api';
import { TABS, type Tab } from '../lib/routes';
import Shell from './Shell.svelte';
import TabProbe from './TabProbe.fixture.svelte';

const client = {} as Client;
const admin = { email: 'ops@example.test' } as Administrator;
const noop = (): void => {};
const base = { client, admin, theme: 'dark' as const, onTab: noop, onTheme: noop, onSignedOut: noop };

describe('Shell renders the route table (§3.17, AC-37)', () => {
  it('shows the screen an added entry carries, under its tab', () => {
    const tabs: Tab[] = [...TABS, { id: 'stats', label: 'tab.audit', screen: TabProbe }];
    const { body } = render(Shell, { props: { ...base, tabs, tab: 'stats' } });
    expect(body).toContain('data-probe="stats"');
    expect(body).toContain('with its client');
    expect(body.match(/class="tab/g)).toHaveLength(TABS.length + 1);
  });

  it('shows no screen for an id the table does not hold', () => {
    const { body } = render(Shell, { props: { ...base, tab: 'stats' } });
    expect(body).not.toContain('data-probe');
    expect(body.match(/class="tab/g)).toHaveLength(TABS.length);
  });
});
