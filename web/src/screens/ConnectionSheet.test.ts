import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import type { Client, Connection } from '../lib/api';
import ConnectionSheet, { type ConnectionActs } from './ConnectionSheet.svelte';

const connection: Connection = {
  name: 'acme/key', service: 'provider', type: 'provided', stored_state: 'active', state: 'active',
  application: null, key: null, expiry_estimated: false, not_before: null,
  last_outcome: null, application_to_redeclare: null, upstream: 'not declared', refusal: null,
  prefix: 'key', account: 'operator', scopes: [], placements: [],
  access_ends_at: '2030-01-07T00:00:00Z', expires_at: '2030-01-07T00:00:00Z',
};
const acts = {} as ConnectionActs;
const client = {} as Client;

describe('BR-238 connection expiry presentation', () => {
  it('shows the server warning and action date', () => {
    const { body } = render(ConnectionSheet, {props: {client, acts, connection: {...connection, expiring: true}}});
    expect(body).toContain('data-state="SCR-07.expiring"');
    expect(body).toContain('Access ends on 2030-01-07T00:00:00Z.');
    expect(body).toContain('Re-authenticate or replace the provided token');
  });
  it('does not infer a warning from a date when the server says no', () => {
    const { body } = render(ConnectionSheet, {props: {client, acts, connection: {...connection, expiring: false, access_ends_at: '2000-01-01T00:00:00Z'}}});
    expect(body).not.toContain('SCR-07.expiring');
    expect(body).not.toContain('Access ends on');
  });
});
