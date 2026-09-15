import { describe, expect, it, vi } from "vitest";
import worker, { type Env } from "../src/index";

const env: Env = {
  POW_DIFFICULTY_PREFIX_LENGTH: "0",
  POW_MAX_SKEW_MS: "300000",
  RTDB_DATABASE_URL: "https://test.firebaseio.com",
  FIREBASE_SERVICE_ACCOUNT_JSON: "{}",
};

function request(body: unknown, headers: HeadersInit = {}): Request {
  return new Request("https://worker.example/telemetry", {
    method: "POST",
    headers: { "content-type": "application/json", ...headers },
    body: JSON.stringify(body),
  });
}

describe("gateway request bounds and configuration", () => {
  it("rejects a declared oversized body without parsing it", async () => {
    const response = await worker.fetch(
      request({}, { "content-length": String(64 * 1024 + 1) }),
      env,
    );
    expect(response.status).toBe(413);
    expect(await response.json()).toEqual({ error: "request body too large" });
  });

  it("enforces the body limit even when content-length is absent", async () => {
    const oversized = new Request("https://worker.example/telemetry", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ padding: "x".repeat(64 * 1024) }),
    });
    expect(oversized.headers.has("content-length")).toBe(false);

    const response = await worker.fetch(oversized, env);

    expect(response.status).toBe(413);
    expect(await response.json()).toEqual({ error: "request body too large" });
  });

  it("rejects oversized event_json before hashing", async () => {
    const response = await worker.fetch(
      request({ event_json: "x".repeat(32 * 1024 + 1), timestamp: Date.now(), nonce: 0 }),
      env,
    );
    expect(response.status).toBe(413);
    expect(await response.json()).toEqual({ error: "event_json too large" });
  });

  it("rejects wrapper fields that are not proof-bound", async () => {
    const response = await worker.fetch(
      request({ event_json: "{}", timestamp: Date.now(), nonce: 0, ignored: true }),
      env,
    );
    expect(response.status).toBe(400);
  });

  it("reports out-of-range proof settings as a server error", async () => {
    const response = await worker.fetch(
      request({ event_json: "{}", timestamp: Date.now(), nonce: 0 }),
      { ...env, POW_DIFFICULTY_PREFIX_LENGTH: "65" },
    );
    expect(response.status).toBe(500);
    expect(await response.json()).toEqual({ error: "server misconfigured" });
  });

  it("treats an empty proof-difficulty variable as unset instead of difficulty 0", async () => {
    const response = await worker.fetch(
      request({ event_json: "{}", timestamp: Date.now(), nonce: 0 }),
      { ...env, POW_DIFFICULTY_PREFIX_LENGTH: "" },
    );
    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ error: "proof of work invalid" });
  });

  it("treats a whitespace-only proof-difficulty variable the same way", async () => {
    const response = await worker.fetch(
      request({ event_json: "{}", timestamp: Date.now(), nonce: 0 }),
      { ...env, POW_DIFFICULTY_PREFIX_LENGTH: " " },
    );
    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ error: "proof of work invalid" });
  });

  it("treats an empty skew variable as the default window", async () => {
    const response = await worker.fetch(
      request({ event_json: "{}", timestamp: Date.now() - 1000, nonce: 0 }),
      { ...env, POW_MAX_SKEW_MS: "" },
    );
    const payload = (await response.json()) as { error?: string };
    expect(payload.error).not.toBe("stale or future timestamp");
  });
});

// A throwaway RSA-2048 PKCS8 test key (not used anywhere else) so the
// service-account JWT signing path in src/rtdb.ts can run for real.
const TEST_SERVICE_ACCOUNT_PRIVATE_KEY = `-----BEGIN PRIVATE KEY-----
MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQCwGFg6j9j7X+8k
EM6HNyvc0EKYTmWcOUW740UsZRIWeU19o/GBuqOV3XVEQYXVlRAYC8io26TuoPbM
6bi4uzsfru7Gos1aYtRyy6MFgwabThwstS9tBrhJfLKGcx/aczV5TqojQVvFOSY1
wpqA61uEmmC3VmWzKztFkC6rjYO/FUwygfr6SFLHF3S/TtIX0IBcB4vlhLRqezaD
KrdAXtRsEN/jl7Oa8pMD3fVs2GDLXMenWyToD8rKEtAfQ75mg5bfLqzm0PKw+jlV
5ZlJ2xSnR/1zypB6gHWOC54Oos2DvxwHUbi4zcUZDIEbN0yfNmPb3xoKyixbMIqV
cIpzsvHZAgMBAAECggEAVaWRi/IYw7JWOoFeIc/Iqp40NaWzr/b/HrIcG8qQsJOR
B/Gr7b/b/nD2rxr7P/U/HaLllpM1tcZeIy3t5RNTX0aS5dOa80IsOCUpBe5DUVf9
RhVdmrZw/XUD03a84F+2e2iyQXFxdAwmtHEQ+nD+UxFOxvzje/Aj5OKKgG/UyyN+
HpuVvtlLE2ShTtWx+uPsg+zEahhsZGmsirJPMJAa+R9Xq7g6JN28hysKcvQKSp4T
8P1IEMlSYzMZyKhuXAJtDSC87qRdL2YjT91YuWiMsxxEVspHnVCMouwONGVqWSxF
vJtsnRSjvZU2DPKP4hxm+NvdOwtOA/njoQMJZw+NnQKBgQDqa1M7dlMuHSCMcIXh
4678LgqwBx1dc7mAK65ZrEgSFc4YPf6rumb9GxRJWTEs7fSoP5tKHLOmBaOQ2EcM
nXgPMBbZ6g5uEv5Vv4nJHFT2sShg0Wh4g7TwljIGGJ0wuV6kJ+bTpwH2zAlm8HCF
oS0j7946xiify6TER3EM5u4C9wKBgQDATncl6Xc+eKNeFYd77cLEV+7uA0LNExdB
Oayu5atRtAIvaWU6wU/s5dwbdLVV+hNlLmljcpgWoL9LxqsKFCXrrZnV5lFbfYjb
pPA2UJQhg5ad+63VkFyO8Hi79kgvwoqPuAW62DN7+juvqWsTkve1YOzp2Jzh4j7s
89MOu3KtrwKBgAUMCH+4PXQ5tlCvv4Ish8DwMNS3Yn93lV/YEOnnVqnlBEnrU8dY
vQzn/1jQ7ckc2m6g5/QBiDCj4HCm52izHzmcfHF2o5blG8q20/2beYzSJZ9oAsrN
cyDW6v7Mmt3Ir+vy2/pklxs8K1unA5Us8i7a5Dr5tzgxhzuemiV/91HjAoGBALb4
8XztCjwyZJ5cNbDApJRUZk2oZKLjCzlQOvGeLMdsUrfxvBOPYxCwFCE7hl3rtxCK
fFPW8MZ25AyhVpQcX4hCgSB4J+i5JMJ3yOak/Ix2u5RNpzSQSsDmJLoSttRacaQV
H76Lf1Dy4l9c/zh8mZvGQSSuqXZy4hRqWeKmj5KZAoGBAK3FxWSz/INgbzwmTEC4
+edT04Sett7tNh5smGYdsWOlEgOTnnd/IWGV+zt5NNyXuz+sHdKp4deLVwfK+IBu
iZnjNthtRkctkO685fYVsh9ydR1wOaa7lRLjLnWzcYq3K2En4/gMMu8qF+Et/7AN
mEK9mEIOa3+HH3tEd0aZMNLf
-----END PRIVATE KEY-----`;

describe("write-once key derivation (dedupe on event bytes, not the proof)", () => {
  it("derives the write-once key from the event bytes so a retry cannot duplicate it", async () => {
    const fullEnv: Env = {
      ...env,
      FIREBASE_SERVICE_ACCOUNT_JSON: JSON.stringify({
        client_email: "test@example.iam.gserviceaccount.com",
        private_key: TEST_SERVICE_ACCOUNT_PRIVATE_KEY,
      }),
    };
    const eventJson = JSON.stringify({
      ram_gb: 24,
      unified_memory: true,
      model_installed: "model-7B-Q4.gguf",
      engine: "ollama",
      benchmark_version: 4,
      recorded_at: "2026-07-20T00:00:00+00:00",
      tokens_per_sec: 20.5,
    });

    const putUrls: string[] = [];
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : (input as Request).url ?? String(input);
      if (url.includes("oauth2.googleapis.com")) {
        return new Response(JSON.stringify({ access_token: "test-access-token", expires_in: 3600 }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      putUrls.push(url);
      return new Response("ok", { status: 200 });
    });

    try {
      const first = await worker.fetch(
        request({ event_json: eventJson, timestamp: Date.now(), nonce: 0 }),
        fullEnv,
      );
      const second = await worker.fetch(
        request({ event_json: eventJson, timestamp: Date.now() - 1, nonce: 1 }),
        fullEnv,
      );

      expect(first.status).toBe(200);
      expect(second.status).toBe(200);
      expect(putUrls.length).toBe(2);
      expect(putUrls[0]).toMatch(/\/telemetry\/[0-9a-f]{64}\.json$/);
      expect(putUrls[0]).toBe(putUrls[1]);
    } finally {
      fetchSpy.mockRestore();
    }
  });
});
