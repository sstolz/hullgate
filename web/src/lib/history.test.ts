import { describe, expect, it } from 'vitest';
import { ApiError, Client } from './api';
import { currentExport, importBody, refusalState, undoBody, type Change, type ImportPlan } from './history';

describe('history and import server decisions (BR-211)', () => {
  it('returns the exact quoted impact and entry, without constructing an inverse', () => {
    const change = { seq: 41, impact: { digest: 'server-impact' } } as Change;
    expect(undoBody(change, true)).toEqual({ seq: 41, impact: 'server-impact', confirm_share: true });
  });
  it('quotes the reviewed bytes and digest; deletions require the chosen flag', () => {
    const plan = { digest: 'server-state', changes: [] } as unknown as ImportPlan;
    expect(importBody('format: 5\n', plan, false, false)).toEqual({ document: 'format: 5\n', digest: 'server-state', apply_deletions: false, confirm_share: false });
    expect(importBody('format: 5\n', plan, true, true)).toMatchObject({ apply_deletions: true, confirm_share: true });
  });
  it('shows stale states only on the corresponding server refusal', () => {
    expect(refusalState(new ApiError(409, 'undo_stale', 'moved'), 'undo')).toBe('SCR-11.undo-stale');
    expect(refusalState(new ApiError(409, 'import_stale', 'moved'), 'import')).toBe('SCR-12.stale');
    expect(refusalState(new ApiError(422, 'import_invalid', 'location'), 'import')).toBe('SCR-12.refused');
    expect(refusalState(new ApiError(409, 'share_confirmation_required', 'share'), 'undo')).toBe('SCR-11.undo-confirm');
  });
  it('downloads the export as an authenticated read, preserving server bytes', async () => {
    const client = new Client(async (path, init) => {
      expect(path).toBe('/admin/api/v1/export');
      expect(init?.method).toBe('GET');
      expect(init?.credentials).toBe('same-origin');
      expect(init?.cache).toBe('no-store');
      return new Response('format: 5\n');
    });
    expect(await (await currentExport(client)).text()).toBe('format: 5\n');
  });
});
