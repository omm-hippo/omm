/**
 * Payload-bound Hashcash-style proof of work.
 *
 * The client finds a `nonce` such that
 *   SHA256(`${eventJson}:${timestamp}:${nonce}`)
 * starts with `difficultyPrefixLength` hex zero characters, then sends
 * `eventJson` (the exact string it hashed - not a re-serialization of the
 * parsed object, so the two sides never need to agree on canonical JSON
 * formatting), `timestamp`, and `nonce`. Binding the puzzle to the payload
 * and a fresh timestamp defeats precomputation: a solution for one payload
 * is useless for another, and a solution older than `maxSkewMs` is rejected
 * regardless of validity, so a bank of pre-solved puzzles can't be replayed
 * indefinitely. See omm-hippo/omm#133 for the full design rationale.
 */

export function isTimestampFresh(
  timestamp: number,
  maxSkewMs: number,
  now: number = Date.now(),
): boolean {
  if (!Number.isFinite(timestamp)) return false;
  return Math.abs(now - timestamp) <= maxSkewMs;
}

function toHex(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let hex = "";
  for (const byte of bytes) {
    hex += byte.toString(16).padStart(2, "0");
  }
  return hex;
}

export async function sha256Hex(input: string): Promise<string> {
  const data = new TextEncoder().encode(input);
  const digest = await crypto.subtle.digest("SHA-256", data);
  return toHex(digest);
}

export async function verifyProofOfWork(
  eventJson: string,
  timestamp: number,
  nonce: number,
  difficultyPrefixLength: number,
): Promise<boolean> {
  if (!Number.isInteger(nonce) || nonce < 0) return false;
  const hash = await sha256Hex(`${eventJson}:${timestamp}:${nonce}`);
  return hash.startsWith("0".repeat(difficultyPrefixLength));
}

export async function proofDigest(
  eventJson: string,
  timestamp: number,
  nonce: number,
): Promise<string> {
  return sha256Hex(`${eventJson}:${timestamp}:${nonce}`);
}
