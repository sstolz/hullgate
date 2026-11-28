// The shell's tabs come from this one table (§3.17): a screen is added by one entry, which carries
// the screen the shell renders, and no tab is shown before its screen exists. Slice 2 ships Tree,
// Tokens and Audit; slice 3 adds Tools, whose entry also carries its badge (the queue to classify,
// §3.4); Organisation comes with its slice.

import type { Component } from 'svelte';
import Audit from '../screens/Audit.svelte';
import Import from '../screens/Import.svelte';
import Organisation from '../screens/Organisation.svelte';
import Tokens from '../screens/Tokens.svelte';
import Tools from '../screens/Tools.svelte';
import TreeScreen from '../screens/TreeScreen.svelte';
import type { Client } from './api';
import type { Key } from './i18n';
import { queueCount } from './policy';

export interface Tab {
  id: string;
  label: Key;
  screen: Component<{ client: Client; landing?: SetupLanding | null }>;
  /** A count the shell polls for the tab's badge, without extending the session (BR-204). */
  badge?: (client: Client) => Promise<number>;
}

export const TABS: readonly Tab[] = [
  { id: 'tree', label: 'tab.tree', screen: TreeScreen },
  { id: 'tokens', label: 'tab.tokens', screen: Tokens },
  { id: 'tools', label: 'tab.tools', screen: Tools, badge: queueCount },
  { id: 'audit', label: 'tab.audit', screen: Audit },
  { id: 'import', label: 'tab.import', screen: Import },
  { id: 'organisation', label: 'tab.organisation', screen: Organisation },
];

export const BASE = '/admin';
export const CALLBACK = `${BASE}/oauth/callback`;

export type SetupTarget = 'client' | 'service' | 'connection' | 'placement' | 'credential';
// A fresh request object lets a repeated click land again even on the same screen.
export interface SetupLanding { target: SetupTarget }
export const SETUP_NAVIGATION = Symbol('setup-navigation');
export type SetupNavigation = (url: string) => boolean;

export interface Callback {
  state: string;
  code: string | null;
  error: string | null;
}

export type Place =
  | { kind: 'enrol'; token: string }
  | { kind: 'callback'; callback: Callback }
  | { kind: 'tab'; tab: string; landing?: SetupLanding }
  | { kind: 'unknown' };

/**
 * Where a location of the console points. `/admin/enrol/<token>` is an enrolment link (BR-203);
 * `/admin/oauth/callback?state=…&code=…` is a provider's return to the console (BR-216).
 */
export function place(pathname: string, tabs: readonly Tab[] = TABS, search = ''): Place {
  const rest = pathname.startsWith(BASE) ? pathname.slice(BASE.length) : null;
  if (rest === null) return { kind: 'unknown' };
  const parts = rest.split('/').filter(Boolean);
  if (parts[0] === 'enrol' && parts.length === 2 && parts[1]) {
    return { kind: 'enrol', token: parts[1] };
  }
  if (parts.length === 2 && parts[0] === 'oauth' && parts[1] === 'callback') {
    const q = new URLSearchParams(search);
    // read as it comes, even incomplete: the server judges it (BR-211), the address bar is cleared
    const callback = { state: q.get('state') ?? '', code: q.get('code'), error: q.get('error') };
    return { kind: 'callback', callback };
  }
  const first = tabs[0];
  if (parts.length === 0) return first ? { kind: 'tab', tab: first.id } : { kind: 'unknown' };
  const tab = tabs.find((t) => t.id === parts[0]);
  const target = new URLSearchParams(search).get('setup');
  if (tab && ((tab.id === 'tree' &&
    (target === 'client' || target === 'service' || target === 'connection' || target === 'placement')) ||
    (tab.id === 'tokens' && target === 'credential'))) {
    return { kind: 'tab', tab: tab.id, landing: { target } };
  }
  return tab ? { kind: 'tab', tab: tab.id } : { kind: 'unknown' };
}

export function pathOf(tab: string): string {
  return `${BASE}/${tab}`;
}
