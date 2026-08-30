import { isTimestampFresh, proofDigest, verifyProofOfWork } from "./pow";
import { validateErrorReport, validateTelemetryEvent, validateUsageEvent } from "./validate";
import { writeEventOnce, type ServiceAccount } from "./rtdb";

export interface Env {
  POW_DIFFICULTY_PREFIX_LENGTH: string;
  POW_MAX_SKEW_MS: string;
  RTDB_DATABASE_URL: string;
  FIREBASE_SERVICE_ACCOUNT_JSON: string;
}

interface TelemetryRequestBody {
  event_json: string;
  timestamp: number;
  nonce: number;
}

const MAX_REQUEST_BYTES = 64 * 1024;
const MAX_EVENT_JSON_BYTES = 32 * 1024;

function json(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function boundedIntegerOrDefault(
  raw: string,
  fallback: number,
  minimum: number,
  maximum: number,
): number | null {
  const value = Number(raw);
  if (!Number.isFinite(value)) return fallback;
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    return null;
  }
  return value;
}

function isRequestBody(value: unknown): value is TelemetryRequestBody {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return (
    Object.keys(v).length === 3 &&
    typeof v.event_json === "string" &&
    typeof v.timestamp === "number" &&
    Number.isSafeInteger(v.timestamp) &&
    typeof v.nonce === "number" &&
    Number.isSafeInteger(v.nonce) &&
    v.nonce >= 0
  );
}

async function readBoundedBody(request: Request): Promise<string | null> {
  if (request.body === null) return "";
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    if (!value) continue;
    total += value.byteLength;
    if (total > MAX_REQUEST_BYTES) {
      try {
        await reader.cancel();
      } catch {
        // The size decision is already final; stream cancellation is cleanup.
      }
      return null;
    }
    chunks.push(value);
  }
  const body = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder().decode(body);
}

// FIREBASE_SERVICE_ACCOUNT_JSON is a static deploy-time secret - reparsing it
// on every request is wasted work, so cache the parsed result keyed on the
// raw string (cheap to compare, and correct if the secret is ever rotated).
let cachedServiceAccount: { raw: string; parsed: ServiceAccount } | null = null;

function loadServiceAccount(raw: string): ServiceAccount {
  if (cachedServiceAccount && cachedServiceAccount.raw === raw) {
    return cachedServiceAccount.parsed;
  }
  const parsed = JSON.parse(raw) as unknown;
  if (
    typeof parsed !== "object" ||
    parsed === null ||
    typeof (parsed as Record<string, unknown>).client_email !== "string" ||
    !(parsed as Record<string, unknown>).client_email ||
    typeof (parsed as Record<string, unknown>).private_key !== "string" ||
    !(parsed as Record<string, unknown>).private_key
  ) {
    throw new Error("missing fields");
  }
  const serviceAccount = parsed as ServiceAccount;
  cachedServiceAccount = { raw, parsed: serviceAccount };
  return serviceAccount;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const node = url.pathname === "/telemetry"
      ? "telemetry"
      : url.pathname === "/error-report"
        ? "error_reports"
        : url.pathname === "/usage"
          ? "usage"
          : null;
    if (request.method !== "POST" || node === null) {
      return json({ error: "not found" }, 404);
    }

    const declaredLength = Number(request.headers.get("content-length"));
    if (Number.isFinite(declaredLength) && declaredLength > MAX_REQUEST_BYTES) {
      return json({ error: "request body too large" }, 413);
    }

    let body: unknown;
    try {
      const bodyText = await readBoundedBody(request);
      if (bodyText === null) {
        return json({ error: "request body too large" }, 413);
      }
      body = JSON.parse(bodyText);
    } catch {
      return json({ error: "invalid json body" }, 400);
    }
    if (!isRequestBody(body)) {
      return json({ error: "expected {event_json, timestamp, nonce}" }, 400);
    }
    const { event_json: eventJson, timestamp, nonce } = body;
    if (new TextEncoder().encode(eventJson).byteLength > MAX_EVENT_JSON_BYTES) {
      return json({ error: "event_json too large" }, 413);
    }

    const maxSkewMs = boundedIntegerOrDefault(env.POW_MAX_SKEW_MS, 300000, 0, 86_400_000);
    const difficulty = boundedIntegerOrDefault(env.POW_DIFFICULTY_PREFIX_LENGTH, 5, 0, 64);
    if (maxSkewMs === null || difficulty === null) {
      return json({ error: "server misconfigured" }, 500);
    }
    if (!isTimestampFresh(timestamp, maxSkewMs)) {
      return json({ error: "stale or future timestamp" }, 400);
    }

    const powOk = await verifyProofOfWork(eventJson, timestamp, nonce, difficulty);
    if (!powOk) {
      return json({ error: "proof of work invalid" }, 400);
    }

    let event: unknown;
    try {
      event = JSON.parse(eventJson);
    } catch {
      return json({ error: "event_json is not valid JSON" }, 400);
    }
    if (typeof event !== "object" || event === null || Array.isArray(event)) {
      return json({ error: "event must be a JSON object" }, 400);
    }

    const result = node === "telemetry"
      ? validateTelemetryEvent(event as Record<string, unknown>)
      : node === "error_reports"
        ? validateErrorReport(event as Record<string, unknown>)
        : validateUsageEvent(event as Record<string, unknown>);
    if (!result.valid) {
      return json({ error: result.reason ?? "invalid event" }, 400);
    }

    let serviceAccount: ServiceAccount;
    try {
      serviceAccount = loadServiceAccount(env.FIREBASE_SERVICE_ACCOUNT_JSON);
    } catch {
      return json({ error: "server misconfigured" }, 500);
    }

    try {
      const eventId = await proofDigest(eventJson, timestamp, nonce);
      const write = await writeEventOnce(
        serviceAccount, env.RTDB_DATABASE_URL, node, eventId,
        event as Record<string, unknown>,
      );
      if (write.status === 412) {
        return json({ error: "proof already used" }, 409);
      }
      if (!write.ok) {
        return json({ error: "upstream write failed" }, 502);
      }
      return json({ ok: true, id: eventId }, 200);
    } catch {
      return json({ error: "upstream write failed" }, 502);
    }
  },
};
