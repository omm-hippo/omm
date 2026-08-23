import { describe, expect, it } from "vitest";
import { validateErrorReport, validateTelemetryEvent } from "../src/validate";

// Fixtures and expected outcomes are ported 1:1 from
// scripts/test_firebase_rules.mjs (the emulator-based rules test suite) so
// this TS port can be checked against the exact same scenarios the original
// RTDB rules were validated against.

describe("error report validation", () => {
  const report = {
    schema_version: 1,
    error_type: "RuntimeError",
    error_message: "boom",
    trigger: "crash",
    recorded_at: "2026-08-23T00:00:00+00:00",
    os_name: "Darwin",
  };

  it("accepts the allow-listed schema", () => {
    expect(validateErrorReport(report).valid).toBe(true);
    expect(validateErrorReport({ ...report, error_message: 123 }).valid).toBe(false);
  });

  it("rejects unknown fields and local paths", () => {
    expect(validateErrorReport({ ...report, traceback: "secret" }).valid).toBe(false);
    expect(validateErrorReport({ ...report, catalog_ref: "/Users/alice/model.gguf" }).valid).toBe(false);
  });
});

function ok(event: Record<string, unknown>) {
  const result = validateTelemetryEvent(event);
  expect(result.valid, result.reason).toBe(true);
}

function rejected(event: Record<string, unknown>) {
  const result = validateTelemetryEvent(event);
  expect(result.valid).toBe(false);
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

describe("base schema (v1-v6)", () => {
  it("accepts a valid v4 event", () => ok(valid));

  it.each([1, 2, 3, 4])("accepts legacy schema %i", (bv) => {
    const legacy: Record<string, unknown> = { ...valid, benchmark_version: bv };
    if (bv < 3) {
      legacy.os = "test-os";
      legacy.cpu = "test-cpu";
      legacy.gpu = "test-gpu";
    }
    ok(legacy);
  });

  it("accepts a valid v6 event", () => ok(validV6));
  it("accepts a normal Ollama model tag as model_filename", () => ok({ ...validV6, model_filename: "model:latest" }));
  it("rejects missing direct metadata", () => rejected({ ...validV6, client_version: undefined }));
  it("rejects invalid runtime metadata", () => rejected({ ...validV6, cpu_threads: 0 }));
  it("rejects fractional runtime metadata", () => rejected({ ...validV6, cpu_threads: 8.5 }));
  it("rejects fewer than three samples", () => rejected({ ...validV6, sample_count: 2 }));
  it("rejects a local model path as filename", () => rejected({ ...validV6, model_filename: "C:\\private\\model.gguf" }));
  it("rejects a non-normalized digest", () => rejected({ ...validV6, model_digest: "A".repeat(64) }));
  it("rejects a non-hex digest", () => rejected({ ...validV6, model_digest: "g".repeat(64) }));
  it("rejects an inconsistent quality ratio", () => rejected({ ...validV6, quality_accuracy: 0.1 }));
  it("rejects partial quality metadata", () => rejected({ ...validV6, quality_pack_id: undefined }));
  it("rejects a fractional benchmark_version", () => rejected({ ...valid, benchmark_version: 4.5 }));
  it("rejects a raw cpu field on v4", () => rejected({ ...valid, cpu: "Apple M5" }));
  it("rejects an unknown field", () => rejected({ ...valid, unexpected: "value" }));
  it("rejects an out-of-range speed", () => rejected({ ...valid, tokens_per_sec: 5000 }));
});

describe("path/control-character rejection on model fields", () => {
  it.each([
    ["model_installed", "/Users/victim/.ssh/id_rsa"],
    ["model_installed", "C:/Windows/System32/config"],
    ["model_installed", "..\\..\\secrets.txt"],
    ["model_installed", "org/../../../etc/passwd"],
    ["model_installed", "bad\x00value"],
    ["model_repo_id", "/etc/passwd"],
    ["model_repo_id", "org/model\x1b[0m"],
    ["model_repo_id", "org/../../secret"],
  ])("rejects a path/control-character %s value", (field, badValue) =>
    rejected({ ...valid, [field as string]: badValue }),
  );

  it("still accepts a legitimate model_installed tag", () => ok({ ...valid, model_installed: "llama3.1:8b-instruct-q4_0" }));
  it("still accepts a legitimate model_repo_id", () => ok({ ...valid, model_repo_id: "meta-llama/Llama-3.1-8B-Instruct-GGUF" }));
});

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

describe("v7 schema", () => {
  it("accepts a valid success event", () => ok(v7Success));
  it("accepts success with model_provider", () => ok({ ...v7Success, model_provider: "huggingface" }));
  it("rejects an overlong model_provider", () => rejected({ ...v7Success, model_provider: "a".repeat(65) }));
  it("rejects success carrying failure_reason", () => rejected({ ...v7Success, failure_reason: "unknown" }));
  it("rejects success missing tokens_per_sec", () => rejected({ ...v7Success, tokens_per_sec: undefined }));

  it("accepts a valid model_unfit event", () => ok(v7ModelUnfit));
  it("rejects model_unfit with model_load_failed", () => rejected({ ...v7ModelUnfit, failure_reason: "model_load_failed" }));
  it("accepts model_unfit with unsupported_runtime", () => ok({ ...v7ModelUnfit, failure_reason: "unsupported_runtime" }));
  it("rejects model_unfit with a transient-lane reason", () => rejected({ ...v7ModelUnfit, failure_reason: "generation_timeout" }));
  it("rejects model_unfit with a faked zero speed", () =>
    rejected({ ...v7ModelUnfit, tokens_per_sec: 0, tokens_per_sec_min: 0, tokens_per_sec_max: 0, sample_count: 3 }));
  it("rejects model_unfit missing failure_reason", () => rejected({ ...v7ModelUnfit, failure_reason: undefined }));
  it("rejects model_unfit with confirmation_attempts", () => rejected({ ...v7ModelUnfit, confirmation_attempts: 2 }));
  it("rejects model_unfit with a performance_unfit-lane reason", () =>
    rejected({ ...v7ModelUnfit, failure_reason: "confirmed_generation_timeout" }));

  it("accepts a minimal transient_error event with no model metadata", () =>
    ok({
      ram_gb: 24,
      unified_memory: false,
      model_installed: "missing:latest",
      engine: "ollama",
      benchmark_version: 7,
      recorded_at: "2026-07-24T00:00:00+00:00",
      outcome: "transient_error",
      failure_reason: "model_load_failed",
    }));
  it("accepts a valid transient_error event", () => ok(v7Transient));
  it.each(["model_load_failed", "generation_timeout", "connection_error", "no_timing_metrics", "unknown"])(
    "accepts transient_error reason=%s",
    (reason) => ok({ ...v7Transient, failure_reason: reason }),
  );
  it("rejects transient_error with a model_unfit-lane reason", () => rejected({ ...v7Transient, failure_reason: "out_of_memory" }));
  it("rejects an invalid outcome enum", () => rejected({ ...v7Transient, outcome: "maybe" }));
  it("rejects an event with no outcome", () => rejected({ ...v7Success, outcome: undefined }));
  it("rejects an unlisted field", () => rejected({ ...v7Transient, exception_message: "boom" }));
  it("rejects transient_error with timeout_seconds", () => rejected({ ...v7Transient, timeout_seconds: 180 }));

  it("accepts a valid performance_unfit event", () => ok(v7PerformanceUnfit));
  it("accepts performance_unfit with no model metadata", () =>
    ok({
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
    }));
  it.each([1, 3, "2", true])("rejects confirmation_attempts=%s", (attempts) =>
    rejected({ ...v7PerformanceUnfit, confirmation_attempts: attempts }),
  );
  it("rejects missing confirmation_attempts", () => rejected({ ...v7PerformanceUnfit, confirmation_attempts: undefined }));
  it("rejects missing timeout_seconds", () => rejected({ ...v7PerformanceUnfit, timeout_seconds: undefined }));
  it.each([0, -5, 4000])("rejects out-of-range timeout_seconds=%d", (timeout) =>
    rejected({ ...v7PerformanceUnfit, timeout_seconds: timeout }),
  );
  it("rejects performance_unfit with a model_unfit-lane reason", () =>
    rejected({ ...v7PerformanceUnfit, failure_reason: "out_of_memory" }));
  it("rejects unconfirmed generation_timeout", () => rejected({ ...v7PerformanceUnfit, failure_reason: "generation_timeout" }));
  it("rejects a faked zero speed", () =>
    rejected({ ...v7PerformanceUnfit, tokens_per_sec: 0, tokens_per_sec_min: 0, tokens_per_sec_max: 0, sample_count: 1 }));
  it("rejects success carrying confirmation_attempts/timeout_seconds", () =>
    rejected({ ...v7Success, confirmation_attempts: 2, timeout_seconds: 180 }));
});

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

const v8LmStudioSuccess = {
  ram_gb: 15.5,
  unified_memory: false,
  model_installed: "qwen2.5-0.5b-instruct",
  model_filename: "qwen2.5-0.5b-instruct",
  model_provider: "lmstudio",
  model_size_bytes: 400000000,
  engine: "lmstudio",
  benchmark_version: 8,
  recorded_at: "2026-08-18T13:36:44.037615+00:00",
  outcome: "success",
  tokens_per_sec: 103.2,
  tokens_per_sec_min: 100.0,
  tokens_per_sec_max: 106.0,
  sample_count: 3,
  parameter_count_b: 0.5,
  active_parameter_count_b: 0.5,
  quant_bits: 4,
  engine_version: "0.4.21",
  client_version: "0.2.95",
  cpu_score: 0,
  cpu_tier: 3,
  cpu_arch: "AMD64",
  cpu_physical_cores: 16,
  cpu_logical_cores: 22,
  gpu_score: 0,
  gpu_tier: 0,
  quality_pack_id: "gsm8k-lite",
  quality_pack_version: "1",
  quality_correct: 1,
  quality_total: 8,
  quality_accuracy: 0.125,
};

describe("v8 schema", () => {
  it("accepts a valid Ollama success event", () => ok(v8Success));
  it("accepts an event with no GPU detected", () =>
    ok({ ...v8Success, gpu_score: undefined, gpu_tier: undefined, vram_gb: undefined }));
  it("rejects missing cpu_score", () => rejected({ ...v8Success, cpu_score: undefined }));
  it("rejects a raw cpu_model field", () => rejected({ ...v8Success, cpu_model: "AMD Ryzen 5 5600X 6-Core Processor" }));
  it("rejects cpu_score above bound", () => rejected({ ...v8Success, cpu_score: 100_000 }));
  it("rejects cpu_tier above bound", () => rejected({ ...v8Success, cpu_tier: 11 }));

  it("accepts a valid LM Studio success event", () => ok(v8LmStudioSuccess));
  it.each([
    ["runtime_profile", "explicit_ollama_options"],
    ["context_length", 4096],
    ["gpu_offload_percent", 100],
    ["cpu_threads", 8],
    ["num_batch", 512],
  ])("rejects a fabricated %s on an LM Studio row", (field, value) =>
    rejected({ ...v8LmStudioSuccess, [field as string]: value }),
  );
  it.each([
    ["missing engine_version", { engine_version: undefined }],
    ["missing client_version", { client_version: undefined }],
    ["missing cpu_score", { cpu_score: undefined }],
    ["missing cpu_arch", { cpu_arch: undefined }],
    ["missing parameter_count_b", { parameter_count_b: undefined }],
    ["missing quant_bits", { quant_bits: undefined }],
    ["single-sample measurement", { sample_count: 1 }],
    ["active parameters above total", { active_parameter_count_b: 0.75 }],
    ["speed outside its own min/max", { tokens_per_sec: 200 }],
    ["more physical than logical cores", { cpu_physical_cores: 32 }],
    ["a raw cpu_model", { cpu_model: "Intel Core Ultra 9 285K" }],
    ["an unknown field", { lmstudio_note: "hello" }],
  ])("rejects LM Studio lane with %s", (_name, changes) => rejected({ ...v8LmStudioSuccess, ...changes }));

  it.each(["runtime_profile", "context_length", "gpu_offload_percent", "cpu_threads", "num_batch", "engine_version"])(
    "rejects Ollama success missing %s",
    (field) => rejected({ ...v8Success, [field]: undefined }),
  );

  it("accepts a valid model_unfit event", () =>
    ok({
      ram_gb: 24,
      vram_gb: 6,
      unified_memory: false,
      model_installed: "too-big:latest",
      engine: "ollama",
      benchmark_version: 8,
      recorded_at: "2026-07-30T00:00:00+00:00",
      outcome: "model_unfit",
      failure_reason: "out_of_memory",
    }));
  it("accepts a valid transient_error event", () =>
    ok({
      ram_gb: 24,
      unified_memory: false,
      model_installed: "small:latest",
      engine: "ollama",
      benchmark_version: 8,
      recorded_at: "2026-07-30T00:00:00+00:00",
      outcome: "transient_error",
      failure_reason: "ollama_unavailable",
    }));
});

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

describe("v9 schema (contribute-v1)", () => {
  it("accepts a valid success event", () => ok(v9Success));
  it("accepts optional GPU fields represented as JSON null", () =>
    ok({ ...v9Success, vram_gb: null, gpu_tflops: null }));
  it("rejects active parameter count above total", () =>
    rejected({ ...v9Success, parameter_count_b: 0.99989, active_parameter_count_b: 1.0 }));
  it("accepts a dense model whose active count equals its total", () =>
    ok({ ...v9Success, parameter_count_b: 0.99989, active_parameter_count_b: 0.99989 }));
  it("accepts a consistently labelled pressured measurement", () =>
    ok({ ...v9Success, measurement_quality: "pressured", memory_pressure_observed: true }));
  it("accepts a consistently labelled unstable measurement", () =>
    ok({ ...v9Success, measurement_quality: "unstable", tokens_per_sec_mad_ratio: 0.2 }));

  it.each([
    ["missing memory field", { ram_available_min_gb: undefined }],
    ["wrong context", { context_length: 4096 }],
    ["wrong batch", { num_batch: 512 }],
    ["clean high-MAD result", { tokens_per_sec_mad_ratio: 0.2 }],
    ["pressure without label", { memory_pressure_observed: true }],
    ["pressure label without pressure", { measurement_quality: "pressured" }],
    ["unknown estimate source", { memory_estimate_source: "guess" }],
  ])("rejects %s", (_name, changes) => rejected({ ...v9Success, ...changes }));

  it("accepts a loaded measurement with host load", () =>
    ok({ ...v9Success, measurement_quality: "loaded", host_cpu_load_percent: 47.5 }));
  it("accepts loaded exactly at the 25% threshold", () =>
    ok({ ...v9Success, measurement_quality: "loaded", host_cpu_load_percent: 25 }));
  it("accepts a clean measurement with a quiet host reading", () => ok({ ...v9Success, host_cpu_load_percent: 3.2 }));
  it("accepts pressured on a busy host", () =>
    ok({ ...v9Success, measurement_quality: "pressured", memory_pressure_observed: true, host_cpu_load_percent: 80 }));
  it("accepts unstable on a busy host", () =>
    ok({ ...v9Success, measurement_quality: "unstable", tokens_per_sec_mad_ratio: 0.2, host_cpu_load_percent: 80 }));

  it.each([
    ["loaded label without a host reading", { measurement_quality: "loaded" }],
    ["loaded label on a quiet host", { measurement_quality: "loaded", host_cpu_load_percent: 4 }],
    ["busy host still labelled clean", { host_cpu_load_percent: 61 }],
    [
      "loaded label outranking dispersion",
      { measurement_quality: "loaded", tokens_per_sec_mad_ratio: 0.4, host_cpu_load_percent: 61 },
    ],
    [
      "loaded label outranking memory pressure",
      { measurement_quality: "loaded", memory_pressure_observed: true, host_cpu_load_percent: 61 },
    ],
    ["host reading above 100", { host_cpu_load_percent: 140 }],
    ["negative host reading", { host_cpu_load_percent: -1 }],
    ["non-numeric host reading", { host_cpu_load_percent: "high" }],
  ])("rejects %s", (_name, changes) => rejected({ ...v9Success, ...changes }));

  it.each([
    ["clean", {}],
    ["pressured", { measurement_quality: "pressured", memory_pressure_observed: true }],
    ["unstable", { measurement_quality: "unstable", tokens_per_sec_mad_ratio: 0.2 }],
  ])("accepts old-shape %s row with no host reading", (_name, changes) => ok({ ...v9Success, ...changes }));

  it("rejects a failure event without measurement metadata", () =>
    rejected({
      ram_gb: 24,
      unified_memory: false,
      model_installed: "small:latest",
      engine: "ollama",
      benchmark_version: 9,
      recorded_at: "2026-07-30T00:00:00+00:00",
      outcome: "transient_error",
      failure_reason: "ollama_unavailable",
    }));
  it("rejects an LM Studio row", () => rejected({ ...v9Success, engine: "lmstudio" }));
  it("rejects an LM Studio row shaped like the relaxed v8 lane", () =>
    rejected({
      ...v9Success,
      engine: "lmstudio",
      runtime_profile: undefined,
      context_length: undefined,
      gpu_offload_percent: undefined,
      cpu_threads: undefined,
      num_batch: undefined,
    }));
  it("rejects missing engine_version", () => rejected({ ...v9Success, engine_version: undefined }));
});
