import { isTimestampFresh, verifyProofOfWork } from "./pow";
import { validateTelemetryEvent } from "./validate";
import { writeTelemetryEvent, type ServiceAccount } from "./rtdb";

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

function json(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function isRequestBody(value: unknown): value is TelemetryRequestBody {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return typeof v.event_json === "string" && typeof v.timestamp === "number" && typeof v.nonce === "number";
}

// FIREBASE_SERVICE_ACCOUNT_JSON is a static deploy-time secret - reparsing it
// on every request is wasted work, so cache the parsed result keyed on the
// raw string (cheap to compare, and correct if the secret is ever rotated).
let cachedServiceAccount: { raw: string; parsed: ServiceAccount } | null = null;

function loadServiceAccount(raw: string): ServiceAccount {
  if (cachedServiceAccount && cachedServiceAccount.raw === raw) {
    return cachedServiceAccount.parsed;
  }
  const parsed = JSON.parse(raw) as ServiceAccount;
  if (!parsed.client_email || !parsed.private_key) throw new Error("missing fields");
  cachedServiceAccount = { raw, parsed };
  return parsed;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (request.method !== "POST" || url.pathname !== "/telemetry") {
      return json({ error: "not found" }, 404);
    }

    let body: unknown;
    try {
      body = await request.json();
    } catch {
      return json({ error: "invalid json body" }, 400);
    }
    if (!isRequestBody(body)) {
      return json({ error: "expected {event_json, timestamp, nonce}" }, 400);
    }
    const { event_json: eventJson, timestamp, nonce } = body;

    const maxSkewMs = Number(env.POW_MAX_SKEW_MS) || 300000;
    if (!isTimestampFresh(timestamp, maxSkewMs)) {
      return json({ error: "stale or future timestamp" }, 400);
    }

    const difficulty = Number(env.POW_DIFFICULTY_PREFIX_LENGTH) || 5;
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

    const result = validateTelemetryEvent(event as Record<string, unknown>);
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
      const write = await writeTelemetryEvent(serviceAccount, env.RTDB_DATABASE_URL, event as Record<string, unknown>);
      if (!write.ok) {
        return json({ error: "upstream write failed" }, 502);
      }
      let id: string | undefined;
      try {
        id = (JSON.parse(write.body) as { name?: string }).name;
      } catch {
        // Non-fatal - the write itself already succeeded.
      }
      return json({ ok: true, id }, 200);
    } catch {
      return json({ error: "upstream write failed" }, 502);
    }
  },
};
