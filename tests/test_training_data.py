from __future__ import annotations

import json
import subprocess
import sys
from argparse import Namespace
from types import SimpleNamespace

import pytest

pytest.importorskip("sklearn")

from scripts import train_model
from scripts.model_quality_gate import validate_dataset


def test_training_script_can_run_directly_from_outside_repository(tmp_path):
    result = subprocess.run(
        [sys.executable, str(train_model.__file__), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--quality-gate" in result.stdout


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def _row(speed: float, **overrides) -> dict:
    row = {
        "engine": "ollama",
        "benchmark_version": 2,
        "tokens_per_sec": speed,
        "ram_gb": 16,
        "vram_gb": 8,
        "gpu_tflops": 20,
        "unified_memory": False,
        "model_installed": "model-7B-Q4.gguf",
        "model_repo_id": "org/model-7B",
        "model_size_bytes": 4 * 1024**3,
        "context_length": 4096,
        "gpu_offload_percent": 100,
        "cpu_threads": 8,
        "num_batch": 512,
    }
    row.update(overrides)
    return row


def test_repeated_configuration_is_collapsed_to_median_speed():
    X, y = train_model.real_rows_to_training_data([_v6_row(10), _v6_row(100), _v6_row(20)])

    assert len(X) == 1
    assert y == [20]


def test_unknown_benchmark_schema_is_ignored():
    X, y = train_model.real_rows_to_training_data([_row(20, benchmark_version=999)])

    assert X == []
    assert y == []


def test_privacy_minimized_schema_three_is_rejected_for_missing_hardware_identity():
    # Schema v3 predates cpu_model collection entirely - a valid row here
    # is indistinguishable from any other pre-v6 hardware, so it's excluded
    # rather than silently scored as "unknown" (see no_hardware_identity_
    # pre_v6_schema in _extract_features_and_reason).
    row = _row(20, benchmark_version=3)
    row.pop("gpu", None)
    row.pop("cpu", None)
    row.pop("os", None)

    X, y, audit = train_model.real_rows_to_training_data_with_audit([row])

    assert X == [] and y == []
    assert audit["rejections"] == {"no_hardware_identity_pre_v6_schema": 1}


def test_multi_sample_schema_four_is_rejected_for_missing_hardware_identity():
    X, y, audit = train_model.real_rows_to_training_data_with_audit(
        [
            _row(
                20,
                benchmark_version=4,
                sample_count=3,
                tokens_per_sec_min=19,
                tokens_per_sec_max=21,
            )
        ]
    )

    assert X == [] and y == []
    assert audit["rejections"] == {"no_hardware_identity_pre_v6_schema": 1}


def test_inconsistent_schema_four_sample_summary_is_rejected():
    X, y, audit = train_model.real_rows_to_training_data_with_audit(
        [
            _row(
                20,
                benchmark_version=4,
                sample_count=3,
                tokens_per_sec_min=30,
                tokens_per_sec_max=40,
            )
        ]
    )

    assert X == [] and y == []
    assert audit["rejections"] == {"invalid_samples": 1}


def test_v6_uses_direct_metadata_without_parsing_the_model_name():
    row = _row(
        20,
        benchmark_version=6,
        model_installed="unparseable-name.bin",
        model_repo_id="org/unparseable",
        model_size_bytes=None,
        parameter_count_b=8.0,
        active_parameter_count_b=2.0,
        quant_bits=4.0,
        engine_version="1.0",
        client_version="1.0",
        runtime_profile="throughput",
        cpu_model="AMD Ryzen 5 5600X",
        cpu_arch="x86_64",
        cpu_physical_cores=6,
        cpu_logical_cores=12,
        sample_count=3,
        tokens_per_sec_min=19,
        tokens_per_sec_max=21,
    )

    X, y = train_model.real_rows_to_training_data([row])

    assert len(X) == 1
    assert y == [20]


def test_v6_rejects_missing_or_invalid_direct_metadata_without_name_fallback():
    base = dict(
        benchmark_version=6,
        model_installed="model-7B-Q4.gguf",
        parameter_count_b=7.0,
        active_parameter_count_b=3.0,
        quant_bits=4.0,
        engine_version="1.0",
        client_version="1.0",
        runtime_profile="throughput",
        cpu_model="AMD Ryzen 5 5600X",
        cpu_arch="x86_64",
        cpu_physical_cores=6,
        cpu_logical_cores=12,
        sample_count=3,
        tokens_per_sec_min=19,
        tokens_per_sec_max=21,
    )
    missing_metadata = {**base, "parameter_count_b": None}
    _, reason = train_model._real_row_to_sample(_row(20, **missing_metadata))
    assert reason == "missing_model_metadata"
    invalid_metadata = {**base, "parameter_count_b": "7"}
    _, reason = train_model._real_row_to_sample(_row(20, **invalid_metadata))
    assert reason == "invalid_model_metadata"
    missing_runtime = {**base, "cpu_threads": None}
    _, reason = train_model._real_row_to_sample(_row(20, **missing_runtime))
    assert reason == "missing_runtime_metadata"


def _v8_row(speed: float, **overrides) -> dict:
    defaults = dict(
        benchmark_version=8,
        outcome="success",
        parameter_count_b=7.0,
        active_parameter_count_b=3.0,
        quant_bits=4.0,
        engine_version="1.0",
        client_version="1.0",
        runtime_profile="throughput",
        cpu_score=7950.0,
        cpu_tier=1.0,
        cpu_arch="x86_64",
        cpu_physical_cores=8,
        cpu_logical_cores=16,
        gpu_score=4090.0,
        gpu_tier=0.0,
        sample_count=3,
        tokens_per_sec_min=speed - 1,
        tokens_per_sec_max=speed + 1,
    )
    defaults.update(overrides)
    return _row(speed, **defaults)


def _v9_row(speed: float, **overrides) -> dict:
    defaults = dict(
        benchmark_version=9,
        context_length=1024,
        num_batch=128,
        measurement_profile="contribute-v1",
        measurement_quality="clean",
        ram_available_before_gb=2.2,
        ram_available_min_gb=2.0,
        ram_available_after_gb=2.1,
        memory_pressure_observed=False,
        tokens_per_sec_mad_ratio=0.02,
        memory_estimate_source="gguf_header",
        memory_estimate_confidence="medium",
        estimated_mapped_weights_gb=4.0,
        estimated_committed_ram_gb=0.3,
        estimated_required_vram_gb=4.2,
    )
    defaults.update(overrides)
    return _v8_row(speed, **defaults)


def test_v9_clean_measurement_trains_but_pressured_and_unstable_do_not():
    rows = [
        _v9_row(20),
        _v9_row(
            19,
            measurement_quality="pressured",
            memory_pressure_observed=True,
        ),
        _v9_row(
            18,
            measurement_quality="unstable",
            tokens_per_sec_mad_ratio=0.2,
        ),
    ]

    _X, y, audit = train_model.real_rows_to_training_data_with_audit(rows)

    assert y == [20]
    assert audit["direct_v9_unique_configurations"] == 1
    assert audit["rejections"] == {
        "pressured_measurement_excluded": 1,
        "unstable_measurement_excluded": 1,
    }


def test_v8_uses_direct_cpu_and_gpu_score_without_a_raw_name():
    X, y = train_model.real_rows_to_training_data([_v8_row(20)])

    assert len(X) == 1
    assert y == [20]


def test_v8_rejects_missing_cpu_score():
    row = _v8_row(20, cpu_score=None)

    _, reason = train_model._real_row_to_sample(row)

    assert reason == "missing_cpu_metadata"


def test_v8_gpu_score_defaults_to_zero_when_no_gpu_was_detected():
    with_gpu, reason1 = train_model._real_row_to_sample(_v8_row(20))
    without_gpu, reason2 = train_model._real_row_to_sample(
        _v8_row(20, gpu_score=None, gpu_tier=None, vram_gb=None)
    )

    assert reason1 is None and reason2 is None
    gpu_score_index = train_model.FEATURE_ORDER.index("gpu_score")
    assert with_gpu[0][gpu_score_index] == 4090.0
    assert without_gpu[0][gpu_score_index] == 0.0


def test_v8_success_outcome_required_like_v7():
    row = _v8_row(20, outcome="bogus")

    _, reason = train_model._real_row_to_sample(row)

    assert reason == "invalid_outcome"


def _v6_row(speed: float, **overrides) -> dict:
    return _row(
        speed,
        benchmark_version=6,
        parameter_count_b=7.0,
        active_parameter_count_b=3.0,
        quant_bits=4.0,
        engine_version="1.0",
        client_version="1.0",
        runtime_profile="throughput",
        cpu_model="AMD Ryzen 5 5600X",
        cpu_arch="x86_64",
        cpu_physical_cores=6,
        cpu_logical_cores=12,
        sample_count=3,
        tokens_per_sec_min=speed - 1,
        tokens_per_sec_max=speed + 1,
        **overrides,
    )


def test_quality_gate_rejects_legacy_only_configurations():
    # Legacy (pre-v6) rows carry no hardware identity at all, so they're
    # excluded before ever reaching unique_configurations - a legacy-only
    # telemetry batch trains on nothing.
    _X, _y, audit = train_model.real_rows_to_training_data_with_audit(
        [_row(10), _row(20, vram_gb=6)]
    )

    assert audit["unique_configurations"] == 0
    assert audit["rejections"] == {"no_hardware_identity_pre_v6_schema": 2}
    with pytest.raises(ValueError, match="too few unique direct-v6"):
        validate_dataset(audit, min_unique_configurations=1)


def test_direct_v6_duplicate_configurations_are_collapsed_for_the_gate():
    _X, _y, audit = train_model.real_rows_to_training_data_with_audit(
        [_v6_row(10), _v6_row(20)]
    )

    assert audit["unique_configurations"] == 1
    assert audit["direct_v6_unique_configurations"] == 1
    with pytest.raises(ValueError, match="too few unique direct-v6"):
        validate_dataset(audit, min_unique_configurations=2)


def test_supported_engines_are_kept_as_distinct_training_configurations():
    rows = [
        _v6_row(20, engine="ollama"),
        _v6_row(21, engine="llama.cpp"),
        _v6_row(19, engine="lmstudio"),
    ]

    X, y = train_model.real_rows_to_training_data(rows)
    llama_index = train_model.FEATURE_ORDER.index("engine_llamacpp")
    lmstudio_index = train_model.FEATURE_ORDER.index("engine_lmstudio")

    assert len(X) == 3
    assert sorted(y) == [19, 20, 21]
    assert {(features[llama_index], features[lmstudio_index]) for features in X} == {
        (0.0, 0.0),
        (1.0, 0.0),
        (0.0, 1.0),
    }


def test_training_audit_explains_rejections_and_duplicate_collapse():
    rows = [
        _v6_row(10),
        _v6_row(20),
        _row(30, engine="other"),
        _row(40, ram_gb="not-a-number"),
        _row(50, model_installed="unknown.gguf", model_repo_id="org/unknown"),
    ]

    X, y, audit = train_model.real_rows_to_training_data_with_audit(rows)

    assert len(X) == 1
    assert y == [15]
    assert audit == {
        "raw_rows": 5,
        "valid_rows": 2,
        "rejected_rows": 3,
        "samples_used": 2,
        "samples_capped": 0,
        "unique_configurations": 1,
        "direct_v6_unique_configurations": 1,
        "direct_v7_unique_configurations": 0,
        "direct_v8_unique_configurations": 0,
        "direct_v9_unique_configurations": 0,
        "direct_unique_configurations": 1,
        "duplicates_collapsed": 1,
        "rejections": {
            "invalid_measurement": 1,
            "unparseable_model": 1,
            "unsupported_engine": 1,
        },
    }


def test_bootstrap_grid_distinguishes_small_and_large_models():
    X, y = train_model.synthetic_rows_from_rules()
    indexes = {
        name: train_model.FEATURE_ORDER.index(name)
        for name in (
            "ram_gb",
            "vram_gb",
            "unified_memory",
            "param_count_b",
            "quant_bits",
            "context_length",
            "active_param_count_b",
        )
    }

    def speed_for(parameters: float) -> float:
        for features, speed in zip(X, y):
            if (
                features[indexes["ram_gb"]] == 24
                and features[indexes["vram_gb"]] == 24
                and features[indexes["unified_memory"]] == 1
                and features[indexes["param_count_b"]] == parameters
                and features[indexes["quant_bits"]] == 4
                and features[indexes["context_length"]] == 8192
            ):
                return speed
        raise AssertionError("bootstrap point not found")

    assert speed_for(0.5) > speed_for(1.5) > speed_for(7.0)


def test_bootstrap_grid_models_moe_active_parameters_separately():
    X, y = train_model.synthetic_rows_from_rules()
    indexes = {name: train_model.FEATURE_ORDER.index(name) for name in train_model.FEATURE_ORDER}

    def speed_for(total: float, active: float) -> float:
        for features, speed in zip(X, y):
            if (
                features[indexes["ram_gb"]] == 24
                and features[indexes["vram_gb"]] == 24
                and features[indexes["param_count_b"]] == total
                and features[indexes["active_param_count_b"]] == active
                and features[indexes["quant_bits"]] == 4
                and features[indexes["context_length"]] == 4096
            ):
                return speed
        raise AssertionError("bootstrap point not found")

    assert speed_for(30.0, 3.0) > speed_for(27.0, 27.0)


def test_load_telemetry_file_accepts_local_jsonl(tmp_path):
    path = tmp_path / "benchmarks.jsonl"
    path.write_text("\n".join(json.dumps(_row(speed)) for speed in (10, 20)) + "\n")

    rows = train_model.load_telemetry_file(path)

    assert [row["tokens_per_sec"] for row in rows] == [10, 20]


def test_load_telemetry_file_accepts_firebase_mapping(tmp_path):
    path = tmp_path / "firebase.json"
    path.write_text(json.dumps({"push-a": _row(10), "push-b": _row(20)}))

    rows = train_model.load_telemetry_file(path)

    assert len(rows) == 2


def test_load_telemetry_file_accepts_self_hosted_export(tmp_path):
    path = tmp_path / "server-export.json"
    path.write_text(json.dumps({"count": 2, "benchmarks": [_row(10), _row(20)]}))

    rows = train_model.load_telemetry_file(path)

    assert [row["tokens_per_sec"] for row in rows] == [10, 20]


def test_fetch_real_rows_accepts_authenticated_self_hosted_export(monkeypatch):
    captured = {}
    monkeypatch.setenv("LOCALFIT_ADMIN_TOKEN", "secret")

    def fake_get(url, headers, timeout):
        captured.update(url=url, headers=headers, timeout=timeout)
        return _Response({"count": 1, "benchmarks": [_row(22, engine="llama.cpp")]})

    monkeypatch.setattr(train_model.requests, "get", fake_get)

    rows = train_model.fetch_real_rows("https://collector.example/v1/benchmarks/export")

    assert rows[0]["engine"] == "llama.cpp"
    assert captured["headers"] == {"Authorization": "Bearer secret"}


def test_fetch_real_rows_omits_authorization_for_firebase_with_token(monkeypatch):
    captured = {}
    monkeypatch.setenv("LOCALFIT_ADMIN_TOKEN", "secret")

    def fake_get(url, headers, timeout):
        captured.update(url=url, headers=headers, timeout=timeout)
        return _Response([_row(22)])

    monkeypatch.setattr(train_model.requests, "get", fake_get)

    train_model.fetch_real_rows("https://project.firebaseio.com/benchmarks.json")

    assert "Authorization" not in captured["headers"]


def test_fetch_real_rows_omits_authorization_when_token_is_unset(monkeypatch):
    captured = {}
    monkeypatch.delenv("LOCALFIT_ADMIN_TOKEN", raising=False)

    def fake_get(url, headers, timeout):
        captured.update(url=url, headers=headers, timeout=timeout)
        return _Response([_row(22)])

    monkeypatch.setattr(train_model.requests, "get", fake_get)

    train_model.fetch_real_rows("https://project.firebaseio.com/benchmarks.json")

    assert "Authorization" not in captured["headers"]


@pytest.mark.parametrize(
    "url",
    [
        "https://project.firebaseio.com/benchmarks.json",
        "https://project-default-rtdb.firebasedatabase.app/benchmarks.json",
    ],
)
def test_firebase_rtdb_json_urls_are_recognized(url):
    assert train_model.is_firebase_realtime_database_json_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://firebaseio.com/benchmarks.json",
        "https://project.firebaseio.com.evil.example/benchmarks.json",
        "https://project.firebaseio.com/v1/benchmarks/export",
    ],
)
def test_non_firebase_rtdb_json_urls_are_not_recognized(url):
    assert not train_model.is_firebase_realtime_database_json_url(url)


def test_load_telemetry_file_rejects_malformed_jsonl(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps(_row(10)) + "\nnot-json\n")

    with pytest.raises(ValueError, match=":2 is not valid JSON"):
        train_model.load_telemetry_file(path)


def test_stable_holdout_split_is_independent_of_input_order():
    def row(ram, parameters):
        values = {name: 0.0 for name in train_model.FEATURE_ORDER}
        values["ram_gb"] = ram
        values["param_count_b"] = parameters
        values["quant_bits"] = 4.0
        values["model_size_gb"] = parameters * 0.5
        values["active_param_count_b"] = parameters
        return [values[name] for name in train_model.FEATURE_ORDER]
    X = [row(16.0, 3.0), row(16.0, 7.0), row(32.0, 3.0), row(32.0, 7.0)]
    y = [20.0, 10.0, 30.0, 40.0]

    first = train_model.stable_holdout_split(X, y, 0.25)
    second = train_model.stable_holdout_split(list(reversed(X)), list(reversed(y)), 0.25)

    assert first == second


def test_stable_holdout_split_keeps_sibling_candidates_atomic():
    def row(ram, parameters):
        values = {name: 0.0 for name in train_model.FEATURE_ORDER}
        values["ram_gb"] = ram
        values["param_count_b"] = parameters
        values["quant_bits"] = 4.0
        values["model_size_gb"] = parameters * 0.5
        values["active_param_count_b"] = parameters
        values["gpu_offload_ratio"] = parameters / 10
        return [values[name] for name in train_model.FEATURE_ORDER]
    X = [row(16.0, 3.0), row(16.0, 7.0), row(32.0, 3.0), row(32.0, 7.0)]
    train_X, _train_y, holdout_X, _holdout_y = train_model.stable_holdout_split(X, [1.0] * 4, 0.25)
    train_contexts = {train_model.selection_context_key(train_model.FEATURE_ORDER, x) for x in train_X}
    holdout_contexts = {train_model.selection_context_key(train_model.FEATURE_ORDER, x) for x in holdout_X}

    assert train_contexts.isdisjoint(holdout_contexts)
    assert len(holdout_X) == 2


def test_quality_gate_split_rejects_too_few_selection_contexts():
    with pytest.raises(ValueError, match="at least two selection contexts"):
        train_model.stable_holdout_split([[0.0] * len(train_model.FEATURE_ORDER)], [1.0], 0.2)


def test_quality_gate_regression_republishes_baseline_unchanged(tmp_path, monkeypatch):
    telemetry = tmp_path / "telemetry.json"
    telemetry.write_text(json.dumps([_v6_row(10), _v6_row(20, vram_gb=6)]))
    output = tmp_path / "model.json"
    output.write_text("incumbent-output")
    baseline = tmp_path / "baseline.json"
    monkeypatch.setattr(train_model, "load_candidates", lambda: [])
    X, y = train_model.real_rows_to_training_data(json.loads(telemetry.read_text()))
    baseline.write_text(
        json.dumps(
            train_model.train_artifact(
                X, y, sample_weight=None, training_mode="telemetry", bootstrap_method=None,
                real_rows=[], telemetry_audit={"unique_configurations": len(X)},
                input_sources=[], evaluation=None,
            )
        )
    )
    monkeypatch.setattr(
        train_model,
        "compare_artifacts",
        lambda *_args, **_kwargs: {"passed": False, "failures": ["forced"]},
    )
    monkeypatch.setattr(
        train_model,
        "parse_args",
        lambda: Namespace(
            telemetry_file=[telemetry], offline=True, telemetry_url="", output=output,
            baseline=baseline, quality_gate=True, minimum_real_configurations=0,
            maximum_rejection_rate=0.25, holdout_fraction=0.2, quality_report=None,
            minimum_fit_negative_examples=5,
        ),
    )

    train_model.main()  # must not raise: gate rejection is expected, not a code bug

    assert output.read_text() == baseline.read_text()


def test_quality_gate_insufficient_selection_groups_republishes_baseline_unchanged(
    tmp_path, monkeypatch, capsys
):
    telemetry = tmp_path / "telemetry.json"
    telemetry.write_text(json.dumps([_v6_row(10), _v6_row(20, vram_gb=6)]))
    output = tmp_path / "model.json"
    baseline = tmp_path / "baseline.json"
    quality_report = tmp_path / "quality-report.json"
    monkeypatch.setattr(train_model, "load_candidates", lambda: [])
    X, y = train_model.real_rows_to_training_data(json.loads(telemetry.read_text()))
    baseline.write_text(
        json.dumps(
            train_model.train_artifact(
                X, y, sample_weight=None, training_mode="telemetry", bootstrap_method=None,
                real_rows=[], telemetry_audit={"unique_configurations": len(X)},
                input_sources=[], evaluation=None,
            )
        )
    )
    monkeypatch.setattr(
        train_model,
        "compare_artifacts",
        lambda *_args, **_kwargs: {
            "passed": False,
            "failures": [
                "insufficient selection groups: candidate=2, required=3",
                "p90_absolute_percentage_error regressed: candidate=0.51, allowed=0.40",
            ],
            "candidate": {"selection_group_count": 2},
            "baseline": {"selection_group_count": 2},
            "thresholds": {"min_selection_groups": 3},
        },
    )
    monkeypatch.setattr(
        train_model,
        "parse_args",
        lambda: Namespace(
            telemetry_file=[telemetry], offline=True, telemetry_url="", output=output,
            baseline=baseline, quality_gate=True, minimum_real_configurations=0,
            maximum_rejection_rate=0.25, holdout_fraction=0.2, quality_report=quality_report,
            minimum_fit_negative_examples=5,
        ),
    )

    train_model.main()  # must not raise: too few real configs to compare, not a code bug

    assert output.read_text() == baseline.read_text()
    report = json.loads(quality_report.read_text())
    assert report["skipped"] is True
    # A real regression riding along with the data-volume skip must still
    # reach the CI log, not just the "not enough data" line - otherwise a
    # genuinely regressing candidate on a selection-group-starved night
    # would read as a harmless data shortage.
    assert "p90_absolute_percentage_error regressed" in capsys.readouterr().out


def test_quality_gate_insufficient_data_republishes_baseline_unchanged(tmp_path, monkeypatch):
    output = tmp_path / "model.json"
    baseline = tmp_path / "baseline.json"
    baseline.write_text("incumbent-output")
    quality_report = tmp_path / "quality-report.json"
    monkeypatch.setattr(train_model, "fetch_real_rows", lambda _url: [])
    monkeypatch.setattr(
        train_model,
        "parse_args",
        lambda: Namespace(
            telemetry_file=[],
            offline=False,
            telemetry_url="https://collector.example/export",
            output=output,
            baseline=baseline,
            quality_gate=True,
            minimum_real_configurations=100,
            maximum_rejection_rate=0.25,
            holdout_fraction=0.2,
            quality_report=quality_report,
            minimum_fit_negative_examples=5,
        ),
    )

    train_model.main()  # must not raise: too little telemetry is not a bug

    assert output.read_text() == "incumbent-output"
    report = json.loads(quality_report.read_text())
    assert report["passed"] is False
    assert report["skipped"] is True
    assert "too few unique" in report["reason"]


def test_quality_gate_insufficient_data_still_requires_readable_baseline(tmp_path, monkeypatch):
    output = tmp_path / "model.json"
    monkeypatch.setattr(train_model, "fetch_real_rows", lambda _url: [])
    monkeypatch.setattr(
        train_model,
        "parse_args",
        lambda: Namespace(
            telemetry_file=[],
            offline=False,
            telemetry_url="https://collector.example/export",
            output=output,
            baseline=tmp_path / "missing-baseline.json",
            quality_gate=True,
            minimum_real_configurations=100,
            maximum_rejection_rate=0.25,
            holdout_fraction=0.2,
            quality_report=None,
            minimum_fit_negative_examples=5,
        ),
    )

    with pytest.raises(ValueError, match="could not read baseline artifact"):
        train_model.main()

    assert not output.exists()


def test_offline_training_exports_v4_with_64_trees(tmp_path, monkeypatch):
    output = tmp_path / "model.json"
    monkeypatch.setattr(train_model, "load_candidates", lambda: [])
    monkeypatch.setattr(
        train_model,
        "parse_args",
        lambda: Namespace(
            telemetry_file=[], offline=True, telemetry_url="", output=output,
            baseline=None, quality_gate=False, minimum_real_configurations=0,
            maximum_rejection_rate=0.25, holdout_fraction=0.2, quality_report=None,
            minimum_fit_negative_examples=5,
        ),
    )

    train_model.main()

    artifact = json.loads(output.read_text())
    assert artifact["model_version"] == 4
    assert artifact["feature_schema_version"] == 1
    assert artifact["evaluation"] is None
    assert len(artifact["trees"]) == 64


# --- v7: structured success/failure telemetry ------------------------------


def _v7_success_row(speed: float, **overrides) -> dict:
    defaults = dict(
        benchmark_version=7,
        outcome="success",
        parameter_count_b=7.0,
        active_parameter_count_b=3.0,
        quant_bits=4.0,
        engine_version="1.0",
        client_version="1.0",
        runtime_profile="throughput",
        cpu_model="AMD Ryzen 5 5600X",
        cpu_arch="x86_64",
        cpu_physical_cores=6,
        cpu_logical_cores=12,
        sample_count=3,
        tokens_per_sec_min=speed - 1,
        tokens_per_sec_max=speed + 1,
    )
    defaults.update(overrides)
    return _row(speed, **defaults)


def _v7_model_unfit_row(**overrides) -> dict:
    row = {
        "engine": "ollama",
        "benchmark_version": 7,
        "outcome": "model_unfit",
        "failure_reason": "out_of_memory",
        "ram_gb": 16,
        "vram_gb": 8,
        "unified_memory": False,
        "model_installed": "big-model-70B-Q4.gguf",
        "parameter_count_b": 70.0,
        "active_parameter_count_b": 70.0,
        "quant_bits": 4.0,
        "engine_version": "1.0",
        "client_version": "1.0",
        "runtime_profile": "throughput",
        "context_length": 4096,
        "gpu_offload_percent": 100,
        "cpu_threads": 8,
        "num_batch": 512,
        "cpu_model": "AMD Ryzen 5 5600X",
        "cpu_arch": "x86_64",
        "cpu_physical_cores": 6,
        "cpu_logical_cores": 12,
    }
    row.update(overrides)
    return row


def _v7_transient_row(**overrides) -> dict:
    row = {
        "engine": "ollama",
        "benchmark_version": 7,
        "outcome": "transient_error",
        "failure_reason": "ollama_unavailable",
        "ram_gb": 16,
        "unified_memory": False,
        "model_installed": "small-model-1B-Q4.gguf",
    }
    row.update(overrides)
    return row


def test_v7_success_is_accepted_like_v6_and_counted_as_direct_v7():
    X, y, audit = train_model.real_rows_to_training_data_with_audit([_v7_success_row(20)])

    assert len(X) == 1
    assert y == [20]
    assert audit["direct_v6_unique_configurations"] == 0
    assert audit["direct_v7_unique_configurations"] == 1
    assert audit["rejections"] == {}


def test_v7_model_unfit_is_excluded_from_speed_regression_without_faking_zero():
    X, y, audit = train_model.real_rows_to_training_data_with_audit([_v7_model_unfit_row()])

    assert X == [] and y == []
    assert audit["rejections"] == {"model_unfit_excluded_from_regression": 1}
    assert audit["direct_v6_unique_configurations"] == 0
    assert audit["direct_v7_unique_configurations"] == 0


def test_v7_transient_error_is_excluded_from_speed_regression():
    X, y, audit = train_model.real_rows_to_training_data_with_audit([_v7_transient_row()])

    assert X == [] and y == []
    assert audit["rejections"] == {"transient_error_excluded": 1}


def test_v7_invalid_outcome_is_rejected():
    _sample, reason = train_model._real_row_to_sample(_v7_success_row(20, outcome="maybe"))
    assert reason == "invalid_outcome"


def test_v7_missing_outcome_is_rejected():
    row = _v7_success_row(20)
    del row["outcome"]
    _sample, reason = train_model._real_row_to_sample(row)
    assert reason == "invalid_outcome"


def test_direct_unique_configurations_counts_distinct_v6_and_v7_separately():
    _X, _y, audit = train_model.real_rows_to_training_data_with_audit(
        [_v6_row(10), _v7_success_row(20, model_installed="other.gguf", parameter_count_b=3.0)]
    )

    assert audit["direct_v6_unique_configurations"] == 1
    assert audit["direct_v7_unique_configurations"] == 1
    assert audit["direct_unique_configurations"] == 2
    validate_dataset(audit, min_unique_configurations=2)  # must not raise
    with pytest.raises(ValueError, match="too few unique direct-v6"):
        validate_dataset(audit, min_unique_configurations=3)


def test_direct_unique_configurations_deduplicates_same_config_across_v6_and_v7():
    """The exact bug this test guards against: a configuration measured
    under both v6 and v7 (identical hardware/model/runtime feature vector,
    just an older vs. newer client) is one real training example, not two.
    direct_v6_unique_configurations + direct_v7_unique_configurations would
    (wrongly) count it as 2; the quality gate must use the union instead.
    """
    _X, _y, audit = train_model.real_rows_to_training_data_with_audit(
        [_v6_row(10), _v7_success_row(20)]  # identical feature vector by default
    )

    assert audit["direct_v6_unique_configurations"] == 1
    assert audit["direct_v7_unique_configurations"] == 1
    assert audit["unique_configurations"] == 1
    assert audit["direct_unique_configurations"] == 1

    validate_dataset(audit, min_unique_configurations=1)  # must not raise
    with pytest.raises(ValueError, match="too few unique direct-v6"):
        validate_dataset(audit, min_unique_configurations=2)


def test_direct_unique_configurations_unaffected_by_transient_error():
    _X, _y, audit_before = train_model.real_rows_to_training_data_with_audit(
        [_v6_row(10), _v7_success_row(20)]
    )
    _X2, _y2, audit_after = train_model.real_rows_to_training_data_with_audit(
        [_v6_row(10), _v7_success_row(20), _v7_transient_row()]
    )

    assert audit_after["direct_unique_configurations"] == audit_before["direct_unique_configurations"]
    assert audit_after["unique_configurations"] == audit_before["unique_configurations"]


def test_speed_regression_unique_configurations_unaffected_by_model_unfit():
    _X, _y, audit_before = train_model.real_rows_to_training_data_with_audit(
        [_v6_row(10), _v7_success_row(20)]
    )
    _X2, _y2, audit_after = train_model.real_rows_to_training_data_with_audit(
        [_v6_row(10), _v7_success_row(20), _v7_model_unfit_row()]
    )

    assert audit_after["direct_unique_configurations"] == audit_before["direct_unique_configurations"]
    assert audit_after["unique_configurations"] == audit_before["unique_configurations"]


def test_validate_dataset_excludes_intentional_v7_exclusions_from_rejection_rate():
    rows = [_v6_row(10)] + [_v7_model_unfit_row() for _ in range(10)]
    _X, _y, audit = train_model.real_rows_to_training_data_with_audit(rows)

    assert audit["rejected_rows"] == 10
    assert audit["raw_rows"] == 11
    # A naive rejected/raw ratio would be 10/11 (~91%) and fail any sane
    # rejection-rate gate, even though every one of those 10 rows is a
    # legitimate, correctly-classified model_unfit failure event - not bad
    # data. validate_dataset must not punish a healthy stream of v7 failure
    # telemetry this way.
    validate_dataset(audit, min_unique_configurations=1, max_rejection_rate=0.25)


def test_validate_dataset_excludes_pre_v6_hardware_identity_gap_from_rejection_rate():
    # Regression test for the 2026-08-07 incident: a healthy stream of
    # legitimate pre-v6 telemetry (no hardware identity to report, by
    # schema design - not malformed) must not trip the rejection-rate gate
    # any more than a healthy stream of v7 model_unfit events does.
    rows = [_v6_row(10)] + [_row(20) for _ in range(10)]
    _X, _y, audit = train_model.real_rows_to_training_data_with_audit(rows)

    assert audit["rejected_rows"] == 10
    assert audit["rejections"] == {"no_hardware_identity_pre_v6_schema": 10}
    validate_dataset(audit, min_unique_configurations=1, max_rejection_rate=0.25)


def test_real_rows_to_fit_training_data_separates_success_and_model_unfit():
    rows = [
        _v6_row(20),
        _v7_success_row(30, model_installed="other.gguf", parameter_count_b=3.0),
        _v7_model_unfit_row(),
        _v7_transient_row(),
    ]

    fit_X, fit_y, audit = train_model.real_rows_to_fit_training_data_with_audit(rows)

    assert fit_y.count(True) == 2
    assert fit_y.count(False) == 1
    assert len(fit_X) == 3
    assert audit["positive_examples"] == 2
    assert audit["negative_examples"] == 1
    assert audit["rejections"] == {"transient_error_excluded": 1}


def test_real_rows_to_fit_training_data_model_unfit_needs_no_speed_fields():
    row = _v7_model_unfit_row()
    assert "tokens_per_sec" not in row
    assert "sample_count" not in row

    fit_X, fit_y, audit = train_model.real_rows_to_fit_training_data_with_audit([row])

    assert fit_y == [False]
    assert audit["rejected_rows"] == 0


def test_real_rows_to_fit_training_data_model_unfit_still_requires_model_metadata():
    row = _v7_model_unfit_row()
    del row["parameter_count_b"]

    fit_X, fit_y, audit = train_model.real_rows_to_fit_training_data_with_audit([row])

    assert fit_X == [] and fit_y == []
    assert audit["rejections"] == {"missing_model_metadata": 1}


def test_real_rows_to_fit_training_data_v6_rows_with_no_outcome_are_implicit_positives():
    """v6 telemetry has no `outcome` field yet, but it does carry a real
    cpu_model - so a valid v6 row is still an implicit success, unlike
    pre-v6 rows (see the next test)."""
    fit_X, fit_y, audit = train_model.real_rows_to_fit_training_data_with_audit(
        [_v6_row(10), _v6_row(20, vram_gb=6)]
    )

    assert fit_y == [True, True]
    assert audit["positive_examples"] == 2
    assert audit["negative_examples"] == 0


def test_real_rows_to_fit_training_data_pre_v6_rows_are_excluded_not_assumed_positive():
    # Pre-v6 rows have no cpu_model at all, so they'd be indistinguishable
    # "unknown hardware" in the fit dataset too - excluded rather than
    # assumed fit, same as the speed-regression dataset.
    fit_X, fit_y, audit = train_model.real_rows_to_fit_training_data_with_audit(
        [_row(10), _row(20, vram_gb=6)]
    )

    assert fit_X == [] and fit_y == []
    assert audit["positive_examples"] == 0
    assert audit["negative_examples"] == 0
    assert audit["rejections"] == {"no_hardware_identity_pre_v6_schema": 2}


def test_v7_outcome_contract_across_both_datasets():
    """Single source of truth for the v7 outcome contract:
    - success -> positive fit label AND a real speed-regression sample.
    - model_unfit -> negative fit label, excluded from speed regression
      (no faked tokens_per_sec).
    - transient_error -> excluded from BOTH datasets entirely; it is never
      a fit label (positive or negative) and never a speed sample.
    """
    success = _v7_success_row(42, model_installed="ok.gguf")
    unfit = _v7_model_unfit_row(model_installed="oom.gguf")
    transient = _v7_transient_row(model_installed="flaky.gguf")
    rows = [success, unfit, transient]

    speed_X, speed_y, speed_audit = train_model.real_rows_to_training_data_with_audit(rows)
    fit_X, fit_y, fit_audit = train_model.real_rows_to_fit_training_data_with_audit(rows)

    # Speed regression: only the success row contributes, with its real
    # tokens_per_sec - never a synthesized value for the other two.
    assert speed_y == [42]
    assert speed_audit["valid_rows"] == 1
    assert speed_audit["rejections"] == {
        "model_unfit_excluded_from_regression": 1,
        "transient_error_excluded": 1,
    }

    # Fit classification: success is the only positive, model_unfit is the
    # only negative, transient_error contributes to neither.
    assert fit_y.count(True) == 1
    assert fit_y.count(False) == 1
    assert len(fit_X) == 2
    assert fit_audit["positive_examples"] == 1
    assert fit_audit["negative_examples"] == 1
    assert fit_audit["rejections"] == {"transient_error_excluded": 1}


def test_report_telemetry_v8_success_event_feeds_speed_regression_and_positive_fit_label(monkeypatch):
    """End-to-end contract check between omm.cli and scripts.train_model:
    the exact event `_report_telemetry` sends for a successful benchmark
    must be consumable by both training datasets. This is exactly the kind
    of client/schema mismatch that once let v7 success events silently
    stay on v6 with no test catching it."""
    from omm import cli as cli_mod

    monkeypatch.setattr(
        cli_mod,
        "scan_hardware",
        lambda: SimpleNamespace(
            ram_total_gb=16.0, vram_total_gb=8.0, unified_memory=False, gpu_tflops=20.0,
            cpu="AMD Ryzen 5 5600X", cpu_arch="x86_64", cpu_physical_cores=4, cpu_logical_cores=8,
            gpu_name="NVIDIA RTX 4090",
        ),
    )
    sent = []
    monkeypatch.setattr(
        cli_mod.telemetry, "send_event", lambda event, force=False: sent.append(event) or True
    )

    cli_mod._report_telemetry(
        "model-7B-Q4.gguf", "org/model", 42.5,
        size_bytes=4 * 1024**3, sample_count=3, speed_min=40.0, speed_max=45.0,
        model_metadata={"parameter_size": "7B", "quantization_level": "Q4_K_M"},
        runtime={
            "runtime_profile": "explicit_ollama_options", "context_length": 4096,
            "gpu_offload_percent": 100, "cpu_threads": 8, "num_batch": 512,
        },
        engine_version="0.32.1", model_filename="model-7B-Q4.gguf", model_digest="sha256:" + "a" * 64,
    )

    event = sent[0]
    assert event["benchmark_version"] == 8
    assert event["outcome"] == "success"
    assert event["cpu_score"] == 5600.0
    assert event["gpu_score"] == 4090.0
    assert "failure_reason" not in event
    assert "cpu_model" not in event

    speed_X, speed_y, speed_audit = train_model.real_rows_to_training_data_with_audit([event])
    assert speed_y == [42.5]
    assert speed_audit["direct_v8_unique_configurations"] == 1
    assert speed_audit["rejections"] == {}

    fit_X, fit_y, fit_audit = train_model.real_rows_to_fit_training_data_with_audit([event])
    assert fit_y == [True]
    assert fit_audit["positive_examples"] == 1
    assert fit_audit["negative_examples"] == 0


# --- v7: performance_unfit (confirmed_generation_timeout) -----------------


def _v7_performance_unfit_row(**overrides) -> dict:
    row = {
        "engine": "ollama",
        "benchmark_version": 7,
        "outcome": "performance_unfit",
        "failure_reason": "confirmed_generation_timeout",
        "confirmation_attempts": 2,
        "timeout_seconds": 180,
        "ram_gb": 31.3,
        "vram_gb": 6.0,
        "unified_memory": False,
        "model_installed": "qwen2.5-32b-q8.gguf",
        "parameter_count_b": 32.8,
        "active_parameter_count_b": 32.0,
        "quant_bits": 8.0,
        "engine_version": "1.0",
        "client_version": "1.0",
        "runtime_profile": "explicit_ollama_options",
        "context_length": 4096,
        "gpu_offload_percent": 12,
        "cpu_threads": 8,
        "num_batch": 256,
        "cpu_model": "Intel Core i7-9700",
        "cpu_arch": "x86_64",
        "cpu_physical_cores": 8,
        "cpu_logical_cores": 8,
    }
    row.update(overrides)
    return row


def test_v7_performance_unfit_is_excluded_from_speed_regression_without_faking_zero():
    """13. Never coerced into a fake tokens_per_sec=0 regression row."""
    X, y, audit = train_model.real_rows_to_training_data_with_audit([_v7_performance_unfit_row()])

    assert X == [] and y == []
    assert audit["rejections"] == {"performance_unfit_excluded_from_regression": 1}
    assert audit["direct_v6_unique_configurations"] == 0
    assert audit["direct_v7_unique_configurations"] == 0


def test_speed_regression_unique_configurations_unaffected_by_performance_unfit():
    _X, _y, audit_before = train_model.real_rows_to_training_data_with_audit(
        [_v6_row(10), _v7_success_row(20)]
    )
    _X2, _y2, audit_after = train_model.real_rows_to_training_data_with_audit(
        [_v6_row(10), _v7_success_row(20), _v7_performance_unfit_row()]
    )

    assert audit_after["direct_unique_configurations"] == audit_before["direct_unique_configurations"]
    assert audit_after["unique_configurations"] == audit_before["unique_configurations"]


def test_real_rows_to_fit_training_data_performance_unfit_is_a_negative_example():
    """12. performance_unfit contributes a negative (fit=False) example,
    exactly like model_unfit, built without any speed fields."""
    row = _v7_performance_unfit_row()
    assert "tokens_per_sec" not in row
    assert "sample_count" not in row

    fit_X, fit_y, audit = train_model.real_rows_to_fit_training_data_with_audit([row])

    assert fit_y == [False]
    assert audit["positive_examples"] == 0
    assert audit["negative_examples"] == 1
    assert audit["rejected_rows"] == 0


def test_v7_outcome_contract_across_all_four_outcomes():
    """Extends test_v7_outcome_contract_across_both_datasets to include
    performance_unfit: it behaves exactly like model_unfit in both
    datasets - negative fit label, excluded from speed regression - even
    though the two failure_reasons mean different things operationally."""
    success = _v7_success_row(42, model_installed="ok.gguf")
    unfit = _v7_model_unfit_row(model_installed="oom.gguf")
    perf_unfit = _v7_performance_unfit_row(model_installed="slow.gguf")
    transient = _v7_transient_row(model_installed="flaky.gguf")
    rows = [success, unfit, perf_unfit, transient]

    speed_X, speed_y, speed_audit = train_model.real_rows_to_training_data_with_audit(rows)
    fit_X, fit_y, fit_audit = train_model.real_rows_to_fit_training_data_with_audit(rows)

    assert speed_y == [42]
    assert speed_audit["valid_rows"] == 1
    assert speed_audit["rejections"] == {
        "model_unfit_excluded_from_regression": 1,
        "performance_unfit_excluded_from_regression": 1,
        "transient_error_excluded": 1,
    }

    assert fit_y.count(True) == 1
    assert fit_y.count(False) == 2
    assert len(fit_X) == 3
    assert fit_audit["positive_examples"] == 1
    assert fit_audit["negative_examples"] == 2
    assert fit_audit["rejections"] == {"transient_error_excluded": 1}


def test_validate_dataset_excludes_performance_unfit_from_rejection_rate():
    """14. Same treatment as model_unfit: a healthy stream of confirmed-
    timeout telemetry must not look like bad data to the rejection gate."""
    rows = [_v6_row(10)] + [_v7_performance_unfit_row() for _ in range(10)]
    _X, _y, audit = train_model.real_rows_to_training_data_with_audit(rows)

    assert audit["rejected_rows"] == 10
    assert audit["raw_rows"] == 11
    validate_dataset(audit, min_unique_configurations=1, max_rejection_rate=0.25)
