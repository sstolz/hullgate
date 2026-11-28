// The policy surfaces of slice 3 (docs/admin-api.md, "Routes of slice 3"): the rights preview
// (BR-217), the policy of a placement and the tool catalogue, all F-015's operations. The console
// decides nothing (BR-211): these helpers sort and count what the server sent, and never derive a
// right, a class or a verdict of their own.

import type { Client } from './api';

export type ToolClass = 'read' | 'write' | 'destructive';
export const CLASSES: readonly ToolClass[] = ['read', 'write', 'destructive'];
export const MODES = ['read', 'write', 'destructive', 'inherit'] as const;
export const PINS = ['on', 'off', 'inherit'] as const;
export const LISTS = ['open', 'deny', 'allowlist'] as const;
export type ListName = (typeof LISTS)[number];

/** A row of `policy preview --json` (F-015 §3.5), as `POST rights` returns it. */
export interface RightsRow {
  public_name: string;
  connection: string;
  upstream_name: string;
  class: string;
  source: string;
  exposed: boolean;
  reasons: string[];
  opened: boolean;
  grouped: Record<string, string[]>;
  argument_rule: { argument: string; values: string[] } | null;
  pinned: boolean;
  sensitive_via: string[];
}

/** Steps 4 to 7 of the authorization order played for a token on a role (BR-055, BR-051). */
export interface Reach {
  token: string;
  type: string;
  role: string;
  reaches: boolean;
  step: number | string | null;
  reason: string | null;
}

export interface Rights {
  role: string;
  token: Reach | null;
  exposed: string[];
  tools: RightsRow[];
  unavailable: { connection: string; code: string }[];
  policy_errors: string[];
}

/** `policy show --json`'s object, the answer of every placement policy write. */
/** The lists of the policy object whose entries are labels of upstream names. */
export type NamedList = 'open' | 'deny' | 'allowlist' | 'pinned_tools';

export interface PlacementPolicy {
  connection: string;
  role: string;
  mode: string;
  mode_origin: string;
  pin: string;
  sensitive: boolean;
  open: string[];
  deny: string[];
  allowlist: string[];
  pinned_tools: string[];
  arguments: Record<string, { argument: string; values: string[] }>;
  /** the upstream names behind the labels above, in their order; argument rules by label */
  names?: Partial<Record<NamedList, string[]>> & { arguments?: Record<string, string> };
  tools_notice?: boolean;
}

export interface Queued {
  service: string;
  name: string;
  class: string;
  source: string;
  proposal: string | null;
  removed: boolean;
}

export interface Classified {
  service: string;
  tool: string;
  class: string;
  source: string;
}

export interface Change {
  connection: string;
  role: string;
  tool: string;
  state: string;
  baseline_sha256: string | null;
  current_sha256: string | null;
  fields: { field: string; old: unknown; new: unknown; type_marks?: { path: string; old: string; new: string }[] }[];
}

export interface Validated {
  connection: string;
  role: string;
  tool: string;
  before_sha256: string | null;
  after_sha256: string | null;
}

export type RightsState = 'SCR-08' | 'SCR-08.empty' | 'SCR-08.unknown' | 'SCR-08.out-of-scope';

/**
 * The visual state of a preview, read from the server's answer: a token that does not reach the
 * role first; then connections whose tools are unknown (BR-217: never shown as absent); then a
 * role with nothing placed (no row and nothing unavailable).
 */
export function rightsState(r: Rights): RightsState {
  if (r.token && !r.token.reaches) return 'SCR-08.out-of-scope';
  if (r.unavailable.length > 0) return 'SCR-08.unknown';
  if (r.tools.length === 0 && r.policy_errors.length === 0) return 'SCR-08.empty';
  return 'SCR-08';
}

/** The server's rows split on its own `exposed` flag, its order kept (exposed first). */
export function partition(r: Rights): { exposed: RightsRow[]; hidden: RightsRow[] } {
  return {
    exposed: r.tools.filter((row) => row.exposed),
    hidden: r.tools.filter((row) => !row.exposed),
  };
}

/** The counts of the inspector's summary: the server's exposed names, its hidden rows, and the
 * connections it could not list. */
export function summary(r: Rights): { exposed: number; hidden: number; unknown: number } {
  return {
    exposed: r.exposed.length,
    hidden: r.tools.filter((row) => !row.exposed).length,
    unknown: r.unavailable.length,
  };
}

/** The upstream names the preview listed for one connection, sorted, for the policy editor's
 * suggestions (a list entry names an upstream tool, BR-176; the server refuses an unknown one). */
export function upstreamNames(r: Rights | null, connection: string): string[] {
  if (!r) return [];
  const names = r.tools.filter((row) => row.connection === connection).map((row) => row.upstream_name);
  return [...new Set(names)].sort();
}

/** `grouped` as `[argument, "v1, v2"]` pairs, in the server's order. */
export function groupedPairs(grouped: Record<string, string[]>): [string, string][] {
  return Object.entries(grouped).map(([argument, values]) => [argument, values.join(', ')]);
}

/** Values typed for an argument rule: comma or newline separated, trimmed, empties dropped. */
export function parseValues(text: string): string[] {
  return text
    .split(/[,\n]/)
    .map((v) => v.trim())
    .filter((v) => v !== '');
}

/** The connections a diff names, in the server's order, once each: one "validate" act each. */
export function changedConnections(changes: readonly Change[]): string[] {
  return [...new Set(changes.map((c) => c.connection))];
}

/** The body of `tools/validate` (`tool validate <node> <connection> [--tool]`, BR-182). A row's act
 * validates that row only: its tool on its own role, never the connection's other placements
 * under the node read (review 579 B1). The connection's act validates what the read showed: the
 * connection under that node. */
export function validationBody(
  scope: string,
  connection: string,
  row: Pick<Change, 'role' | 'tool'> | null,
): Record<string, string> {
  return row === null ? { path: scope, connection } : { path: row.role, connection, tool: row.tool };
}

/** The upstream name behind the `i`-th label of a list of the policy object, which an act on that
 * entry sends: a label may differ from its name (review 579 N1). */
export function entryName(p: PlacementPolicy, list: NamedList, i: number): string {
  return p.names?.[list]?.[i] ?? p[list][i] ?? '';
}

/** The upstream name behind an argument rule's label (review 579 N1). */
export function ruleName(p: PlacementPolicy, label: string): string {
  return p.names?.arguments?.[label] ?? label;
}

/** A field value of a diff as text: strings as they are, anything else as JSON. Rendered as text
 * by the screen, never as markup (the upstream's text is data). */
export function fieldText(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'string') return value;
  return JSON.stringify(value, null, 2);
}

/** The mode the editor shows as chosen: the placement's own, or `inherit` when it comes from a
 * node (`mode_origin` is `placement` or the path carrying the setting). */
export function modeChoice(p: PlacementPolicy): string {
  return p.mode_origin === 'placement' ? p.mode : 'inherit';
}

/** The Tools tab's badge: the size of the queue, read without extending the session (BR-204). */
export async function queueCount(client: Client): Promise<number> {
  const read = await client.call<{ queue: Queued[] }>('GET', 'tools/queue', undefined, {
    activity: false,
  });
  return read.queue.length;
}
