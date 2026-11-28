import { describe, expect, it } from 'vitest';
import { auditFilter, exportName, utc } from './audit';

describe('audit filter (BR-218)', () => {
  it('sends only the fields filled, trimmed, times in UTC ISO 8601', () => {
    const filter = auditFilter({
      since: '2026-11-14T08:30',
      until: '',
      actor: ' admin:adm_1 ',
      path: 'acme/',
      action: 'tree.',
      result: '',
    });
    expect(Object.keys(filter)).toEqual(['since', 'actor', 'path', 'action']);
    expect(filter.since).toMatch(/^2026-11-1[34]T\d\d:\d\d:00Z$/);
    expect(filter.actor).toBe('admin:adm_1');
  });

  it('drops a time it cannot read', () => {
    expect(utc('not a date')).toBe('');
    expect(utc('')).toBe('');
  });

  it('names the export as JSON Lines', () => {
    expect(exportName(new Date('2026-11-14T08:30:05.123Z'))).toBe(
      'hullgate-audit-2026-11-14T083005Z.jsonl',
    );
  });
});
