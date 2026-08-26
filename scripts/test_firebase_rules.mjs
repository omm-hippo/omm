import assert from "node:assert/strict";


const base = "http://127.0.0.1:9000";
// Realtime Database emulator instances use the project's default RTDB name,
// not the bare project ID. Using `demo-localfit` here silently activated a
// second, rules-free namespace and made every authorization assertion
// meaningless.
const namespace = "demo-localfit-default-rtdb";

// The RTDB emulator only consults `auth_variable_override` once it has
// already found *some* token via `access_token` (or an Authorization
// header) - `owner` is the emulator's well-known admin-bypass literal, not
// a real credential, and is only meaningful alongside the override below,
// which is what actually becomes the rules' `auth` variable. Without the
// `access_token`, findAuthToken() short-circuits to `auth == null` and the
// override is never even inspected. Every scenario below defaults to
// authenticated, matching a real omm client after the anonymous-auth fix;
// pass `auth: false` to specifically exercise the unauthenticated-write
// rejection the fix introduced.
const authOverride =
  `access_token=owner&auth_variable_override=${encodeURIComponent(JSON.stringify({ uid: "test-uid" }))}`;

async function request(path, method, body, { auth = true } = {}) {
  const query = auth ? `ns=${namespace}&${authOverride}` : `ns=${namespace}`;
  const response = await fetch(`${base}/${path}.json?${query}`, {
    method,
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(5_000),
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    // A denied emulator response may not have a JSON body.
  }
  return { ok: response.ok, status: response.status, payload };
}

// --- telemetry: direct writes are closed --------------------------------
//
// omm-hippo/omm#133: unlimited-mintable anonymous auth tokens made
// `auth != null` an ineffective flooding gate, so telemetry/$event now
// denies every direct write - the Cloudflare Worker gateway (proof-of-work
// gated, writing with an Admin OAuth token that bypasses these rules
// entirely) is the only writer. The schema shape this file used to check
// against the emulator now lives in cf-worker/test/validate.test.ts,
// ported 1:1 from these same fixtures and run against the TS port of
// `.validate` that the gateway actually enforces. This file now only pins
// that the direct path stays shut - for any auth state and any payload,
// valid schema or not.

const validV9Shape = {
  ram_gb: 24,
  vram_gb: 6,
  unified_memory: false,
  model_installed: "small:latest",
  engine: "ollama",
  benchmark_version: 9,
  recorded_at: "2026-08-18T00:00:00+00:00",
  outcome: "success",
  tokens_per_sec: 20.5,
};

for (const auth of [true, false]) {
  const rejected = await request("telemetry", "POST", validV9Shape, { auth });
  assert.equal(
    rejected.ok,
    false,
    `telemetry unexpectedly accepted a direct write (auth: ${auth}) - the gateway must be the only writer`,
  );
}

const overwrite = await request("telemetry/some-fixed-key", "PUT", validV9Shape);
assert.equal(overwrite.ok, false, "telemetry unexpectedly accepted a direct write to a fixed key");

const unrelated = await request("unrelated", "POST", { value: true });
assert.equal(unrelated.ok, false, "default-deny rule unexpectedly allowed another path");

const readable = await request("telemetry", "GET");
assert.equal(readable.ok, true, "public retraining read unexpectedly failed");

// --- /error_reports: gateway-only and unreadable ----------------------------
// The Worker validates schema and proof-of-work, then writes with Admin OAuth.
// Direct Firebase clients must be denied just like direct telemetry writers.

const validReport = {
  schema_version: 1,
  error_type: "QualityEvaluationError",
  error_message: "Ollama /api/generate request failed",
  trigger: "install_quality_eval",
  recorded_at: "2026-08-19T00:00:00+00:00",
  client_version: "0.1.0",
  os_name: "Windows",
  os_version: "11",
  cpu_arch: "x86_64",
  cpu_score: 5600,
  cpu_tier: 0,
  gpu_score: 4090,
  gpu_tier: 0,
  catalog_ref: "unsloth/Qwen3-4B-GGUF:Qwen3-4B-Q4_K_M.gguf",
  engine: "ollama",
};

const reportCreated = await request("error_reports", "POST", validReport);
assert.equal(reportCreated.ok, false, "direct authenticated error report write was accepted");
const reportUnauthenticated = await request("error_reports", "POST", validReport, { auth: false });
assert.equal(reportUnauthenticated.ok, false, "direct unauthenticated error report write was accepted");

// The whole point of the separate node: nobody may read it back.
for (const auth of [true, false]) {
  const read = await request("error_reports", "GET", undefined, { auth });
  assert.equal(read.ok, false, `error_reports was readable (auth: ${auth})`);
  const readChild = await request("error_reports/some-report-id", "GET", undefined, { auth });
  assert.equal(readChild.ok, false, `an error report child was readable (auth: ${auth})`);
}

const fixedKey = `error_reports/fixed-${Date.now()}`;
const firstWrite = await request(fixedKey, "PUT", validReport);
assert.equal(firstWrite.ok, false, "direct fixed-key error report write was accepted");

console.log("Firebase rules scenarios passed.");
