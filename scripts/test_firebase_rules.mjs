import assert from "node:assert/strict";


const base = "http://127.0.0.1:9000";
// Realtime Database emulator instances use the project's default RTDB name,
// not the bare project ID. Using `demo-localfit` here silently activated a
// second, rules-free namespace and made every authorization assertion
// meaningless.
const namespace = "demo-localfit-default-rtdb";

async function request(path, method, body) {
  const response = await fetch(`${base}/${path}.json?ns=${namespace}`, {
    method,
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    // A denied emulator response may not have a JSON body.
  }
  return { ok: response.ok, status: response.status, payload };
}

const valid = {
  ram_gb: 24,
  vram_gb: 24,
  unified_memory: true,
  model_installed: "model-7B-Q4.gguf",
  model_repo_id: "org/model-7B-GGUF",
  model_size_bytes: 4 * 1024 ** 3,
  engine: "ollama",
  benchmark_version: 4,
  recorded_at: "2026-07-20T00:00:00+00:00",
  tokens_per_sec: 20.5,
  sample_count: 3,
  tokens_per_sec_min: 19.5,
  tokens_per_sec_max: 21.5,
  runtime_profile: "balanced",
  context_length: 4096,
  gpu_offload_percent: 100,
  cpu_threads: 8,
  num_batch: 512,
};

const created = await request("telemetry", "POST", valid);
assert.equal(created.ok, true, `valid schema 4 event was rejected (${created.status})`);
assert.equal(typeof created.payload?.name, "string");

for (const benchmarkVersion of [1, 2, 3, 4]) {
  const legacy = {
    ...valid,
    benchmark_version: benchmarkVersion,
  };
  if (benchmarkVersion < 3) {
    legacy.os = "test-os";
    legacy.cpu = "test-cpu";
    legacy.gpu = "test-gpu";
  }
  const legacyCreated = await request("telemetry", "POST", legacy);
  assert.equal(
    legacyCreated.ok,
    true,
    `legacy schema ${benchmarkVersion} event was rejected (${legacyCreated.status})`,
  );
}

const validV6 = {
  ...valid,
  benchmark_version: 6,
  model_filename: "model-7B-Q4.gguf",
  model_digest: "a".repeat(64),
  parameter_count_b: 7,
  active_parameter_count_b: 7,
  quant_bits: 4,
  engine_version: "0.12.0",
  client_version: "0.1.0",
  runtime_profile: "explicit_ollama_options",
  context_length: 4096,
  gpu_offload_percent: 100,
  cpu_threads: 8,
  num_batch: 512,
  sample_count: 3,
  tokens_per_sec_min: 19.5,
  tokens_per_sec_max: 21.5,
  quality_pack_id: "localfit-smoke",
  quality_pack_version: "1",
  quality_correct: 4,
  quality_total: 5,
  quality_accuracy: 0.8,
  cpu_model: "AMD Ryzen 5 5600X 6-Core Processor",
  cpu_arch: "x86_64",
  cpu_physical_cores: 6,
  cpu_logical_cores: 12,
};
const v6Created = await request("telemetry", "POST", validV6);
assert.equal(v6Created.ok, true, `valid schema 6 event was rejected (${v6Created.status})`);

const taggedFilenameV5 = await request("telemetry", "POST", {
  ...validV6,
  model_filename: "model:latest",
});
assert.equal(taggedFilenameV5.ok, true, "schema 6 rejected a normal Ollama model tag");

const missingV5Metadata = await request("telemetry", "POST", {
  ...validV6,
  client_version: undefined,
});
assert.equal(missingV5Metadata.ok, false, "schema 6 accepted missing direct metadata");

const invalidV5Runtime = await request("telemetry", "POST", {
  ...validV6,
  cpu_threads: 0,
});
assert.equal(invalidV5Runtime.ok, false, "schema 6 accepted invalid runtime metadata");

const fractionalV5Runtime = await request("telemetry", "POST", {
  ...validV6,
  cpu_threads: 8.5,
});
assert.equal(fractionalV5Runtime.ok, false, "schema 5 accepted fractional runtime metadata");

const invalidV5Samples = await request("telemetry", "POST", {
  ...validV6,
  sample_count: 2,
});
assert.equal(invalidV5Samples.ok, false, "schema 5 accepted fewer than three samples");

const invalidV5Filename = await request("telemetry", "POST", {
  ...validV6,
  model_filename: "C:\\private\\model.gguf",
});
assert.equal(invalidV5Filename.ok, false, "schema 5 accepted a local model path");

const invalidV5Digest = await request("telemetry", "POST", {
  ...validV6,
  model_digest: "A".repeat(64),
});
assert.equal(invalidV5Digest.ok, false, "schema 5 accepted a non-normalized digest");

const nonHexV5Digest = await request("telemetry", "POST", {
  ...validV6,
  model_digest: "g".repeat(64),
});
assert.equal(nonHexV5Digest.ok, false, "schema 5 accepted a non-hex digest");

const invalidV5Quality = await request("telemetry", "POST", {
  ...validV6,
  quality_accuracy: 0.1,
});
assert.equal(invalidV5Quality.ok, false, "schema 5 accepted an inconsistent quality ratio");

const partialV5Quality = await request("telemetry", "POST", {
  ...validV6,
  quality_pack_id: undefined,
});
assert.equal(partialV5Quality.ok, false, "schema 5 accepted partial quality metadata");

const fractionalVersion = await request("telemetry", "POST", {
  ...valid,
  benchmark_version: 4.5,
});
assert.equal(fractionalVersion.ok, false, "schema accepted a fractional benchmark version");

const rawName = await request("telemetry", "POST", { ...valid, cpu: "Apple M5" });
assert.equal(rawName.ok, false, "schema 4 unexpectedly accepted a raw CPU name");

const unknown = await request("telemetry", "POST", { ...valid, unexpected: "value" });
assert.equal(unknown.ok, false, "telemetry unexpectedly accepted an unknown field");

const outOfRange = await request("telemetry", "POST", { ...valid, tokens_per_sec: 5000 });
assert.equal(outOfRange.ok, false, "telemetry unexpectedly accepted an out-of-range speed");

const overwrite = await request(`telemetry/${created.payload.name}`, "PUT", {
  ...valid,
  tokens_per_sec: 99,
});
assert.equal(overwrite.ok, false, "append-only telemetry unexpectedly allowed an overwrite");

const unrelated = await request("unrelated", "POST", { value: true });
assert.equal(unrelated.ok, false, "default-deny rule unexpectedly allowed another path");

const readable = await request("telemetry", "GET");
assert.equal(readable.ok, true, "public retraining read unexpectedly failed");

// --- v7: structured success/failure telemetry -----------------------------

const v7Success = {
  ram_gb: 24,
  vram_gb: 6,
  unified_memory: false,
  model_installed: "small:latest",
  engine: "ollama",
  benchmark_version: 7,
  recorded_at: "2026-07-24T00:00:00+00:00",
  outcome: "success",
  tokens_per_sec: 20.5,
  parameter_count_b: 7,
  active_parameter_count_b: 7,
  quant_bits: 4,
  engine_version: "0.32.1",
  client_version: "0.1.64",
  runtime_profile: "explicit_ollama_options",
  context_length: 4096,
  gpu_offload_percent: 100,
  cpu_threads: 8,
  num_batch: 512,
  sample_count: 3,
  tokens_per_sec_min: 19.5,
  tokens_per_sec_max: 21.5,
  cpu_model: "AMD Ryzen 5 5600X 6-Core Processor",
  cpu_arch: "x86_64",
  cpu_physical_cores: 6,
  cpu_logical_cores: 12,
};
const v7SuccessCreated = await request("telemetry", "POST", v7Success);
assert.equal(v7SuccessCreated.ok, true, `valid v7 success event was rejected (${v7SuccessCreated.status})`);

const v7SuccessWithModelProvider = await request("telemetry", "POST", {
  ...v7Success,
  model_provider: "huggingface",
});
assert.equal(
  v7SuccessWithModelProvider.ok,
  true,
  `v7 success with model_provider was rejected (${v7SuccessWithModelProvider.status})`,
);

const v7SuccessWithOverlongModelProvider = await request("telemetry", "POST", {
  ...v7Success,
  model_provider: "a".repeat(65),
});
assert.equal(
  v7SuccessWithOverlongModelProvider.ok,
  false,
  "v7 success accepted a model_provider longer than 64 characters",
);

const v7SuccessWithFailureReason = await request("telemetry", "POST", {
  ...v7Success,
  failure_reason: "unknown",
});
assert.equal(v7SuccessWithFailureReason.ok, false, "v7 success accepted a failure_reason field");

const v7SuccessMissingTokens = await request("telemetry", "POST", {
  ...v7Success,
  tokens_per_sec: undefined,
});
assert.equal(v7SuccessMissingTokens.ok, false, "v7 success accepted a missing tokens_per_sec");

const v7ModelUnfit = {
  ram_gb: 24,
  vram_gb: 6,
  unified_memory: false,
  model_installed: "too-big:latest",
  engine: "ollama",
  benchmark_version: 7,
  recorded_at: "2026-07-24T00:00:00+00:00",
  outcome: "model_unfit",
  failure_reason: "out_of_memory",
  parameter_count_b: 70,
  active_parameter_count_b: 70,
  quant_bits: 4,
  engine_version: "0.32.1",
  client_version: "0.1.64",
  cpu_model: "AMD Ryzen 5 5600X 6-Core Processor",
  cpu_arch: "x86_64",
  cpu_physical_cores: 6,
  cpu_logical_cores: 12,
};
const v7ModelUnfitCreated = await request("telemetry", "POST", v7ModelUnfit);
assert.equal(v7ModelUnfitCreated.ok, true, `valid v7 model_unfit event was rejected (${v7ModelUnfitCreated.status})`);

const v7ModelUnfitLoadFailedRejected = await request("telemetry", "POST", {
  ...v7ModelUnfit,
  failure_reason: "model_load_failed",
});
assert.equal(
  v7ModelUnfitLoadFailedRejected.ok,
  false,
  "v7 model_unfit accepted model_load_failed - a missing/undiagnosed load failure is not fit evidence",
);

const v7ModelUnfitUnsupported = await request("telemetry", "POST", {
  ...v7ModelUnfit,
  failure_reason: "unsupported_runtime",
});
assert.equal(v7ModelUnfitUnsupported.ok, true, "v7 model_unfit rejected unsupported_runtime");

const v7TransientMinimal = await request("telemetry", "POST", {
  ram_gb: 24,
  unified_memory: false,
  model_installed: "missing:latest",
  engine: "ollama",
  benchmark_version: 7,
  recorded_at: "2026-07-24T00:00:00+00:00",
  outcome: "transient_error",
  failure_reason: "model_load_failed",
});
assert.equal(
  v7TransientMinimal.ok,
  true,
  "v7 transient_error (model_load_failed) rejected an event with no model metadata " +
    "(e.g. tag never resolved / not yet downloaded)",
);

const v7ModelUnfitWrongLaneReason = await request("telemetry", "POST", {
  ...v7ModelUnfit,
  failure_reason: "generation_timeout",
});
assert.equal(
  v7ModelUnfitWrongLaneReason.ok,
  false,
  "v7 model_unfit accepted a transient_error-lane failure_reason",
);

const v7ModelUnfitFakeSpeed = await request("telemetry", "POST", {
  ...v7ModelUnfit,
  tokens_per_sec: 0,
  tokens_per_sec_min: 0,
  tokens_per_sec_max: 0,
  sample_count: 3,
});
assert.equal(v7ModelUnfitFakeSpeed.ok, false, "v7 model_unfit accepted a faked zero tokens_per_sec");

const v7ModelUnfitMissingReason = await request("telemetry", "POST", {
  ...v7ModelUnfit,
  failure_reason: undefined,
});
assert.equal(v7ModelUnfitMissingReason.ok, false, "v7 model_unfit accepted a missing failure_reason");

const v7Transient = {
  ram_gb: 24,
  vram_gb: 6,
  unified_memory: false,
  model_installed: "small:latest",
  engine: "ollama",
  benchmark_version: 7,
  recorded_at: "2026-07-24T00:00:00+00:00",
  outcome: "transient_error",
  failure_reason: "ollama_unavailable",
};
const v7TransientCreated = await request("telemetry", "POST", v7Transient);
assert.equal(v7TransientCreated.ok, true, `valid v7 transient_error event was rejected (${v7TransientCreated.status})`);

for (const reason of ["model_load_failed", "generation_timeout", "connection_error", "no_timing_metrics", "unknown"]) {
  const created = await request("telemetry", "POST", { ...v7Transient, failure_reason: reason });
  assert.equal(created.ok, true, `v7 transient_error rejected failure_reason=${reason}`);
}

const v7TransientWrongLaneReason = await request("telemetry", "POST", {
  ...v7Transient,
  failure_reason: "out_of_memory",
});
assert.equal(
  v7TransientWrongLaneReason.ok,
  false,
  "v7 transient_error accepted a model_unfit-lane failure_reason",
);

const v7InvalidOutcome = await request("telemetry", "POST", { ...v7Transient, outcome: "maybe" });
assert.equal(v7InvalidOutcome.ok, false, "v7 accepted an invalid outcome enum value");

const v7MissingOutcome = await request("telemetry", "POST", { ...v7Success, outcome: undefined });
assert.equal(v7MissingOutcome.ok, false, "v7 accepted an event with no outcome at all");

const v7UnknownField = await request("telemetry", "POST", { ...v7Transient, exception_message: "boom" });
assert.equal(v7UnknownField.ok, false, "v7 accepted an unlisted field (e.g. raw exception text)");

// --- v7: performance_unfit (confirmed_generation_timeout) ------------------

const v7PerformanceUnfit = {
  ram_gb: 31.3,
  vram_gb: 6,
  unified_memory: false,
  model_installed: "qwen2.5:32b-instruct-q8_0",
  engine: "ollama",
  benchmark_version: 7,
  recorded_at: "2026-07-24T00:00:00+00:00",
  outcome: "performance_unfit",
  failure_reason: "confirmed_generation_timeout",
  confirmation_attempts: 2,
  timeout_seconds: 180,
  parameter_count_b: 32.8,
  active_parameter_count_b: 32,
  quant_bits: 8,
  engine_version: "0.32.1",
  client_version: "0.1.65",
  cpu_model: "Intel(R) Core(TM) i7-9700 CPU @ 3.00GHz",
  cpu_arch: "x86_64",
  cpu_physical_cores: 8,
  cpu_logical_cores: 8,
};
const v7PerformanceUnfitCreated = await request("telemetry", "POST", v7PerformanceUnfit);
assert.equal(
  v7PerformanceUnfitCreated.ok, true,
  `valid v7 performance_unfit event was rejected (${v7PerformanceUnfitCreated.status})`,
);

const v7PerformanceUnfitMinimal = await request("telemetry", "POST", {
  ram_gb: 31.3,
  unified_memory: false,
  model_installed: "qwen2.5:32b-instruct-q8_0",
  engine: "ollama",
  benchmark_version: 7,
  recorded_at: "2026-07-24T00:00:00+00:00",
  outcome: "performance_unfit",
  failure_reason: "confirmed_generation_timeout",
  confirmation_attempts: 2,
  timeout_seconds: 180,
});
assert.equal(
  v7PerformanceUnfitMinimal.ok, true,
  "v7 performance_unfit rejected an event with no model metadata (best-effort fields are optional)",
);

for (const attempts of [1, 3, "2", true]) {
  const wrongAttempts = await request("telemetry", "POST", { ...v7PerformanceUnfit, confirmation_attempts: attempts });
  assert.equal(wrongAttempts.ok, false, `v7 performance_unfit accepted confirmation_attempts=${JSON.stringify(attempts)}`);
}

const v7PerformanceUnfitMissingAttempts = await request("telemetry", "POST", {
  ...v7PerformanceUnfit,
  confirmation_attempts: undefined,
});
assert.equal(v7PerformanceUnfitMissingAttempts.ok, false, "v7 performance_unfit accepted a missing confirmation_attempts");

const v7PerformanceUnfitMissingTimeout = await request("telemetry", "POST", {
  ...v7PerformanceUnfit,
  timeout_seconds: undefined,
});
assert.equal(v7PerformanceUnfitMissingTimeout.ok, false, "v7 performance_unfit accepted a missing timeout_seconds");

for (const timeout of [0, -5, 4000]) {
  const badTimeout = await request("telemetry", "POST", { ...v7PerformanceUnfit, timeout_seconds: timeout });
  assert.equal(badTimeout.ok, false, `v7 performance_unfit accepted an out-of-range timeout_seconds=${timeout}`);
}

const v7PerformanceUnfitWrongLaneReason = await request("telemetry", "POST", {
  ...v7PerformanceUnfit,
  failure_reason: "out_of_memory",
});
assert.equal(
  v7PerformanceUnfitWrongLaneReason.ok, false,
  "v7 performance_unfit accepted a model_unfit-lane failure_reason",
);

const v7PerformanceUnfitTransientReason = await request("telemetry", "POST", {
  ...v7PerformanceUnfit,
  failure_reason: "generation_timeout",
});
assert.equal(
  v7PerformanceUnfitTransientReason.ok, false,
  "v7 performance_unfit accepted an unconfirmed generation_timeout (only confirmed_generation_timeout is valid here)",
);

const v7PerformanceUnfitFakeSpeed = await request("telemetry", "POST", {
  ...v7PerformanceUnfit,
  tokens_per_sec: 0,
  tokens_per_sec_min: 0,
  tokens_per_sec_max: 0,
  sample_count: 1,
});
assert.equal(v7PerformanceUnfitFakeSpeed.ok, false, "v7 performance_unfit accepted a faked zero tokens_per_sec");

const v7ConfirmedTimeoutOnModelUnfit = await request("telemetry", "POST", {
  ...v7ModelUnfit,
  failure_reason: "confirmed_generation_timeout",
});
assert.equal(
  v7ConfirmedTimeoutOnModelUnfit.ok, false,
  "v7 model_unfit accepted a performance_unfit-lane failure_reason",
);

const v7ModelUnfitWithConfirmationAttempts = await request("telemetry", "POST", {
  ...v7ModelUnfit,
  confirmation_attempts: 2,
});
assert.equal(
  v7ModelUnfitWithConfirmationAttempts.ok, false,
  "v7 model_unfit accepted confirmation_attempts - that field belongs to performance_unfit only",
);

const v7SuccessWithConfirmationAttempts = await request("telemetry", "POST", {
  ...v7Success,
  confirmation_attempts: 2,
  timeout_seconds: 180,
});
assert.equal(
  v7SuccessWithConfirmationAttempts.ok, false,
  "v7 success accepted confirmation_attempts/timeout_seconds - those fields belong to performance_unfit only",
);

const v7TransientWithTimeoutSeconds = await request("telemetry", "POST", {
  ...v7Transient,
  timeout_seconds: 180,
});
assert.equal(
  v7TransientWithTimeoutSeconds.ok, false,
  "v7 transient_error accepted timeout_seconds - that field belongs to performance_unfit only",
);

// --- v8: cpu_score/cpu_tier replace cpu_model, gpu_score/gpu_tier added ----

const v8Success = {
  ram_gb: 24,
  vram_gb: 6,
  unified_memory: false,
  model_installed: "small:latest",
  engine: "ollama",
  benchmark_version: 8,
  recorded_at: "2026-07-30T00:00:00+00:00",
  outcome: "success",
  tokens_per_sec: 20.5,
  parameter_count_b: 7,
  active_parameter_count_b: 7,
  quant_bits: 4,
  engine_version: "0.32.1",
  client_version: "0.1.70",
  runtime_profile: "explicit_ollama_options",
  context_length: 4096,
  gpu_offload_percent: 100,
  cpu_threads: 8,
  num_batch: 512,
  sample_count: 3,
  tokens_per_sec_min: 19.5,
  tokens_per_sec_max: 21.5,
  cpu_score: 7950,
  cpu_tier: 1,
  cpu_arch: "x86_64",
  cpu_physical_cores: 6,
  cpu_logical_cores: 12,
  gpu_score: 4090,
  gpu_tier: 0,
};
const v8SuccessCreated = await request("telemetry", "POST", v8Success);
assert.equal(v8SuccessCreated.ok, true, `valid v8 success event was rejected (${v8SuccessCreated.status})`);

const v8SuccessWithoutGpu = await request("telemetry", "POST", {
  ...v8Success,
  gpu_score: undefined,
  gpu_tier: undefined,
  vram_gb: undefined,
});
assert.equal(
  v8SuccessWithoutGpu.ok,
  true,
  "v8 success rejected an event with no GPU detected (gpu_score/gpu_tier/vram_gb all absent)",
);

const v8SuccessMissingCpuScore = await request("telemetry", "POST", {
  ...v8Success,
  cpu_score: undefined,
});
assert.equal(
  v8SuccessMissingCpuScore.ok,
  false,
  "v8 success accepted a missing cpu_score - it is required, unlike gpu_score",
);

const v8SuccessWithRawCpuModelRejected = await request("telemetry", "POST", {
  ...v8Success,
  cpu_model: "AMD Ryzen 5 5600X 6-Core Processor",
});
assert.equal(
  v8SuccessWithRawCpuModelRejected.ok,
  false,
  "v8 accepted a raw cpu_model field - v8 must never carry it, that's the whole point",
);

const v8CpuScoreOutOfRange = await request("telemetry", "POST", {
  ...v8Success,
  cpu_score: 100_000,
});
assert.equal(v8CpuScoreOutOfRange.ok, false, "v8 accepted a cpu_score above the 99999 bound");

const v8CpuTierOutOfRange = await request("telemetry", "POST", {
  ...v8Success,
  cpu_tier: 11,
});
assert.equal(v8CpuTierOutOfRange.ok, false, "v8 accepted a cpu_tier above the 10 bound");

const v8ModelUnfit = {
  ram_gb: 24,
  vram_gb: 6,
  unified_memory: false,
  model_installed: "too-big:latest",
  engine: "ollama",
  benchmark_version: 8,
  recorded_at: "2026-07-30T00:00:00+00:00",
  outcome: "model_unfit",
  failure_reason: "out_of_memory",
};
const v8ModelUnfitCreated = await request("telemetry", "POST", v8ModelUnfit);
assert.equal(v8ModelUnfitCreated.ok, true, `valid v8 model_unfit event was rejected (${v8ModelUnfitCreated.status})`);

const v8Transient = {
  ram_gb: 24,
  unified_memory: false,
  model_installed: "small:latest",
  engine: "ollama",
  benchmark_version: 8,
  recorded_at: "2026-07-30T00:00:00+00:00",
  outcome: "transient_error",
  failure_reason: "ollama_unavailable",
};
const v8TransientCreated = await request("telemetry", "POST", v8Transient);
assert.equal(v8TransientCreated.ok, true, `valid v8 transient_error event was rejected (${v8TransientCreated.status})`);

// --- v9: fixed contribution profile and measurement quality ---------------

const v9Success = {
  ...v8Success,
  benchmark_version: 9,
  recorded_at: "2026-08-18T00:00:00+00:00",
  client_version: "0.2.78",
  context_length: 1024,
  gpu_offload_percent: 0,
  num_batch: 128,
  measurement_profile: "contribute-v1",
  measurement_quality: "clean",
  ram_available_before_gb: 2.2,
  ram_available_min_gb: 2.0,
  ram_available_after_gb: 2.1,
  memory_pressure_observed: false,
  tokens_per_sec_mad_ratio: 0.02,
  memory_estimate_source: "gguf_header",
  memory_estimate_confidence: "medium",
  estimated_mapped_weights_gb: 0.75,
  estimated_committed_ram_gb: 0.3,
  estimated_required_vram_gb: 0,
};
const v9SuccessCreated = await request("telemetry", "POST", v9Success);
assert.equal(v9SuccessCreated.ok, true, `valid v9 success event was rejected (${v9SuccessCreated.status})`);

const v9SuccessWithNullGpuFields = await request("telemetry", "POST", {
  ...v9Success,
  vram_gb: null,
  gpu_tflops: null,
});
assert.equal(
  v9SuccessWithNullGpuFields.ok,
  true,
  "v9 rejected optional GPU fields represented as JSON null",
);

// A model file named "-1B-" parses to 1.0 while its GGUF tensor count measured
// 0.99989. Emitting the rounded name-derived value put active above total and
// the server rejected the whole benchmark, so pin that the server does reject
// it - the client must reconcile the two counts before sending.
const v9ActiveAboveTotal = await request("telemetry", "POST", {
  ...v9Success,
  parameter_count_b: 0.99989,
  active_parameter_count_b: 1.0,
});
assert.equal(
  v9ActiveAboveTotal.ok,
  false,
  "v9 accepted an active parameter count above the total parameter count",
);

const v9ActiveEqualToTotal = await request("telemetry", "POST", {
  ...v9Success,
  parameter_count_b: 0.99989,
  active_parameter_count_b: 0.99989,
});
assert.equal(
  v9ActiveEqualToTotal.ok,
  true,
  "v9 rejected a dense model whose active count equals its total",
);

const v9Pressured = await request("telemetry", "POST", {
  ...v9Success,
  measurement_quality: "pressured",
  memory_pressure_observed: true,
});
assert.equal(v9Pressured.ok, true, "v9 rejected a consistently labelled pressured measurement");

const v9Unstable = await request("telemetry", "POST", {
  ...v9Success,
  measurement_quality: "unstable",
  tokens_per_sec_mad_ratio: 0.2,
});
assert.equal(v9Unstable.ok, true, "v9 rejected a consistently labelled unstable measurement");

for (const [name, changes] of [
  ["missing memory field", { ram_available_min_gb: undefined }],
  ["wrong context", { context_length: 4096 }],
  ["wrong batch", { num_batch: 512 }],
  ["clean high-MAD result", { tokens_per_sec_mad_ratio: 0.2 }],
  ["pressure without label", { memory_pressure_observed: true }],
  ["pressure label without pressure", { measurement_quality: "pressured" }],
  ["unknown estimate source", { memory_estimate_source: "guess" }],
]) {
  const rejected = await request("telemetry", "POST", { ...v9Success, ...changes });
  assert.equal(rejected.ok, false, `v9 accepted ${name}`);
}

const v9FailureRejected = await request("telemetry", "POST", {
  ...v8Transient,
  benchmark_version: 9,
});
assert.equal(v9FailureRejected.ok, false, "v9 accepted a failure event without measurement metadata");

console.log("Firebase rules scenarios passed.");
