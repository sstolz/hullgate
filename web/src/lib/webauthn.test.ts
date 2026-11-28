import { describe, expect, it } from 'vitest';
import { creationOptions, fromBase64url, requestOptions, toBase64url } from './webauthn';

describe('base64url', () => {
  it('round-trips every byte value without padding', () => {
    const bytes = new Uint8Array(256).map((_, i) => i);
    const text = toBase64url(bytes);
    expect(text).not.toMatch(/[+/=]/);
    expect(new Uint8Array(fromBase64url(text))).toEqual(bytes);
  });

  it('decodes the server form of a 32-byte challenge', () => {
    expect(fromBase64url('A'.repeat(43)).byteLength).toBe(32);
  });
});

describe('options', () => {
  it('turns the request options into buffers and keeps user verification required', () => {
    const options = requestOptions({
      challenge: toBase64url(new Uint8Array([1, 2, 3])),
      rpId: 'localhost',
      userVerification: 'required',
      allowCredentials: [{ type: 'public-key', id: toBase64url(new Uint8Array([9])) }],
    });
    expect(new Uint8Array(options.challenge as ArrayBuffer)).toEqual(new Uint8Array([1, 2, 3]));
    expect(options.userVerification).toBe('required');
    expect(options.allowCredentials?.[0]?.type).toBe('public-key');
  });

  it('turns the creation options user id into a buffer', () => {
    const options = creationOptions({
      challenge: 'AQID',
      rp: { id: 'localhost', name: 'hullgate' },
      user: { id: toBase64url(new TextEncoder().encode('adm_1')), name: 'a@x', displayName: 'a' },
      pubKeyCredParams: [{ type: 'public-key', alg: -7 }],
    });
    expect(new TextDecoder().decode(options.user.id as ArrayBuffer)).toBe('adm_1');
    expect(options.excludeCredentials).toEqual([]);
  });
});
