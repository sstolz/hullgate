// WebAuthn glue (BR-202): the server sends options as JSON with base64url strings; the browser
// wants ArrayBuffers, and sends back a credential the server reads as JSON again.

export function toBase64url(data: ArrayBuffer | Uint8Array): string {
  const bytes = data instanceof Uint8Array ? data : new Uint8Array(data);
  let binary = '';
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export function fromBase64url(text: string): ArrayBuffer {
  const padded = text.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (text.length % 4)) % 4);
  const binary = atob(padded);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i);
  return out.buffer;
}

export function supported(): boolean {
  return typeof window !== 'undefined' && typeof window.PublicKeyCredential === 'function';
}

interface DescriptorJSON {
  type: 'public-key';
  id: string;
}

export interface CreationOptionsJSON {
  challenge: string;
  rp: { id: string; name: string };
  user: { id: string; name: string; displayName: string };
  pubKeyCredParams: { type: 'public-key'; alg: number }[];
  timeout?: number;
  attestation?: AttestationConveyancePreference;
  authenticatorSelection?: AuthenticatorSelectionCriteria;
  excludeCredentials?: DescriptorJSON[];
}

export interface RequestOptionsJSON {
  challenge: string;
  rpId: string;
  timeout?: number;
  userVerification?: UserVerificationRequirement;
  allowCredentials?: DescriptorJSON[];
}

const descriptor = (d: DescriptorJSON): PublicKeyCredentialDescriptor => ({
  type: d.type,
  id: fromBase64url(d.id),
});

export function creationOptions(json: CreationOptionsJSON): PublicKeyCredentialCreationOptions {
  return {
    ...json,
    challenge: fromBase64url(json.challenge),
    user: { ...json.user, id: fromBase64url(json.user.id) },
    excludeCredentials: (json.excludeCredentials ?? []).map(descriptor),
  };
}

export function requestOptions(json: RequestOptionsJSON): PublicKeyCredentialRequestOptions {
  return {
    ...json,
    challenge: fromBase64url(json.challenge),
    allowCredentials: (json.allowCredentials ?? []).map(descriptor),
  };
}

type Json = Record<string, unknown>;

export function registrationJSON(credential: PublicKeyCredential): Json {
  const response = credential.response as AuthenticatorAttestationResponse;
  return {
    id: credential.id,
    rawId: toBase64url(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: toBase64url(response.clientDataJSON),
      attestationObject: toBase64url(response.attestationObject),
      transports: typeof response.getTransports === 'function' ? response.getTransports() : [],
    },
    clientExtensionResults: {},
  };
}

export function assertionJSON(credential: PublicKeyCredential): Json {
  const response = credential.response as AuthenticatorAssertionResponse;
  return {
    id: credential.id,
    rawId: toBase64url(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: toBase64url(response.clientDataJSON),
      authenticatorData: toBase64url(response.authenticatorData),
      signature: toBase64url(response.signature),
      userHandle: response.userHandle ? toBase64url(response.userHandle) : null,
    },
    clientExtensionResults: {},
  };
}

export async function create(json: CreationOptionsJSON): Promise<Json> {
  const credential = await navigator.credentials.create({ publicKey: creationOptions(json) });
  if (!(credential instanceof PublicKeyCredential)) throw new Error('no credential');
  return registrationJSON(credential);
}

export async function get(json: RequestOptionsJSON): Promise<Json> {
  const credential = await navigator.credentials.get({ publicKey: requestOptions(json) });
  if (!(credential instanceof PublicKeyCredential)) throw new Error('no credential');
  return assertionJSON(credential);
}
