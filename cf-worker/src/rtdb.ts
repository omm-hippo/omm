/**
 * Admin (rules-bypassing) write to the telemetry RTDB node, authenticated
 * as the Firebase service account rather than an end user. This is only
 * safe to call after `validateTelemetryEvent` has already accepted the
 * event - see src/validate.ts.
 */

export interface ServiceAccount {
  client_email: string;
  private_key: string;
}

interface CachedToken {
  accessToken: string;
  expiresAtMs: number;
  clientEmail: string;
  privateKey: string;
}

let cachedToken: CachedToken | null = null;
let pendingToken: {
  clientEmail: string;
  privateKey: string;
  promise: Promise<string>;
} | null = null;

function base64UrlEncode(bytes: ArrayBuffer | Uint8Array): string {
  const arr = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  let binary = "";
  for (const byte of arr) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function pemToArrayBuffer(pem: string): ArrayBuffer {
  const base64 = pem
    .replace(/-----BEGIN PRIVATE KEY-----/, "")
    .replace(/-----END PRIVATE KEY-----/, "")
    .replace(/\s+/g, "");
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

async function signJwt(serviceAccount: ServiceAccount): Promise<string> {
  const header = { alg: "RS256", typ: "JWT" };
  const nowSeconds = Math.floor(Date.now() / 1000);
  const claims = {
    iss: serviceAccount.client_email,
    scope:
      "https://www.googleapis.com/auth/firebase.database https://www.googleapis.com/auth/userinfo.email",
    aud: "https://oauth2.googleapis.com/token",
    iat: nowSeconds,
    exp: nowSeconds + 3600,
  };
  const encoder = new TextEncoder();
  const encodedHeader = base64UrlEncode(encoder.encode(JSON.stringify(header)));
  const encodedClaims = base64UrlEncode(encoder.encode(JSON.stringify(claims)));
  const signingInput = `${encodedHeader}.${encodedClaims}`;

  const key = await crypto.subtle.importKey(
    "pkcs8",
    pemToArrayBuffer(serviceAccount.private_key),
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, encoder.encode(signingInput));
  return `${signingInput}.${base64UrlEncode(signature)}`;
}

async function fetchAccessToken(serviceAccount: ServiceAccount): Promise<string> {
  const jwt = await signJwt(serviceAccount);
  const response = await fetch("https://oauth2.googleapis.com/token", {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    // "manual", not "error": the Workers runtime rejects redirect:"error"
    // with a TypeError. "manual" surfaces a 3xx as a non-ok response, which
    // the `!response.ok` check below treats as a failure just the same - we
    // still never follow a redirect off oauth2.googleapis.com.
    redirect: "manual",
    body: new URLSearchParams({
      grant_type: "urn:ietf:params:oauth:grant-type:jwt-bearer",
      assertion: jwt,
    }),
  });
  if (!response.ok) {
    throw new Error(`token exchange failed: ${response.status} ${await response.text()}`);
  }
  const data = (await response.json()) as Record<string, unknown>;
  if (
    typeof data.access_token !== "string" ||
    !data.access_token ||
    typeof data.expires_in !== "number" ||
    !Number.isFinite(data.expires_in) ||
    data.expires_in <= 60
  ) {
    throw new Error("token exchange returned an invalid response");
  }
  cachedToken = {
    accessToken: data.access_token,
    // Refresh a minute early so a request never races an expiry boundary.
    expiresAtMs: Date.now() + (data.expires_in - 60) * 1000,
    clientEmail: serviceAccount.client_email,
    privateKey: serviceAccount.private_key,
  };
  return cachedToken.accessToken;
}

async function getAccessToken(serviceAccount: ServiceAccount): Promise<string> {
  if (
    cachedToken &&
    cachedToken.expiresAtMs > Date.now() &&
    cachedToken.clientEmail === serviceAccount.client_email &&
    cachedToken.privateKey === serviceAccount.private_key
  ) {
    return cachedToken.accessToken;
  }
  if (
    pendingToken &&
    pendingToken.clientEmail === serviceAccount.client_email &&
    pendingToken.privateKey === serviceAccount.private_key
  ) {
    return pendingToken.promise;
  }
  const promise = fetchAccessToken(serviceAccount).finally(() => {
    if (pendingToken?.promise === promise) pendingToken = null;
  });
  pendingToken = {
    clientEmail: serviceAccount.client_email,
    privateKey: serviceAccount.private_key,
    promise,
  };
  return promise;
}

function firebaseDatabaseOrigin(databaseUrl: string): string {
  let url: URL;
  try {
    url = new URL(databaseUrl);
  } catch {
    throw new Error("invalid Firebase database URL");
  }
  const hostname = url.hostname.toLowerCase();
  const isFirebaseHost =
    hostname.endsWith(".firebaseio.com") ||
    hostname.endsWith(".firebasedatabase.app");
  if (
    url.protocol !== "https:" ||
    !isFirebaseHost ||
    url.username ||
    url.password ||
    url.port ||
    (url.pathname !== "/" && url.pathname !== "") ||
    url.search ||
    url.hash
  ) {
    throw new Error("invalid Firebase database URL");
  }
  return url.origin;
}

export async function writeEventOnce(
  serviceAccount: ServiceAccount,
  databaseUrl: string,
  node: "telemetry" | "error_reports" | "usage",
  eventId: string,
  event: Record<string, unknown>,
): Promise<{ ok: boolean; status: number; body: string }> {
  const accessToken = await getAccessToken(serviceAccount);
  return putEventOnce(accessToken, databaseUrl, node, eventId, event);
}

export async function putEventOnce(
  accessToken: string,
  databaseUrl: string,
  node: "telemetry" | "error_reports" | "usage",
  eventId: string,
  event: Record<string, unknown>,
): Promise<{ ok: boolean; status: number; body: string }> {
  if (!/^[0-9a-f]{64}$/.test(eventId)) {
    throw new Error("invalid event id");
  }
  const databaseOrigin = firebaseDatabaseOrigin(databaseUrl);
  const response = await fetch(`${databaseOrigin}/${node}/${eventId}.json`, {
    method: "PUT",
    // "manual", not "error" (the Workers runtime rejects redirect:"error").
    // A 3xx comes back as a non-ok response and is reported as {ok:false}.
    redirect: "manual",
    headers: {
      "content-type": "application/json",
      authorization: `Bearer ${accessToken}`,
      "if-match": "null_etag",
    },
    body: JSON.stringify(event),
  });
  const body = await response.text();
  return { ok: response.ok, status: response.status, body };
}
