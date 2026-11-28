// SCR-11's filter (BR-218): the form's fields become the query the server filters on. `since` and
// `until` are typed in local time and sent in UTC ISO 8601; `path` and `action` are prefixes the
// server matches. The same object is the export's body, so the export is the current filter.

export interface AuditForm {
  since: string;
  until: string;
  actor: string;
  path: string;
  action: string;
  result: string;
}

/** A `datetime-local` value as UTC ISO 8601 (`2026-11-14T08:30:00Z`), or '' when unreadable. */
export function utc(local: string): string {
  if (!local) return '';
  const at = new Date(local);
  return Number.isNaN(at.getTime()) ? '' : at.toISOString().replace(/\.\d{3}Z$/, 'Z');
}

export function auditFilter(form: AuditForm): Record<string, string> {
  const out: Record<string, string> = {};
  const put = (key: string, value: string): void => {
    if (value) out[key] = value;
  };
  put('since', utc(form.since));
  put('until', utc(form.until));
  put('actor', form.actor.trim());
  put('path', form.path.trim());
  put('action', form.action.trim());
  put('result', form.result.trim());
  return out;
}

export function exportName(now: Date): string {
  return `hullgate-audit-${now.toISOString().replace(/[:]/g, '').replace(/\.\d{3}Z$/, 'Z')}.jsonl`;
}
