import { describe, expect, it } from 'vitest';
import { ApiError, Client, query, toolsNotice, type Fetch } from './api';

interface Seen {
  url: string;
  init: RequestInit;
}

function fake(status: number, body: unknown, seen: Seen[]): Fetch {
  return async (url, init = {}) => {
    seen.push({ url, init });
    return new Response(JSON.stringify(body), { status });
  };
}

const header = (s: Seen, name: string): string | undefined =>
  (s.init.headers as Record<string, string>)[name];

describe('Client', () => {
  it('sends the CSRF token on writes, never on reads (BR-205)', async () => {
    const seen: Seen[] = [];
    const client = new Client(fake(200, {}, seen));
    client.csrf = 'tok';
    await client.call('GET', 'session');
    await client.call('POST', 'session/signout');
    expect(header(seen[0]!, 'X-CSRF-Token')).toBeUndefined();
    expect(header(seen[1]!, 'X-CSRF-Token')).toBe('tok');
    expect(seen[1]!.url).toBe('/admin/api/v1/session/signout');
  });

  it('marks operator calls as activity and polls as not (BR-204)', async () => {
    const seen: Seen[] = [];
    const client = new Client(fake(200, {}, seen));
    await client.call('GET', 'session');
    await client.call('GET', 'meta', undefined, { activity: false });
    expect(header(seen[0]!, 'X-Hullgate-Activity')).toBe('1');
    expect(header(seen[1]!, 'X-Hullgate-Activity')).toBeUndefined();
  });

  it('shows the server refusal as it is and returns to sign-in on 401 (SCR-03)', async () => {
    let signedOut = 0;
    const client = new Client(fake(401, { code: 'unauthenticated', message: 'Sign in again.' }, []));
    client.onUnauthenticated = () => signedOut++;
    const error = await client.call('GET', 'meta').catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).message).toBe('Sign in again.');
    expect((error as ApiError).code).toBe('unauthenticated');
    expect(signedOut).toBe(1);
  });

  it('does not treat a failed assertion as a lost session', async () => {
    let signedOut = 0;
    const client = new Client(fake(401, { code: 'sign_in_failed', message: 'x' }, []));
    client.onUnauthenticated = () => signedOut++;
    await client.call('POST', 'auth/assert', {}).catch(() => undefined);
    expect(signedOut).toBe(0);
  });

  it('keeps the token returned at sign-in', () => {
    const client = new Client(fake(200, {}, []));
    const admin = client.signedIn({
      csrf_token: 'abc',
      administrator: { id: 'adm_1', email: 'a@x', name: 'a', state: 'active' },
    });
    expect(client.csrf).toBe('abc');
    expect(admin.id).toBe('adm_1');
  });
});

describe('slice 2 helpers', () => {
  it('keeps the whole refusal body: the impact of a 409 impact_changed (BR-214)', async () => {
    const refusal = { code: 'impact_changed', message: 'changed', impact: { digest: 'd2' } };
    const client = new Client(fake(409, refusal, []));
    const error = (await client.call('POST', 'nodes/delete', {}).catch((e: unknown) => e)) as ApiError;
    expect(error.code).toBe('impact_changed');
    expect(error.body.impact).toEqual({ digest: 'd2' });
  });

  it('builds a query from the filled values only', () => {
    expect(query({ act: 'tree.delete', path: 'a/b', before: null, actor: '' })).toBe(
      '?act=tree.delete&path=a%2Fb',
    );
    expect(query({})).toBe('');
  });

  it('downloads the export as a file, a write with its CSRF token (BR-218)', async () => {
    const seen: Seen[] = [];
    const lines = '{"seq":2}\n{"seq":1}\n';
    const client = new Client(async (url, init = {}) => {
      seen.push({ url, init });
      return new Response(lines, { status: 200 });
    });
    client.csrf = 'tok';
    const blob = await client.download('audit/export', { action: 'tree.' }, 'application/x-ndjson');
    expect(await blob.text()).toBe(lines);
    expect(blob.type).toBe('application/x-ndjson');
    expect(seen[0]!.init.method).toBe('POST');
    expect(seen[0]!.init.body).toBe('{"action":"tree."}');
    expect(header(seen[0]!, 'X-CSRF-Token')).toBe('tok');
    expect(header(seen[0]!, 'X-Hullgate-Activity')).toBe('1');
  });

  it("reads BR-215's notice from a write's answer", () => {
    expect(toolsNotice({ tools_notice: true })).toBe(true);
    expect(toolsNotice({ tools_notice: 'yes' })).toBe(false);
    expect(toolsNotice(null)).toBe(false);
  });
});
