// The admin API client (§3.15). The console decides nothing (BR-211): it shows the server's values
// and refusal texts. Writes carry the session's CSRF token (BR-205); calls the operator caused carry
// `X-Hullgate-Activity: 1`, background polls do not (BR-204).

export const API = '/admin/api/v1';

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    /** The whole refusal body: some refusals carry more than the code and message (BR-214). */
    readonly body: Record<string, unknown> = {},
  ) {
    super(message);
  }
}

export interface Administrator {
  id: string;
  email: string;
  name: string;
  state: 'pending' | 'active';
}

export interface SignedIn {
  administrator: Administrator;
  csrf_token: string;
}

export interface Meta {
  organisation_name?: string;
  version: string;
  public_url: string;
  admin_url: string;
  readiness: { ok: boolean; reason: string | null };
  features?: { web_return?: boolean };
  web_redirect_uri?: string;
}

export interface Passkey {
  id: string;
  label: string;
  created_at: string;
  last_used_at: string | null;
}

// --- slice 2 (docs/admin-api.md, "Routes of slice 2") -------------------------------------------

export type Level = 'org' | 'client' | 'project' | 'env' | 'role';

export interface TreeNode {
  id: string;
  path: string;
  parent: string | null;
  level: Level;
  segment: string;
  name: string;
  sensitive: boolean;
  marked: boolean;
  locked_by: string | null;
  locked_here: boolean;
  url: string | null;
}

export interface ShareSignal {
  connection: string;
  reasons: string[];
  roles: { role: string; sensitive: boolean }[];
}

export interface ImpactRead {
  act: string;
  target: unknown;
  impact: Record<string, unknown>;
  shares: ShareSignal[];
  digest: string;
}

export interface Setting {
  key: string;
  value: string;
  origin: string;
  here: boolean;
}

export interface Token {
  id: string;
  type: 'operator' | 'oauth';
  holder: string;
  path: string;
  issued_at: string;
  expires_at: string;
  state: string;
}

export interface Issued {
  id: string;
  value: string;
  path: string;
  label: string;
  expires_at: string;
  capped: boolean;
  uncovered_sensitive: string[];
}

export interface OAuthClient {
  client_id: string;
  kind: string;
  path: string;
  redirect_uris: string[];
  name: string | null;
  state: string;
}

export interface Service {
  id: string;
  model: string;
  url: string;
  scopes: string | string[];
  internal_host: boolean;
  endpoints: unknown;
  resource: string | null;
  upstream: string | null;
}

export interface Application {
  id: string;
  service: string;
  mode: string;
  client_id: string | null;
  redirect_uri: string | null;
  secret: string | boolean | null;
  secret_expires_at: string | null;
  method: string | null;
  web_return: boolean;
}

export interface Connection {
  expiring?: boolean;
  access_ends_at?: string | null;
  name: string;
  service: string;
  type: string;
  application: string | null;
  prefix: string;
  account: string;
  scopes: string[];
  state: string;
  stored_state: string;
  key: string | null;
  expires_at: string | null;
  expiry_estimated: boolean;
  not_before: string | null;
  last_outcome: string | null;
  placements: { role: string; sensitive: boolean }[];
  application_to_redeclare: string | null;
  upstream: string;
  refusal: string | null;
  web_return?: boolean;
}

export interface Opened {
  name: string;
  type?: string;
  state: string;
  authorization_url: string | null;
  redirect: 'web' | 'paste' | null;
}

export interface Authorized {
  name: string;
  account: string;
  scopes: string[] | string;
  expires_at: string | null;
}

export interface Diagnosis {
  connection: string;
  role: string;
  version: unknown;
  tools: { name: string; upstream_name: string }[];
  invalid_names: string[];
  purged_names: string[];
}

export interface AuditEntry {
  undoable?: boolean;
  undo_reason?: string | null;
  seq: number;
  at: string;
  actor: string;
  node_id: string | null;
  path: string | null;
  action: string;
  result: string;
  reason: string | null;
  before: unknown;
  after: unknown;
  detail: unknown;
}

export type Fetch = (input: string, init?: RequestInit) => Promise<Response>;

export interface CallOptions {
  activity?: boolean;
}

/** A query string from the non-empty values of `params`, `?` included (or empty). */
export function query(params: Record<string, string | number | boolean | null | undefined>): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') q.set(k, String(v));
  }
  const text = q.toString();
  return text ? `?${text}` : '';
}

export class Client {
  csrf = '';
  onUnauthenticated: () => void = () => {};

  constructor(private readonly fetcher: Fetch = (i, init) => fetch(i, init)) {}

  private async send(
    method: string,
    path: string,
    body: unknown,
    opts: CallOptions,
    accept: string,
  ): Promise<Response> {
    const headers: Record<string, string> = { Accept: accept };
    if (opts.activity ?? true) headers['X-Hullgate-Activity'] = '1';
    if (method !== 'GET') {
      headers['Content-Type'] = 'application/json';
      if (this.csrf) headers['X-CSRF-Token'] = this.csrf;
    }
    const response = await this.fetcher(`${API}/${path}`, {
      method,
      headers,
      body: method === 'GET' ? undefined : JSON.stringify(body ?? {}),
      credentials: 'same-origin',
      cache: 'no-store',
    });
    if (!response.ok) {
      const data: unknown = await response.json().catch(() => ({}));
      const refusal = (data ?? {}) as { code?: string; message?: string };
      if (response.status === 401 && path !== 'auth/assert') this.onUnauthenticated();
      throw new ApiError(
        response.status,
        refusal.code ?? 'error',
        refusal.message ?? `HTTP ${response.status}`,
        typeof data === 'object' && data !== null ? (data as Record<string, unknown>) : {},
      );
    }
    return response;
  }

  async call<T>(method: string, path: string, body?: unknown, opts: CallOptions = {}): Promise<T> {
    const response = await this.send(method, path, body, opts, 'application/json');
    const data: unknown = await response.json().catch(() => ({}));
    return data as T;
  }

  /** A write whose answer is a file (the audit export, BR-218): returned as a Blob. */
  async download(path: string, body: unknown, type: string, method = 'POST'): Promise<Blob> {
    const response = await this.send(method, path, body, {}, type);
    return new Blob([await response.arrayBuffer()], { type });
  }

  signedIn(result: SignedIn): Administrator {
    this.csrf = result.csrf_token;
    return result.administrator;
  }
}

/** The text to show for a failed call: the server's refusal as it is (BR-211). */
export function message(error: unknown): string {
  return error instanceof ApiError ? error.message : String(error);
}

/** BR-215: a write's answer says whether connected clients see a new list at their next call. */
export function toolsNotice(answer: unknown): boolean {
  return (
    typeof answer === 'object' &&
    answer !== null &&
    (answer as { tools_notice?: unknown }).tools_notice === true
  );
}
