import { describe, expect, it } from 'vitest';
import type { Client } from './api';
import {
  changedConnections,
  entryName,
  fieldText,
  groupedPairs,
  modeChoice,
  parseValues,
  partition,
  queueCount,
  rightsState,
  ruleName,
  summary,
  upstreamNames,
  validationBody,
  type Change,
  type PlacementPolicy,
  type Rights,
  type RightsRow,
} from './policy';

function row(over: Partial<RightsRow>): RightsRow {
  return {
    public_name: 'tr__list_issues',
    connection: 'acme/tracker',
    upstream_name: 'list_issues',
    class: 'read',
    source: 'annotations',
    exposed: true,
    reasons: [],
    opened: false,
    grouped: {},
    argument_rule: null,
    pinned: false,
    sensitive_via: [],
    ...over,
  };
}

function rights(over: Partial<Rights>): Rights {
  return { role: 'acme/web/dev/ops', token: null, exposed: [], tools: [], unavailable: [], policy_errors: [], ...over };
}

const ROWS = [
  row({}),
  row({ public_name: 'tr__delete_repo', upstream_name: 'delete_repo', class: 'destructive', exposed: false, reasons: ['above_mode'] }),
  row({ public_name: 'tr__create_issue', upstream_name: 'create_issue', class: 'write' }),
  row({ public_name: 'ot__x', connection: 'acme/other', upstream_name: 'x', exposed: false, reasons: ['denied'] }),
];

describe('rights preview (SCR-08, BR-217): the server decides, the console sorts', () => {
  it('reads the visual state from the answer', () => {
    expect(rightsState(rights({ tools: ROWS, exposed: ['tr__list_issues'] }))).toBe('SCR-08');
    expect(rightsState(rights({}))).toBe('SCR-08.empty');
    expect(rightsState(rights({ unavailable: [{ connection: 'acme/down', code: 'upstream_unavailable' }] }))).toBe(
      'SCR-08.unknown',
    );
    const out = { token: 't1', type: 'operator', role: 'r', reaches: false, step: 6, reason: 'out_of_scope' };
    expect(rightsState(rights({ token: out, tools: ROWS }))).toBe('SCR-08.out-of-scope');
    expect(rightsState(rights({ token: { ...out, reaches: true, reason: null } }))).toBe('SCR-08.empty');
    // a role whose only connection failed evaluation is not "nothing placed"
    expect(rightsState(rights({ policy_errors: ['acme/x'] }))).toBe('SCR-08');
  });

  it("splits on the server's exposed flag, its order kept", () => {
    const { exposed, hidden } = partition(rights({ tools: ROWS }));
    expect(exposed.map((r) => r.upstream_name)).toEqual(['list_issues', 'create_issue']);
    expect(hidden.map((r) => r.upstream_name)).toEqual(['delete_repo', 'x']);
  });

  it("counts the server's exposed names, hidden rows and unknown connections", () => {
    const r = rights({
      tools: ROWS,
      exposed: ['tr__list_issues', 'tr__create_issue'],
      unavailable: [{ connection: 'acme/down', code: 'upstream_unavailable' }],
    });
    expect(summary(r)).toEqual({ exposed: 2, hidden: 2, unknown: 1 });
  });

  it('suggests the upstream names of one connection, once each, sorted', () => {
    const r = rights({ tools: [...ROWS, row({})] });
    expect(upstreamNames(r, 'acme/tracker')).toEqual(['create_issue', 'delete_repo', 'list_issues']);
    expect(upstreamNames(null, 'acme/tracker')).toEqual([]);
  });

  it('shows grouped values as given', () => {
    expect(groupedPairs({ method: ['get', 'list'] })).toEqual([['method', 'get, list']]);
  });
});

describe('placement policy', () => {
  const base: PlacementPolicy = {
    connection: 'acme/tracker',
    role: 'acme/web/dev/ops',
    mode: 'write',
    mode_origin: '/',
    pin: 'inherit',
    sensitive: false,
    open: [],
    deny: [],
    allowlist: [],
    pinned_tools: [],
    arguments: {},
  };

  it("shows the placement's own mode, or inherit when a node carries it", () => {
    expect(modeChoice(base)).toBe('inherit');
    expect(modeChoice({ ...base, mode: 'read', mode_origin: 'placement' })).toBe('read');
  });

  it('reads typed values, comma or newline separated', () => {
    expect(parseValues(' get, list ,\nsearch,,')).toEqual(['get', 'list', 'search']);
    expect(parseValues('  ')).toEqual([]);
  });

  it('acts on an entry by its upstream name, not its label (review 579 N1)', () => {
    const p: PlacementPolicy = {
      ...base,
      deny: ['a', 'we_ird'],
      pinned_tools: ['we_ird'],
      arguments: { we_ird: { argument: 'm', values: ['x'] } },
      names: { deny: ['a', 'we\u001bird'], pinned_tools: ['we\u001bird'], arguments: { we_ird: 'we\u001bird' } },
    };
    expect(entryName(p, 'deny', 1)).toBe('we\u001bird');
    expect(entryName(p, 'pinned_tools', 0)).toBe('we\u001bird');
    expect(ruleName(p, 'we_ird')).toBe('we\u001bird');
    // a server that sends no names: the label is all there is
    expect(entryName({ ...base, deny: ['a'] }, 'deny', 0)).toBe('a');
    expect(ruleName(base, 'x')).toBe('x');
  });
});

describe('pinned changes (SCR-10.diff)', () => {
  const change = (connection: string, tool: string): Change => ({
    connection,
    role: 'acme/web/dev/ops',
    tool,
    state: 'suspended',
    baseline_sha256: 'a',
    current_sha256: 'b',
    fields: [],
  });

  it('offers one validation per connection, in the server order', () => {
    expect(changedConnections([change('b/x', 't1'), change('a/y', 't2'), change('b/x', 't3')])).toEqual([
      'b/x',
      'a/y',
    ]);
  });

  it("validates a row on the row's own role, the connection on the node read (review 579 B1)", () => {
    const row = { ...change('acme/tracker', 'create_issue'), role: 'acme/web/dev/tools' };
    expect(validationBody('acme/web/dev', 'acme/tracker', row)).toEqual({
      path: 'acme/web/dev/tools',
      connection: 'acme/tracker',
      tool: 'create_issue',
    });
    expect(validationBody('acme/web/dev', 'acme/tracker', null)).toEqual({
      path: 'acme/web/dev',
      connection: 'acme/tracker',
    });
  });

  it('renders a field value as text, never markup', () => {
    expect(fieldText('<img src=x onerror=alert(1)>')).toBe('<img src=x onerror=alert(1)>');
    expect(fieldText({ type: 'object' })).toBe('{\n  "type": "object"\n}');
    expect(fieldText(null)).toBe('—');
  });
});

describe('Tools badge (§3.4, BR-204)', () => {
  it('counts the queue on a background read, without the activity header', async () => {
    const calls: { path: string; activity: boolean | undefined }[] = [];
    const client = {
      call: (_method: string, path: string, _body: unknown, opts: { activity?: boolean } = {}) => {
        calls.push({ path, activity: opts.activity });
        return Promise.resolve({ queue: [{}, {}, {}] });
      },
    } as unknown as Client;
    expect(await queueCount(client)).toBe(3);
    expect(calls).toEqual([{ path: 'tools/queue', activity: false }]);
  });
});
