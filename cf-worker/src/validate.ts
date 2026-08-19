/**
 * Faithful TypeScript port of the `telemetry/$event` rules in
 * `database.rules.json`. The Worker writes via a service-account OAuth
 * token, which bypasses RTDB security rules entirely - so this file, not
 * the JSON rules, is now the only thing enforcing the telemetry schema.
 * Keep the two in sync; `database.rules.json` is left in place (with
 * `.write` denied) as the documented source of truth to diff against.
 */

export type TelemetryEvent = Record<string, unknown>;

function has(e: TelemetryEvent, key: string): boolean {
  return e[key] !== undefined && e[key] !== null;
}

function hasAll(e: TelemetryEvent, keys: string[]): boolean {
  return keys.every((k) => has(e, k));
}

function num(e: TelemetryEvent, key: string): number {
  const v = e[key];
  return typeof v === "number" && Number.isFinite(v) ? v : NaN;
}

function str(e: TelemetryEvent, key: string): string {
  const v = e[key];
  return typeof v === "string" ? v : "";
}

function bool(e: TelemetryEvent, key: string): boolean | undefined {
  const v = e[key];
  return typeof v === "boolean" ? v : undefined;
}

function isInt(n: number): boolean {
  return Number.isFinite(n) && n % 1 === 0;
}

// --- per-field validators -----------------------------------------------
// Applied only when the field is present (RTDB's per-field .validate never
// fires for a key that was never written - see #133 design notes). Each
// entry mirrors database.rules.json's rule for that field, minus its
// `!newData.exists() || (...)` guard (redundant here since absent fields
// are already skipped by the caller).

type FieldValidator = (e: TelemetryEvent) => boolean;

const FIELD_VALIDATORS: Record<string, FieldValidator> = {
  ram_gb: (e) => num(e, "ram_gb") >= 1 && num(e, "ram_gb") <= 1024,
  vram_gb: (e) => num(e, "vram_gb") >= 0 && num(e, "vram_gb") <= 512,
  unified_memory: (e) => bool(e, "unified_memory") !== undefined,
  gpu_tflops: (e) => num(e, "gpu_tflops") >= 0 && num(e, "gpu_tflops") <= 1000,
  model_installed: (e) => str(e, "model_installed").length > 0 && str(e, "model_installed").length <= 512,
  model_repo_id: (e) => str(e, "model_repo_id").length <= 512,
  model_provider: (e) => str(e, "model_provider").length > 0 && str(e, "model_provider").length <= 64,
  model_size_bytes: (e) => num(e, "model_size_bytes") > 0 && num(e, "model_size_bytes") <= 1099511627776,
  model_filename: (e) => {
    const v = str(e, "model_filename");
    return (
      v.length > 0 &&
      v.length <= 300 &&
      !v.includes("/") &&
      !v.includes("\\") &&
      !v.includes(":/")
    );
  },
  model_digest: (e) => /^[0-9a-f]{64}$/.test(str(e, "model_digest")),
  parameter_count_b: (e) => num(e, "parameter_count_b") > 0 && num(e, "parameter_count_b") <= 10000,
  active_parameter_count_b: (e) =>
    num(e, "active_parameter_count_b") > 0 && num(e, "active_parameter_count_b") <= 10000,
  quant_bits: (e) => num(e, "quant_bits") >= 0.5 && num(e, "quant_bits") <= 32,
  engine_version: (e) => str(e, "engine_version").length > 0 && str(e, "engine_version").length <= 100,
  client_version: (e) => str(e, "client_version").length > 0 && str(e, "client_version").length <= 100,
  engine: (e) => str(e, "engine") === "ollama" || str(e, "engine") === "lmstudio",
  benchmark_version: (e) => isInt(num(e, "benchmark_version")) && num(e, "benchmark_version") >= 1 && num(e, "benchmark_version") <= 9,
  outcome: (e) =>
    ["success", "model_unfit", "transient_error", "performance_unfit"].includes(str(e, "outcome")),
  failure_reason: (e) =>
    [
      "out_of_memory",
      "model_load_failed",
      "unsupported_runtime",
      "generation_timeout",
      "ollama_unavailable",
      "connection_error",
      "no_timing_metrics",
      "unknown",
      "confirmed_generation_timeout",
    ].includes(str(e, "failure_reason")),
  confirmation_attempts: (e) => num(e, "confirmation_attempts") === 2,
  timeout_seconds: (e) => num(e, "timeout_seconds") > 0 && num(e, "timeout_seconds") <= 3600,
  recorded_at: (e) => str(e, "recorded_at").length >= 20 && str(e, "recorded_at").length <= 50,
  tokens_per_sec: (e) => num(e, "tokens_per_sec") >= 0 && num(e, "tokens_per_sec") <= 1000,
  sample_count: (e) => isInt(num(e, "sample_count")) && num(e, "sample_count") >= 1 && num(e, "sample_count") <= 10,
  tokens_per_sec_min: (e) => num(e, "tokens_per_sec_min") >= 0 && num(e, "tokens_per_sec_min") <= 1000,
  tokens_per_sec_max: (e) => num(e, "tokens_per_sec_max") >= 0 && num(e, "tokens_per_sec_max") <= 1000,
  runtime_profile: (e) => str(e, "runtime_profile").length <= 32,
  context_length: (e) =>
    isInt(num(e, "context_length")) && num(e, "context_length") >= 256 && num(e, "context_length") <= 131072,
  gpu_offload_percent: (e) =>
    isInt(num(e, "gpu_offload_percent")) && num(e, "gpu_offload_percent") >= 0 && num(e, "gpu_offload_percent") <= 100,
  cpu_threads: (e) => isInt(num(e, "cpu_threads")) && num(e, "cpu_threads") >= 0 && num(e, "cpu_threads") <= 1024,
  num_batch: (e) => isInt(num(e, "num_batch")) && num(e, "num_batch") >= 0 && num(e, "num_batch") <= 65536,
  cpu_model: (e) =>
    num(e, "benchmark_version") < 8 && str(e, "cpu_model").length > 0 && str(e, "cpu_model").length <= 256,
  cpu_arch: (e) => str(e, "cpu_arch").length > 0 && str(e, "cpu_arch").length <= 64,
  cpu_physical_cores: (e) =>
    isInt(num(e, "cpu_physical_cores")) && num(e, "cpu_physical_cores") >= 1 && num(e, "cpu_physical_cores") <= 1024,
  cpu_logical_cores: (e) =>
    isInt(num(e, "cpu_logical_cores")) && num(e, "cpu_logical_cores") >= 1 && num(e, "cpu_logical_cores") <= 1024,
  cpu_score: (e) => num(e, "cpu_score") >= 0 && num(e, "cpu_score") <= 99999,
  cpu_tier: (e) => num(e, "cpu_tier") >= 0 && num(e, "cpu_tier") <= 10,
  gpu_score: (e) => num(e, "gpu_score") >= 0 && num(e, "gpu_score") <= 99999,
  gpu_tier: (e) => num(e, "gpu_tier") >= 0 && num(e, "gpu_tier") <= 10,
  quality_pack_id: (e) => str(e, "quality_pack_id").length > 0 && str(e, "quality_pack_id").length <= 100,
  quality_pack_version: (e) =>
    str(e, "quality_pack_version").length > 0 && str(e, "quality_pack_version").length <= 20,
  quality_correct: (e) =>
    isInt(num(e, "quality_correct")) && num(e, "quality_correct") >= 0 && num(e, "quality_correct") <= 100,
  quality_total: (e) =>
    isInt(num(e, "quality_total")) && num(e, "quality_total") >= 1 && num(e, "quality_total") <= 100,
  quality_accuracy: (e) => num(e, "quality_accuracy") >= 0 && num(e, "quality_accuracy") <= 1,
  os: (e) => num(e, "benchmark_version") < 3 && str(e, "os").length <= 128,
  cpu: (e) => num(e, "benchmark_version") < 3 && str(e, "cpu").length <= 256,
  gpu: (e) => num(e, "benchmark_version") < 3 && str(e, "gpu").length <= 256,
  measurement_profile: (e) => str(e, "measurement_profile") === "contribute-v1",
  measurement_quality: (e) =>
    ["clean", "pressured", "unstable", "loaded"].includes(str(e, "measurement_quality")),
  ram_available_before_gb: (e) =>
    num(e, "ram_available_before_gb") >= 0 && num(e, "ram_available_before_gb") <= 1024,
  ram_available_min_gb: (e) => num(e, "ram_available_min_gb") >= 0 && num(e, "ram_available_min_gb") <= 1024,
  ram_available_after_gb: (e) =>
    num(e, "ram_available_after_gb") >= 0 && num(e, "ram_available_after_gb") <= 1024,
  memory_pressure_observed: (e) => bool(e, "memory_pressure_observed") !== undefined,
  tokens_per_sec_mad_ratio: (e) =>
    num(e, "tokens_per_sec_mad_ratio") >= 0 && num(e, "tokens_per_sec_mad_ratio") <= 10,
  host_cpu_load_percent: (e) =>
    num(e, "host_cpu_load_percent") >= 0 && num(e, "host_cpu_load_percent") <= 100,
  memory_estimate_source: (e) =>
    str(e, "memory_estimate_source") === "gguf_header" || str(e, "memory_estimate_source") === "profile_fallback",
  memory_estimate_confidence: (e) =>
    ["low", "medium", "high"].includes(str(e, "memory_estimate_confidence")),
  estimated_mapped_weights_gb: (e) =>
    num(e, "estimated_mapped_weights_gb") >= 0 && num(e, "estimated_mapped_weights_gb") <= 1024,
  estimated_committed_ram_gb: (e) =>
    num(e, "estimated_committed_ram_gb") >= 0 && num(e, "estimated_committed_ram_gb") <= 1024,
  estimated_required_vram_gb: (e) =>
    num(e, "estimated_required_vram_gb") >= 0 && num(e, "estimated_required_vram_gb") <= 1024,
};

const KNOWN_FIELDS = new Set(Object.keys(FIELD_VALIDATORS));

// --- benchmark_version branch combinators --------------------------------

function branchBase(e: TelemetryEvent): boolean {
  const bv = num(e, "benchmark_version");
  if (
    !hasAll(e, ["ram_gb", "unified_memory", "model_installed", "engine", "benchmark_version", "recorded_at", "tokens_per_sec"])
  ) {
    return false;
  }
  if (str(e, "engine") !== "ollama" && str(e, "engine") !== "lmstudio") return false;
  if (!(bv >= 1 && bv <= 6)) return false;

  if (bv === 5 || bv === 6) {
    const runtimeFields = [
      "parameter_count_b",
      "active_parameter_count_b",
      "quant_bits",
      "engine_version",
      "client_version",
      "runtime_profile",
      "context_length",
      "gpu_offload_percent",
      "cpu_threads",
      "num_batch",
      "sample_count",
      "tokens_per_sec_min",
      "tokens_per_sec_max",
    ];
    if (!hasAll(e, runtimeFields)) return false;
    if (bv === 6) {
      if (!hasAll(e, ["cpu_model", "cpu_arch", "cpu_physical_cores", "cpu_logical_cores"])) return false;
      if (!(num(e, "cpu_physical_cores") <= num(e, "cpu_logical_cores"))) return false;
    }
    if (!(num(e, "active_parameter_count_b") <= num(e, "parameter_count_b"))) return false;
    if (!(str(e, "runtime_profile").length > 0)) return false;
    if (!(num(e, "context_length") >= 256 && num(e, "context_length") <= 131072)) return false;
    if (!(num(e, "cpu_threads") >= 1 && num(e, "cpu_threads") <= 1024)) return false;
    if (!(num(e, "num_batch") >= 1 && num(e, "num_batch") <= 65536)) return false;
    if (!(num(e, "sample_count") >= 3)) return false;
    if (!(num(e, "tokens_per_sec_min") <= num(e, "tokens_per_sec") && num(e, "tokens_per_sec") <= num(e, "tokens_per_sec_max"))) {
      return false;
    }
  }

  const hasAnyQuality =
    has(e, "quality_pack_id") ||
    has(e, "quality_pack_version") ||
    has(e, "quality_correct") ||
    has(e, "quality_total") ||
    has(e, "quality_accuracy");
  if (hasAnyQuality) {
    if (!hasAll(e, ["quality_pack_id", "quality_pack_version", "quality_correct", "quality_total", "quality_accuracy"])) {
      return false;
    }
    const correct = num(e, "quality_correct");
    const total = num(e, "quality_total");
    const accuracy = num(e, "quality_accuracy");
    if (!(correct <= total)) return false;
    if (!(accuracy >= correct / total - 0.0001 && accuracy <= correct / total + 0.0001)) return false;
  }
  return true;
}

const TRANSIENT_REASONS = [
  "model_load_failed",
  "generation_timeout",
  "ollama_unavailable",
  "connection_error",
  "no_timing_metrics",
  "unknown",
];

function outcomeShapeCommon(e: TelemetryEvent): boolean {
  return !(
    has(e, "tokens_per_sec") ||
    has(e, "tokens_per_sec_min") ||
    has(e, "tokens_per_sec_max") ||
    has(e, "sample_count")
  );
}

function modelUnfitBranch(e: TelemetryEvent): boolean {
  return (
    str(e, "outcome") === "model_unfit" &&
    has(e, "failure_reason") &&
    (str(e, "failure_reason") === "out_of_memory" || str(e, "failure_reason") === "unsupported_runtime") &&
    outcomeShapeCommon(e) &&
    !has(e, "confirmation_attempts") &&
    !has(e, "timeout_seconds")
  );
}

function transientErrorBranch(e: TelemetryEvent): boolean {
  return (
    str(e, "outcome") === "transient_error" &&
    has(e, "failure_reason") &&
    TRANSIENT_REASONS.includes(str(e, "failure_reason")) &&
    outcomeShapeCommon(e) &&
    !has(e, "confirmation_attempts") &&
    !has(e, "timeout_seconds")
  );
}

function performanceUnfitBranch(e: TelemetryEvent): boolean {
  return (
    str(e, "outcome") === "performance_unfit" &&
    has(e, "failure_reason") &&
    str(e, "failure_reason") === "confirmed_generation_timeout" &&
    has(e, "confirmation_attempts") &&
    num(e, "confirmation_attempts") === 2 &&
    has(e, "timeout_seconds") &&
    outcomeShapeCommon(e)
  );
}

function branchV7(e: TelemetryEvent): boolean {
  if (num(e, "benchmark_version") !== 7) return false;
  if (!hasAll(e, ["ram_gb", "unified_memory", "model_installed", "engine", "benchmark_version", "recorded_at", "outcome"])) {
    return false;
  }
  if (str(e, "engine") !== "ollama" && str(e, "engine") !== "lmstudio") return false;

  if (str(e, "outcome") === "success") {
    const required = [
      "tokens_per_sec",
      "parameter_count_b",
      "active_parameter_count_b",
      "quant_bits",
      "engine_version",
      "client_version",
      "runtime_profile",
      "context_length",
      "gpu_offload_percent",
      "cpu_threads",
      "num_batch",
      "sample_count",
      "tokens_per_sec_min",
      "tokens_per_sec_max",
      "cpu_model",
      "cpu_arch",
      "cpu_physical_cores",
      "cpu_logical_cores",
    ];
    if (!hasAll(e, required)) return false;
    if (has(e, "failure_reason")) return false;
    if (!(num(e, "cpu_physical_cores") <= num(e, "cpu_logical_cores"))) return false;
    if (!(num(e, "active_parameter_count_b") <= num(e, "parameter_count_b"))) return false;
    if (!(str(e, "runtime_profile").length > 0)) return false;
    if (!(num(e, "context_length") >= 256 && num(e, "context_length") <= 131072)) return false;
    if (!(num(e, "cpu_threads") >= 1 && num(e, "cpu_threads") <= 1024)) return false;
    if (!(num(e, "num_batch") >= 1 && num(e, "num_batch") <= 65536)) return false;
    if (!(num(e, "sample_count") >= 3)) return false;
    if (!(num(e, "tokens_per_sec_min") <= num(e, "tokens_per_sec") && num(e, "tokens_per_sec") <= num(e, "tokens_per_sec_max"))) {
      return false;
    }
    if (has(e, "confirmation_attempts") || has(e, "timeout_seconds")) return false;
    return true;
  }
  return modelUnfitBranch(e) || transientErrorBranch(e) || performanceUnfitBranch(e);
}

function branchV8(e: TelemetryEvent): boolean {
  if (num(e, "benchmark_version") !== 8) return false;
  if (!hasAll(e, ["ram_gb", "unified_memory", "model_installed", "engine", "benchmark_version", "recorded_at", "outcome"])) {
    return false;
  }
  if (str(e, "engine") !== "ollama" && str(e, "engine") !== "lmstudio") return false;

  if (str(e, "outcome") === "success") {
    const required = [
      "tokens_per_sec",
      "parameter_count_b",
      "active_parameter_count_b",
      "quant_bits",
      "engine_version",
      "client_version",
      "sample_count",
      "tokens_per_sec_min",
      "tokens_per_sec_max",
      "cpu_score",
      "cpu_tier",
      "cpu_arch",
      "cpu_physical_cores",
      "cpu_logical_cores",
    ];
    if (!hasAll(e, required)) return false;
    if (has(e, "failure_reason")) return false;
    if (!(num(e, "cpu_physical_cores") <= num(e, "cpu_logical_cores"))) return false;
    if (!(num(e, "active_parameter_count_b") <= num(e, "parameter_count_b"))) return false;
    if (!(num(e, "sample_count") >= 3)) return false;
    if (!(num(e, "tokens_per_sec_min") <= num(e, "tokens_per_sec") && num(e, "tokens_per_sec") <= num(e, "tokens_per_sec_max"))) {
      return false;
    }
    if (has(e, "confirmation_attempts") || has(e, "timeout_seconds")) return false;

    const engine = str(e, "engine");
    if (engine === "ollama") {
      const runtimeFields = ["runtime_profile", "context_length", "gpu_offload_percent", "cpu_threads", "num_batch"];
      if (!hasAll(e, runtimeFields)) return false;
      if (!(str(e, "runtime_profile").length > 0)) return false;
      if (!(num(e, "context_length") >= 256 && num(e, "context_length") <= 131072)) return false;
      if (!(num(e, "cpu_threads") >= 1 && num(e, "cpu_threads") <= 1024)) return false;
      if (!(num(e, "num_batch") >= 1 && num(e, "num_batch") <= 65536)) return false;
    } else if (engine === "lmstudio") {
      if (
        has(e, "runtime_profile") ||
        has(e, "context_length") ||
        has(e, "gpu_offload_percent") ||
        has(e, "cpu_threads") ||
        has(e, "num_batch")
      ) {
        return false;
      }
    } else {
      return false;
    }
    return true;
  }
  return modelUnfitBranch(e) || transientErrorBranch(e) || performanceUnfitBranch(e);
}

function branchV9(e: TelemetryEvent): boolean {
  if (num(e, "benchmark_version") !== 9) return false;
  const required = [
    "ram_gb",
    "unified_memory",
    "model_installed",
    "engine",
    "benchmark_version",
    "recorded_at",
    "outcome",
    "tokens_per_sec",
    "parameter_count_b",
    "active_parameter_count_b",
    "quant_bits",
    "engine_version",
    "client_version",
    "runtime_profile",
    "context_length",
    "gpu_offload_percent",
    "cpu_threads",
    "num_batch",
    "sample_count",
    "tokens_per_sec_min",
    "tokens_per_sec_max",
    "cpu_score",
    "cpu_tier",
    "cpu_arch",
    "cpu_physical_cores",
    "cpu_logical_cores",
    "measurement_profile",
    "measurement_quality",
    "ram_available_before_gb",
    "ram_available_min_gb",
    "ram_available_after_gb",
    "memory_pressure_observed",
    "tokens_per_sec_mad_ratio",
    "memory_estimate_source",
    "memory_estimate_confidence",
    "estimated_mapped_weights_gb",
    "estimated_committed_ram_gb",
    "estimated_required_vram_gb",
  ];
  if (!hasAll(e, required)) return false;
  if (str(e, "engine") !== "ollama") return false;
  if (str(e, "outcome") !== "success") return false;
  if (has(e, "failure_reason")) return false;
  if (!(num(e, "cpu_physical_cores") <= num(e, "cpu_logical_cores"))) return false;
  if (!(num(e, "active_parameter_count_b") <= num(e, "parameter_count_b"))) return false;
  if (!(str(e, "runtime_profile").length > 0)) return false;
  if (num(e, "context_length") !== 1024) return false;
  if (!(num(e, "cpu_threads") >= 1 && num(e, "cpu_threads") <= 1024)) return false;
  if (num(e, "num_batch") !== 128) return false;
  if (!(num(e, "sample_count") >= 3)) return false;
  if (!(num(e, "tokens_per_sec_min") <= num(e, "tokens_per_sec") && num(e, "tokens_per_sec") <= num(e, "tokens_per_sec_max"))) {
    return false;
  }
  if (str(e, "measurement_profile") !== "contribute-v1") return false;

  const quality = str(e, "measurement_quality");
  const validEnumQuality =
    quality === "clean" || quality === "pressured" || quality === "unstable" || (quality === "loaded" && has(e, "host_cpu_load_percent"));
  if (!validEnumQuality) return false;

  if (!(num(e, "ram_available_min_gb") <= num(e, "ram_available_before_gb"))) return false;
  if (!(num(e, "ram_available_min_gb") <= num(e, "ram_available_after_gb"))) return false;

  const pressureObserved = bool(e, "memory_pressure_observed");
  const madRatio = num(e, "tokens_per_sec_mad_ratio");
  const hostLoad = has(e, "host_cpu_load_percent") ? num(e, "host_cpu_load_percent") : undefined;

  let consistencyOk = false;
  if (pressureObserved === true) {
    consistencyOk = quality === "pressured";
  } else if (pressureObserved === false) {
    if (madRatio > 0.15) {
      consistencyOk = quality === "unstable";
    } else {
      if (hostLoad === undefined) {
        consistencyOk = quality === "clean";
      } else if (hostLoad < 25) {
        consistencyOk = quality === "clean";
      } else {
        consistencyOk = quality === "loaded";
      }
    }
  }
  if (!consistencyOk) return false;

  if (str(e, "memory_estimate_source") !== "gguf_header" && str(e, "memory_estimate_source") !== "profile_fallback") {
    return false;
  }
  const confidence = str(e, "memory_estimate_confidence");
  if (confidence !== "medium" && confidence !== "low" && confidence !== "high") return false;

  return true;
}

export function validateTelemetryEvent(event: TelemetryEvent): { valid: boolean; reason?: string } {
  for (const key of Object.keys(event)) {
    if (!KNOWN_FIELDS.has(key)) {
      return { valid: false, reason: `unknown field: ${key}` };
    }
  }
  for (const [key, validator] of Object.entries(FIELD_VALIDATORS)) {
    if (has(event, key) && !validator(event)) {
      return { valid: false, reason: `field failed validation: ${key}` };
    }
  }
  if (branchBase(event) || branchV7(event) || branchV8(event) || branchV9(event)) {
    return { valid: true };
  }
  return { valid: false, reason: "no benchmark_version branch matched" };
}
