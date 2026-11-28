import { ApiError, type AuditEntry, type Client } from './api';

export interface Change extends AuditEntry {
  undoable: boolean;
  undo_reason: string | null;
  stale: boolean;
  current: unknown;
  expected: unknown;
  impact: { values: unknown; digest: string; notice: string } | null;
}
export interface ImportChange {
  kind: string;
  action: string;
  path: string;
  before: unknown;
  after: unknown;
  impact: Record<string, unknown>;
}
export interface ImportPlan {
  digest: string;
  changes: ImportChange[];
  reauthorize: number;
  apply_deletions: boolean;
}
export function undoBody(change: Change, confirmShare: boolean): Record<string, unknown> {
  return { seq: change.seq, impact: change.impact?.digest, confirm_share: confirmShare };
}
export function importBody(document: string, plan: ImportPlan, deletions: boolean, shares: boolean): Record<string, unknown> {
  return { document, digest: plan.digest, apply_deletions: deletions, confirm_share: shares };
}
export function refusalState(error: unknown, kind: 'undo' | 'import'): string {
  return error instanceof ApiError && error.code === `${kind}_stale`
    ? (kind === 'undo' ? 'SCR-11.undo-stale' : 'SCR-12.stale')
    : (kind === 'undo' ? 'SCR-11.undo-confirm' : 'SCR-12.refused');
}
export async function currentExport(client: Client): Promise<Blob> {
  return client.download('export', undefined, 'application/yaml', 'GET');
}
