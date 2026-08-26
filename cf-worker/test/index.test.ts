import { describe, expect, it } from "vitest";
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
});
