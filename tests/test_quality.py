from __future__ import annotations

import json

import pytest
import requests

from omm import benchmark, quality
from omm.hardware import HardwareInfo


def _hardware() -> HardwareInfo:
    return HardwareInfo(
        os_name="macOS",
        os_version="",
        cpu="private CPU name",
        ram_total_gb=24,
        ram_available_gb=18,
        unified_memory=True,
        gpu_name="private GPU name",
        vram_total_gb=24,
        vram_free_gb=18,
    )


def test_isolated_evaluator_enforces_absolute_deadline_and_terminates_worker(monkeypatch):
    class FakeConnection:
        def poll(self, timeout):
            return False

        def close(self):
            pass

    class FakeProcess:
        exitcode = None

        def __init__(self):
            self.alive = True
            self.terminated = False

        def start(self):
            pass

        def is_alive(self):
            return self.alive

        def terminate(self):
            self.terminated = True
            self.alive = False

        def join(self, timeout=None):
            pass

        def kill(self):
            self.alive = False

    process = FakeProcess()

    class FakeContext:
        def Pipe(self, duplex=False):
            assert duplex is False
            return FakeConnection(), FakeConnection()

        def Process(self, **kwargs):
            return process

    times = iter([0.0, 31.0, 601.0])
    monkeypatch.setattr(quality.multiprocessing, "get_context", lambda method: FakeContext())
    monkeypatch.setattr(quality.time, "monotonic", lambda: next(times))
    progress = []

    with pytest.raises(quality.QualityEvaluationError, match="session deadline") as error:
        quality.evaluate_model_isolated(
            "model",
            {},
            timeout_seconds=600,
            progress_callback=lambda elapsed, deadline: progress.append((elapsed, deadline)),
        )

    assert error.value.failure_reason == quality.FAILURE_REASON_GENERATION_TIMEOUT
    assert progress == [(31.0, 600)]
    assert process.terminated is True


def test_bundled_quality_pack_is_versioned_bounded_and_attributed():
    pack, digest = quality.load_pack()

    assert pack["pack_id"] == "localfit-gsm8k-bilingual-smoke"
    assert pack["pack_version"] == "1.1.0"
    assert len(pack["items"]) == 8
    assert {item["language"] for item in pack["items"]} == {"en", "ko"}
    assert pack["sources"][0]["license"] == "MIT"
    assert len(digest) == 64


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ("FINAL: 18", "18"),
        ("work here\nFINAL = 70,000", "70000"),
        ("The result is 3.0", "3"),
        ("no numeric answer", None),
    ],
)
def test_parse_numeric_answer(response, expected):
    assert quality.parse_numeric_answer(response) == expected


def test_quality_pack_rejects_duplicate_ids(tmp_path):
    pack, _digest = quality.load_pack()
    pack["items"][1]["id"] = pack["items"][0]["id"]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(pack))

    with pytest.raises(quality.QualityEvaluationError, match="unique"):
        quality.load_pack(path)


def test_evaluate_model_stores_parsed_answers_not_raw_text(monkeypatch):
    pack, _digest = quality.load_pack()
    monkeypatch.setattr(
        quality,
        "_model_metadata",
        lambda tag: {
            "tag": tag,
            "digest": "sha256:abc",
            "size_bytes": 123,
            "format": "gguf",
            "family": "test",
            "parameter_size": "1B",
            "quantization_level": "Q4_K_M",
            "license": "apache-2.0",
            "license_link": None,
            "capabilities": ["completion"],
        },
    )
    answers = iter(item["expected"] for item in pack["items"])

    def fake_generate(tag, prompt, generation, num_predict=None):
        answer = next(answers) if num_predict is None else "1"
        return {
            "response": f"private reasoning must not persist\nFINAL: {answer}",
            "eval_count": 10,
            "eval_duration": 100_000_000,
        }

    monkeypatch.setattr(quality, "_generate", fake_generate)
    result = quality.evaluate_model("model:latest", pack, speed_runs=2)

    assert result["quality"]["accuracy"] == 1.0
    assert result["quality"]["raw_responses_stored"] is False
    assert all("response" not in item for item in result["quality"]["items"])
    assert result["speed"]["samples_tokens_per_sec"] == [100.0, 100.0]


def test_collect_evidence_redacts_hardware_names(monkeypatch):
    monkeypatch.setattr(quality, "ollama_version", lambda: "0.30.10")
    monkeypatch.setattr(
        quality,
        "evaluate_model",
        lambda tag, pack, speed_runs=3: {"tag": tag, "quality": {}, "speed": {}},
    )
    unloaded = []
    monkeypatch.setattr(quality, "unload_model", lambda tag: unloaded.append(tag) or True)

    report = quality.collect_evidence(["model:one"], _hardware())

    assert report["environment"]["ram_gb"] == 24
    assert report["environment"]["raw_hardware_names_stored"] is False
    assert "private CPU name" not in json.dumps(report)
    assert "private GPU name" not in json.dumps(report)
    assert unloaded == ["model:one"]
    assert report["models"][0]["measurement_isolation"]["unloaded_after_run"] is True


def test_collect_evidence_calls_on_model_start_once_per_tag_in_order(monkeypatch):
    monkeypatch.setattr(quality, "ollama_version", lambda: "0.32.1")
    monkeypatch.setattr(
        quality,
        "evaluate_model",
        lambda tag, pack, speed_runs=3: {"tag": tag, "quality": {}, "speed": {}},
    )
    monkeypatch.setattr(quality, "unload_model", lambda tag: True)
    calls = []

    quality.collect_evidence(
        ["model:one", "model:two"],
        _hardware(),
        on_model_start=lambda tag, index, total: calls.append((tag, index, total)),
    )

    assert calls == [("model:one", 1, 2), ("model:two", 2, 2)]


def test_collect_evidence_recovers_from_daemon_crash_mid_batch(monkeypatch):
    """Daemon found dead before the first tag, restart succeeds immediately -
    the batch proceeds and both tags still get benchmarked."""
    version_calls = {"count": 0}

    def fake_ollama_version():
        version_calls["count"] += 1
        return None if version_calls["count"] == 1 else "0.30.10"

    monkeypatch.setattr(quality, "ollama_version", fake_ollama_version)
    monkeypatch.setattr(quality.benchmark, "start_ollama_daemon", lambda: object())
    monkeypatch.setattr(
        quality,
        "evaluate_model",
        lambda tag, pack, speed_runs=3: {"tag": tag, "quality": {}, "speed": {}},
    )
    monkeypatch.setattr(quality, "unload_model", lambda tag: True)
    events = []

    report = quality.collect_evidence(
        ["model:one", "model:two"],
        _hardware(),
        on_daemon_event=events.append,
    )

    assert [m["tag"] for m in report["models"]] == ["model:one", "model:two"]
    assert any("restart" in event.lower() for event in events)


def test_collect_evidence_gives_up_after_max_daemon_restart_failures(monkeypatch):
    """Daemon never comes back - stop after the failure cap instead of
    burning a full per-request timeout on every remaining tag."""
    monkeypatch.setattr(quality, "ollama_version", lambda: None)
    monkeypatch.setattr(quality.benchmark, "start_ollama_daemon", lambda: None)
    monkeypatch.setattr(quality.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        quality,
        "evaluate_model",
        lambda tag, pack, speed_runs=3: {"tag": tag, "quality": {}, "speed": {}},
    )
    monkeypatch.setattr(quality, "unload_model", lambda tag: True)
    events = []

    report = quality.collect_evidence(
        ["model:one", "model:two"],
        _hardware(),
        on_daemon_event=events.append,
    )

    assert report["models"] == []
    assert any("won't come back" in event for event in events)


def test_unload_model_uses_keep_alive_zero_without_deleting(monkeypatch):
    calls = []
    monkeypatch.setattr(
        quality,
        "_request_json",
        lambda method, path, payload=None, timeout=180: calls.append(
            (method, path, payload, timeout)
        )
        or {},
    )

    assert quality.unload_model("model:latest") is True
    assert calls == [
        (
            "POST",
            "/api/generate",
            {"model": "model:latest", "stream": False, "keep_alive": 0},
            30,
        )
    ]


def test_write_evidence_replaces_atomically(tmp_path):
    path = tmp_path / "nested" / "evidence.json"
    quality.write_evidence({"schema_version": 1}, path)

    assert json.loads(path.read_text()) == {"schema_version": 1}
    assert not path.with_suffix(".json.tmp").exists()


def test_runtime_snapshot_prefers_digest_and_reports_actual_offload(monkeypatch):
    digest = "a" * 64
    monkeypatch.setattr(
        quality,
        "_request_json",
        lambda *args, **kwargs: {
            "models": [
                {
                    "name": "model:latest",
                    "digest": "b" * 64,
                    "context_length": 2048,
                    "size": 100,
                    "size_vram": 0,
                },
                {
                    "name": "other:latest",
                    "digest": digest,
                    "context_length": 4096,
                    "size": 100,
                    "size_vram": 75,
                },
            ]
        },
    )

    snapshot = quality.runtime_snapshot(
        "model:latest",
        digest,
        {"num_ctx": 4096, "num_thread": 8, "num_batch": 512},
    )

    assert snapshot == {
        "context_length": 4096,
        "gpu_offload_percent": 75,
        "cpu_threads": 8,
        "num_batch": 512,
        "runtime_profile": "explicit_ollama_options",
    }


def test_model_metadata_matches_bare_tag_against_implicit_latest_suffix(monkeypatch):
    """Ollama's /api/tags always names entries with a suffix ('mmproj:latest'),
    even when the caller passes the bare tag omm hands around internally
    ('mmproj'). A strict-equality lookup used to report a linked, installed
    model as "not installed"."""

    def fake_request(method, path, payload=None, timeout=10):
        if path == "/api/tags":
            return {
                "models": [
                    {"name": "mmproj:latest", "digest": "sha256:" + "a" * 64, "size": 100}
                ]
            }
        assert path == "/api/show"
        return {"details": {}, "model_info": {}, "capabilities": []}

    monkeypatch.setattr(quality, "_request_json", fake_request)

    metadata = quality._model_metadata("mmproj")

    assert metadata["tag"] == "mmproj"
    assert metadata["digest"] == "sha256:" + "a" * 64


def test_model_metadata_rejects_already_linked_clip_mmproj(monkeypatch):
    """A model linked before omm refused clip/mmproj links (or linked
    manually via `ollama create`) must fail fast with a clear reason instead
    of reaching /api/generate, where Ollama's llama-server crashes with
    "unsupported model architecture: 'clip'" and surfaces as an opaque 500."""

    def fake_request(method, path, payload=None, timeout=10):
        assert path == "/api/tags"
        return {
            "models": [
                {
                    "name": "mmproj:latest",
                    "digest": "sha256:" + "a" * 64,
                    "size": 100,
                    "details": {"family": "clip"},
                }
            ]
        }

    monkeypatch.setattr(quality, "_request_json", fake_request)

    with pytest.raises(quality.QualityEvaluationError, match="multimodal projector"):
        quality._model_metadata("mmproj")


def test_moe_active_parameter_count_scales_only_routed_expert_tensors():
    model_info = {
        "general.architecture": "gptoss",
        "general.parameter_count": 1_650,
        "gptoss.expert_count": 4,
        "gptoss.expert_used_count": 1,
    }
    tensors = [
        {"name": "token_embd.weight", "shape": [10, 10]},
        {"name": "output.weight", "shape": [10, 10]},
        {"name": "blk.0.attn.weight", "shape": [10, 20]},
        {"name": "blk.0.ffn_gate_inp.weight", "shape": [10, 4]},
        {"name": "blk.0.ffn_gate_exps.weight", "shape": [10, 10, 4]},
        {"name": "blk.0.ffn_up_exps.weight", "shape": [10, 10, 4]},
        {"name": "blk.0.ffn_down_exps.weight", "shape": [10, 10, 4]},
        {"name": "output_norm.weight", "shape": [10]},
    ]

    # Always-active: total 1650 - expert 1200 - input embedding 100 = 350.
    # Routed expert share: 1200 * 1/4 = 300. Active total = 650.
    assert quality._moe_active_parameter_count_billions(model_info, tensors) == 0.00000065


def test_moe_active_parameter_count_rejects_incomplete_tensor_inventory():
    model_info = {
        "general.architecture": "testmoe",
        "general.parameter_count": 1_000,
        "testmoe.expert_count": 8,
        "testmoe.expert_used_count": 2,
    }
    tensors = [
        {"name": "token_embd.weight", "shape": [10, 10]},
        {"name": "blk.0.ffn_gate_exps.weight", "shape": [10, 10, 8]},
    ]

    assert quality._moe_active_parameter_count_billions(model_info, tensors) is None


def test_model_metadata_derives_moe_active_parameters_from_verbose_show(monkeypatch):
    calls = []

    def fake_request(method, path, payload=None, timeout=10):
        calls.append((method, path, payload, timeout))
        if path == "/api/tags":
            return {
                "models": [
                    {
                        "name": "moe:latest",
                        "digest": "sha256:" + "a" * 64,
                        "size": 100,
                        "details": {"family": "testmoe"},
                    }
                ]
            }
        if payload == {"model": "moe"}:
            return {
                "details": {"parameter_size": "1B", "quantization_level": "Q4"},
                "model_info": {
                    "general.architecture": "testmoe",
                    "general.parameter_count": 1_600,
                    "testmoe.expert_count": 4,
                    "testmoe.expert_used_count": 1,
                },
                "capabilities": ["completion"],
            }
        assert payload == {"model": "moe", "verbose": True}
        return {
            "model_info": {
                "general.architecture": "testmoe",
                "general.parameter_count": 1_600,
                "testmoe.expert_count": 4,
                "testmoe.expert_used_count": 1,
            },
            "tensors": [
                {"name": "always.weight", "shape": [20, 20]},
                {"name": "blk.0.ffn_gate_exps.weight", "shape": [10, 10, 4]},
                {"name": "blk.0.ffn_up_exps.weight", "shape": [10, 10, 4]},
                {"name": "blk.0.ffn_down_exps.weight", "shape": [10, 10, 4]},
            ],
        }

    monkeypatch.setattr(quality, "_request_json", fake_request)

    metadata = quality._model_metadata("moe")

    assert metadata["is_moe"] is True
    assert metadata["active_parameter_count_b"] == 0.0000007
    assert calls[-1][2] == {"model": "moe", "verbose": True}


def test_list_benchmarkable_tags_excludes_clip_and_sorts(monkeypatch):
    def fake_request(method, path, payload=None, timeout=10):
        assert path == "/api/tags"
        return {
            "models": [
                {"name": "zebra:latest", "details": {"family": "llama"}},
                {"name": "mmproj:latest", "details": {"family": "clip"}},
                {"name": "alpha:latest", "details": {"family": "llama"}},
            ]
        }

    monkeypatch.setattr(quality, "_request_json", fake_request)

    assert quality.list_benchmarkable_tags() == ["alpha:latest", "zebra:latest"]


def test_list_benchmarkable_tags_empty_when_nothing_installed(monkeypatch):
    monkeypatch.setattr(quality, "_request_json", lambda *a, **k: {"models": []})

    assert quality.list_benchmarkable_tags() == []


def test_list_benchmarkable_tags_empty_when_models_key_missing(monkeypatch):
    monkeypatch.setattr(quality, "_request_json", lambda *a, **k: {})

    assert quality.list_benchmarkable_tags() == []


def test_multi_sample_benchmark_reuses_identical_options(monkeypatch):
    calls = []
    monkeypatch.setattr(
        benchmark,
        "benchmark_ollama",
        lambda tag, options=None: calls.append((tag, dict(options or {}))) or 10.0,
    )

    result = benchmark.benchmark_ollama_samples(
        "model:latest", runs=3, options={"num_ctx": 4096, "num_thread": 8}
    )

    assert result["count"] == 3
    assert calls == [("model:latest", {"num_ctx": 4096, "num_thread": 8})] * 3


# --- v7 structured failure telemetry ---------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, body=None):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body

    @property
    def text(self):
        return json.dumps(self._body) if self._body is not None else ""


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            {"error": "model requires more system memory (10.0 GiB) than is available (8.0 GiB)"},
            quality.FAILURE_REASON_OUT_OF_MEMORY,
        ),
        ({"error": "CUDA out of memory"}, quality.FAILURE_REASON_OUT_OF_MEMORY),
        ({"error": "failed to load model"}, quality.FAILURE_REASON_MODEL_LOAD_FAILED),
        ({"error": "this model does not support tool calling"}, quality.FAILURE_REASON_UNSUPPORTED_RUNTIME),
        ({"error": "something else entirely"}, quality.FAILURE_REASON_UNKNOWN),
        (None, quality.FAILURE_REASON_UNKNOWN),
    ],
)
def test_classify_error_response_maps_ollama_error_bodies(body, expected):
    assert quality._classify_error_response(_FakeResponse(500, body)) == expected


def test_request_json_classifies_connect_timeout_as_ollama_unavailable(monkeypatch):
    monkeypatch.setattr(
        requests.Session, "request",
        lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.ConnectTimeout("no route")),
    )
    with pytest.raises(quality.QualityEvaluationError) as excinfo:
        quality._request_json("GET", "/api/tags")
    assert excinfo.value.failure_reason == quality.FAILURE_REASON_OLLAMA_UNAVAILABLE


def test_request_json_classifies_read_timeout_as_generation_timeout(monkeypatch):
    monkeypatch.setattr(
        requests.Session, "request",
        lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.ReadTimeout("slow")),
    )
    with pytest.raises(quality.QualityEvaluationError) as excinfo:
        quality._request_json("POST", "/api/generate")
    assert excinfo.value.failure_reason == quality.FAILURE_REASON_GENERATION_TIMEOUT


def test_request_json_classifies_connection_error_as_connection_error(monkeypatch):
    monkeypatch.setattr(
        requests.Session, "request",
        lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.ConnectionError("reset by peer")),
    )
    with pytest.raises(quality.QualityEvaluationError) as excinfo:
        quality._request_json("GET", "/api/ps")
    assert excinfo.value.failure_reason == quality.FAILURE_REASON_CONNECTION_ERROR


def test_request_json_classifies_oom_response_as_out_of_memory(monkeypatch):
    monkeypatch.setattr(
        requests.Session, "request",
        lambda *a, **k: _FakeResponse(500, {"error": "model requires more system memory than is available"}),
    )
    with pytest.raises(quality.QualityEvaluationError) as excinfo:
        quality._request_json("POST", "/api/generate", {})
    assert excinfo.value.failure_reason == quality.FAILURE_REASON_OUT_OF_MEMORY


def test_request_json_defaults_unclassified_http_errors_to_unknown(monkeypatch):
    monkeypatch.setattr(
        requests.Session,
        "request",
        lambda *a, **k: _FakeResponse(503, {"error": "temporarily busy"}),
    )
    with pytest.raises(quality.QualityEvaluationError) as excinfo:
        quality._request_json("GET", "/api/tags")
    assert excinfo.value.failure_reason == quality.FAILURE_REASON_UNKNOWN


@pytest.mark.parametrize("reason", sorted(quality.MODEL_UNFIT_REASONS))
def test_outcome_for_failure_reason_model_unfit_lane(reason):
    assert quality.outcome_for_failure_reason(reason) == "model_unfit"


@pytest.mark.parametrize("reason", sorted(quality.TRANSIENT_ERROR_REASONS))
def test_outcome_for_failure_reason_transient_lane(reason):
    assert quality.outcome_for_failure_reason(reason) == "transient_error"


def test_quality_evaluation_error_falls_back_to_unknown_for_bad_reason():
    error = quality.QualityEvaluationError("boom", failure_reason="not-a-real-reason")
    assert error.failure_reason == quality.FAILURE_REASON_UNKNOWN


def test_collect_evidence_preserves_sibling_results_after_one_model_fails(monkeypatch):
    """A model that OOMs must not take down models already evaluated - and
    the loop must still evaluate whatever comes after it."""
    monkeypatch.setattr(quality, "ollama_version", lambda: "0.32.1")
    monkeypatch.setattr(
        quality,
        "_model_metadata",
        lambda tag: {
            "tag": tag, "digest": "sha256:" + "a" * 64, "size_bytes": 900_000_000,
            "format": "gguf", "family": "test", "parameter_size": "7B",
            "quantization_level": "Q4_K_M", "license": None, "license_link": None,
            "capabilities": [],
        },
    )

    def fake_evaluate(tag, pack, speed_runs=3, runtime_options=None, model_metadata=None):
        if tag == "big:latest":
            raise quality.QualityEvaluationError(
                "simulated OOM at /some/local/path", failure_reason=quality.FAILURE_REASON_OUT_OF_MEMORY
            )
        return {
            "tag": tag,
            "quality": {"correct": 6, "total": 8, "accuracy": 0.75},
            "speed": {"median_tokens_per_sec": 40.0, "samples_tokens_per_sec": [40.0], "runs": 1},
        }

    monkeypatch.setattr(quality, "evaluate_model", fake_evaluate)
    monkeypatch.setattr(quality, "unload_model", lambda tag: True)

    report = quality.collect_evidence(["small:latest", "big:latest", "third:latest"], _hardware())

    by_tag = {m["tag"]: m for m in report["models"]}
    assert set(by_tag) == {"small:latest", "big:latest", "third:latest"}
    assert by_tag["small:latest"]["outcome"] == "success"
    assert by_tag["small:latest"]["speed"]["median_tokens_per_sec"] == 40.0
    assert by_tag["third:latest"]["outcome"] == "success"
    assert by_tag["big:latest"]["outcome"] == "model_unfit"
    assert by_tag["big:latest"]["failure_reason"] == "out_of_memory"
    assert "tokens_per_sec" not in by_tag["big:latest"]
    assert "speed" not in by_tag["big:latest"]
    assert "sample_count" not in by_tag["big:latest"]
    assert by_tag["big:latest"]["model_metadata"]["parameter_size"] == "7B"
    assert "simulated OOM" not in json.dumps(report)


def test_collect_evidence_classifies_daemon_unreachable_as_transient(monkeypatch):
    # A healthy version response here means collect_evidence's own daemon
    # precheck (which now runs before every tag) lets this attempt through;
    # the "unavailable" condition under test comes from _model_metadata /
    # evaluate_model themselves, not from the precheck's own recovery loop
    # (covered separately by test_collect_evidence_gives_up_after_max_daemon_restart_failures).
    monkeypatch.setattr(quality, "ollama_version", lambda: "0.32.1")

    def raising_metadata(tag):
        raise quality.QualityEvaluationError(
            "connection refused by 10.0.0.5", failure_reason=quality.FAILURE_REASON_OLLAMA_UNAVAILABLE
        )

    def raising_evaluate(tag, pack, speed_runs=3):
        raise quality.QualityEvaluationError(
            "connection refused by 10.0.0.5", failure_reason=quality.FAILURE_REASON_OLLAMA_UNAVAILABLE
        )

    monkeypatch.setattr(quality, "_model_metadata", raising_metadata)
    monkeypatch.setattr(quality, "evaluate_model", raising_evaluate)
    monkeypatch.setattr(quality, "unload_model", lambda tag: False)

    report = quality.collect_evidence(["small:latest"], _hardware())

    entry = report["models"][0]
    assert entry["outcome"] == "transient_error"
    assert entry["failure_reason"] == "ollama_unavailable"
    assert "model_metadata" not in entry
    assert "attempted_runtime" not in entry


def test_model_unfit_reasons_are_narrow_and_explicit():
    """Only reasons Ollama's own response makes explicit belong here. A
    missing file, a corrupted one, or any other undiagnosed load failure is
    not proof the model doesn't fit this hardware."""
    assert quality.MODEL_UNFIT_REASONS == {
        quality.FAILURE_REASON_OUT_OF_MEMORY,
        quality.FAILURE_REASON_UNSUPPORTED_RUNTIME,
    }
    assert quality.FAILURE_REASON_MODEL_LOAD_FAILED in quality.TRANSIENT_ERROR_REASONS


def test_outcome_for_model_load_failed_is_transient_not_unfit():
    assert quality.outcome_for_failure_reason(quality.FAILURE_REASON_MODEL_LOAD_FAILED) == "transient_error"


def test_model_metadata_not_installed_is_classified_as_transient(monkeypatch):
    """A tag that isn't installed yet could simply not be downloaded - it is
    not evidence this hardware can't run the model."""

    def fake_request(method, path, payload=None, timeout=10):
        assert path == "/api/tags"
        return {"models": []}

    monkeypatch.setattr(quality, "_request_json", fake_request)

    with pytest.raises(quality.QualityEvaluationError) as excinfo:
        quality._model_metadata("missing:latest")
    assert excinfo.value.failure_reason == quality.FAILURE_REASON_MODEL_LOAD_FAILED
    assert quality.outcome_for_failure_reason(excinfo.value.failure_reason) == "transient_error"


def test_collect_evidence_classifies_missing_model_file_as_transient_not_unfit(monkeypatch):
    monkeypatch.setattr(quality, "ollama_version", lambda: "0.32.1")

    def raising_metadata(tag):
        raise quality.QualityEvaluationError(
            f"Ollama model '{tag}' is not installed", failure_reason=quality.FAILURE_REASON_MODEL_LOAD_FAILED
        )

    def raising_evaluate(tag, pack, speed_runs=3):
        raise quality.QualityEvaluationError(
            f"Ollama model '{tag}' is not installed", failure_reason=quality.FAILURE_REASON_MODEL_LOAD_FAILED
        )

    monkeypatch.setattr(quality, "_model_metadata", raising_metadata)
    monkeypatch.setattr(quality, "evaluate_model", raising_evaluate)
    monkeypatch.setattr(quality, "unload_model", lambda tag: False)

    report = quality.collect_evidence(["missing:latest"], _hardware())

    entry = report["models"][0]
    assert entry["outcome"] == "transient_error"
    assert entry["failure_reason"] == "model_load_failed"


def test_failure_entry_never_leaks_raw_exception_text_paths_or_ips(monkeypatch):
    # See test_collect_evidence_classifies_daemon_unreachable_as_transient:
    # a healthy version response lets collect_evidence's daemon precheck
    # through so the raising _model_metadata/evaluate_model below are what
    # actually produce the failure entry under test.
    monkeypatch.setattr(quality, "ollama_version", lambda: "0.32.1")
    secret_message = "C:\\Users\\alice\\secret\\path connection refused by 10.0.0.5"

    def raising_metadata(tag):
        raise quality.QualityEvaluationError(secret_message, failure_reason=quality.FAILURE_REASON_CONNECTION_ERROR)

    def raising_evaluate(tag, pack, speed_runs=3):
        raise quality.QualityEvaluationError(secret_message, failure_reason=quality.FAILURE_REASON_CONNECTION_ERROR)

    monkeypatch.setattr(quality, "_model_metadata", raising_metadata)
    monkeypatch.setattr(quality, "evaluate_model", raising_evaluate)
    monkeypatch.setattr(quality, "unload_model", lambda tag: True)

    report = quality.collect_evidence(["small:latest"], _hardware())

    serialized = json.dumps(report)
    assert secret_message not in serialized
    assert "10.0.0.5" not in serialized
    assert "alice" not in serialized
    entry = report["models"][0]
    assert entry["failure_reason"] == "connection_error"
    assert set(entry.keys()) <= {
        "tag", "outcome", "failure_reason", "measurement_isolation", "model_metadata", "attempted_runtime",
    }


# --- performance_unfit confirmation flow (--confirm-performance-timeout) --
#
# These tests mock at the _evaluate_tag_once boundary (one full attempt),
# not evaluate_model directly. _evaluate_tag_once has only a TypeError
# compatibility fallback for legacy callable signatures; real evaluator
# failures are never retried there.


def _ok_metadata(tag):
    return {
        "tag": tag, "digest": "abc123", "size_bytes": 1_000_000,
        "parameter_size": "7B", "quantization_level": "Q4_0",
    }


def test_evaluate_tag_once_does_not_retry_real_timeout_as_legacy_fallback(monkeypatch):
    calls = []
    monkeypatch.setattr(quality, "_model_metadata", _ok_metadata)
    monkeypatch.setattr(
        quality.tuning,
        "recommend_runtime_settings",
        lambda hardware, metadata: type(
            "Profile",
            (),
            {
                "ollama_options": {"num_ctx": 2048},
                "context_length": 2048,
                "gpu_offload_percent": 100,
                "cpu_threads": 8,
                "num_batch": 512,
            },
        )(),
    )

    def timeout(*args, **kwargs):
        calls.append(kwargs)
        raise quality.QualityEvaluationError(
            "timed out", failure_reason=quality.FAILURE_REASON_GENERATION_TIMEOUT
        )

    monkeypatch.setattr(quality, "evaluate_model", timeout)
    monkeypatch.setattr(quality, "unload_model", lambda tag: True)

    entry = quality._evaluate_tag_once("big:latest", _hardware(), {}, 3)

    assert entry["outcome"] == "transient_error"
    assert entry["failure_reason"] == "generation_timeout"
    assert len(calls) == 1


def _timeout_entry(tag="big:latest"):
    return {"tag": tag, "outcome": "transient_error", "failure_reason": "generation_timeout"}


def _oom_entry(tag="big:latest"):
    return {"tag": tag, "outcome": "model_unfit", "failure_reason": "out_of_memory"}


def _success_entry(tag="big:latest", tokens_per_sec=12.3):
    return {"tag": tag, "outcome": "success", "speed": {"median_tokens_per_sec": tokens_per_sec}}


def _patch_confirmation_plumbing(monkeypatch, *, ollama_version="0.32.1", unload_confirmed=True):
    """Common non-behavioral mocks for confirmation-flow tests: a healthy
    daemon, an always-available model, an unload that's confirmed by
    default, and a no-op sleep (used only inside ensure_model_unloaded's
    own polling, never as a substitute for that confirmation). Tests
    control the interesting part (each attempt's outcome) themselves via a
    fake `_evaluate_tag_once`."""
    version_calls = {"n": 0}

    def fake_ollama_version():
        version_calls["n"] += 1
        # collect_evidence's own daemon precheck fires before every tag,
        # including the first - it must see a healthy daemon here so the
        # (mocked) _evaluate_tag_once actually runs. Only later calls (e.g.
        # _confirm_generation_timeout's own health check) reflect the
        # scenario a given test is set up to exercise.
        if version_calls["n"] == 1:
            return "0.32.1"
        return ollama_version

    monkeypatch.setattr(quality, "ollama_version", fake_ollama_version)
    monkeypatch.setattr(quality, "_model_metadata", _ok_metadata)
    monkeypatch.setattr(quality, "ensure_model_unloaded", lambda tag, **k: unload_confirmed)
    monkeypatch.setattr(quality.time, "sleep", lambda seconds: None)


def test_default_mode_single_timeout_is_transient_error_never_confirmed(monkeypatch):
    """1. Default benchmark behavior is unchanged: one timeout is one
    transient_error, with no retry and no confirmation_attempts field."""
    _patch_confirmation_plumbing(monkeypatch)
    calls = {"n": 0}

    def fake_attempt(tag, hardware, pack, speed_runs):
        calls["n"] += 1
        return _timeout_entry(tag)

    monkeypatch.setattr(quality, "_evaluate_tag_once", fake_attempt)

    report = quality.collect_evidence(["big:latest"], _hardware())  # confirm_performance_timeout omitted

    entry = report["models"][0]
    assert entry["outcome"] == "transient_error"
    assert entry["failure_reason"] == "generation_timeout"
    assert calls["n"] == 1
    assert "confirmation_attempts" not in entry
    assert "timeout_seconds" not in entry


def test_evaluate_tag_once_passes_only_supported_optional_keywords(monkeypatch):
    calls = []

    def evaluator(tag, pack, speed_runs=3, runtime_options=None):
        calls.append((tag, speed_runs, runtime_options))
        return {
            "tag": tag,
            "speed": {"median_tokens_per_sec": 10.0},
            "quality": None,
            "runtime": None,
        }

    monkeypatch.setattr(quality, "evaluate_model", evaluator)
    monkeypatch.setattr(quality, "_model_metadata", _ok_metadata)
    monkeypatch.setattr(
        quality.tuning,
        "recommend_runtime_settings",
        lambda hardware, metadata: type(
            "Profile", (), {"ollama_options": {"num_ctx": 2048}}
        )(),
    )
    monkeypatch.setattr(quality, "unload_model", lambda tag: True)

    result = quality._evaluate_tag_once(
        "model:latest", _hardware(), {"items": []}, speed_runs=3
    )

    assert result["outcome"] == "success"
    assert calls == [("model:latest", 3, {"num_ctx": 2048})]


def test_confirm_mode_second_attempt_succeeds_reports_real_success(monkeypatch):
    """2. Confirmation mode, first timeout then a real success: outcome is
    success with a genuine measurement, never a fabricated speed."""
    _patch_confirmation_plumbing(monkeypatch)
    calls = {"n": 0}

    def fake_attempt(tag, hardware, pack, speed_runs):
        calls["n"] += 1
        return _timeout_entry(tag) if calls["n"] == 1 else _success_entry(tag)

    monkeypatch.setattr(quality, "_evaluate_tag_once", fake_attempt)

    report = quality.collect_evidence(["big:latest"], _hardware(), confirm_performance_timeout=True)

    entry = report["models"][0]
    assert entry["outcome"] == "success"
    assert entry["speed"]["median_tokens_per_sec"] == 12.3
    assert calls["n"] == 2
    assert "confirmation_attempts" not in entry


def test_confirm_mode_two_confirmed_timeouts_is_performance_unfit(monkeypatch):
    """3 & 9. Two generation_timeouts in a row under a healthy daemon become
    performance_unfit/confirmed_generation_timeout, with confirmation_attempts=2,
    a real timeout_seconds, and no fabricated or leftover speed fields."""
    _patch_confirmation_plumbing(monkeypatch)
    calls = {"n": 0}

    def fake_attempt(tag, hardware, pack, speed_runs):
        calls["n"] += 1
        return _timeout_entry(tag)

    monkeypatch.setattr(quality, "_evaluate_tag_once", fake_attempt)

    report = quality.collect_evidence(["big:latest"], _hardware(), confirm_performance_timeout=True)

    entry = report["models"][0]
    assert entry["outcome"] == "performance_unfit"
    assert entry["failure_reason"] == "confirmed_generation_timeout"
    assert entry["confirmation_attempts"] == 2
    assert entry["timeout_seconds"] == quality.DEFAULT_GENERATION_TIMEOUT_SECONDS
    assert calls["n"] == 2
    for forbidden in ("tokens_per_sec", "tokens_per_sec_min", "tokens_per_sec_max", "sample_count"):
        assert forbidden not in entry


@pytest.mark.parametrize("attempt_of_failure", [1, 2])
def test_confirm_mode_explicit_oom_is_model_unfit(monkeypatch, attempt_of_failure):
    """4. An explicit OOM on either the first or the confirmation attempt is
    model_unfit - OOM is decisive and is never itself retried a third time."""
    _patch_confirmation_plumbing(monkeypatch)
    calls = {"n": 0}

    def fake_attempt(tag, hardware, pack, speed_runs):
        calls["n"] += 1
        if calls["n"] == 1 and attempt_of_failure == 2:
            return _timeout_entry(tag)
        return _oom_entry(tag)

    monkeypatch.setattr(quality, "_evaluate_tag_once", fake_attempt)

    report = quality.collect_evidence(["big:latest"], _hardware(), confirm_performance_timeout=True)

    entry = report["models"][0]
    assert entry["outcome"] == "model_unfit"
    assert entry["failure_reason"] == "out_of_memory"
    assert calls["n"] == attempt_of_failure
    assert "confirmation_attempts" not in entry


def test_confirm_mode_daemon_down_before_confirmation_is_transient_error(monkeypatch):
    """5. If the daemon health check fails before the confirmation attempt
    even starts, the result is transient_error, not performance_unfit -
    a dead daemon proves nothing about the model's own performance."""
    _patch_confirmation_plumbing(monkeypatch, ollama_version=None)
    calls = {"n": 0}

    def fake_attempt(tag, hardware, pack, speed_runs):
        calls["n"] += 1
        return _timeout_entry(tag)

    monkeypatch.setattr(quality, "_evaluate_tag_once", fake_attempt)

    report = quality.collect_evidence(["big:latest"], _hardware(), confirm_performance_timeout=True)

    entry = report["models"][0]
    assert entry["outcome"] == "transient_error"
    assert entry["failure_reason"] == "ollama_unavailable"
    assert calls["n"] == 1  # the confirmation attempt itself never ran


def test_confirm_mode_model_gone_before_confirmation_is_transient_error(monkeypatch):
    """5 (variant) & 15. If the model itself is no longer available at
    confirmation time, that's transient_error, not performance_unfit - and
    a secret-laden exception message from that check never reaches the
    final event (_build_failure_entry only keeps the structured reason)."""
    _patch_confirmation_plumbing(monkeypatch)
    calls = {"n": 0}
    secret_message = "C:\\Users\\alice\\secret - model gone, connection refused by 10.0.0.5"

    def missing_metadata(tag):
        raise quality.QualityEvaluationError(secret_message, failure_reason=quality.FAILURE_REASON_MODEL_LOAD_FAILED)

    monkeypatch.setattr(quality, "_model_metadata", missing_metadata)

    def fake_attempt(tag, hardware, pack, speed_runs):
        calls["n"] += 1
        return _timeout_entry(tag)

    monkeypatch.setattr(quality, "_evaluate_tag_once", fake_attempt)

    report = quality.collect_evidence(["big:latest"], _hardware(), confirm_performance_timeout=True)

    entry = report["models"][0]
    assert entry["outcome"] == "transient_error"
    assert entry["failure_reason"] == "model_load_failed"
    assert calls["n"] == 1
    serialized = json.dumps(report)
    assert secret_message not in serialized
    assert "alice" not in serialized
    assert "10.0.0.5" not in serialized


def test_confirm_mode_second_attempt_waits_for_confirmed_unload_not_a_fixed_sleep(monkeypatch):
    """6 & 7. A requests.ReadTimeout only ends the client's own wait - it is
    not proof Ollama's generation goroutine actually stopped. The second
    attempt must not run until ensure_model_unloaded has *confirmed* the
    model is gone (via /api/ps), not merely after some fixed delay."""
    _patch_confirmation_plumbing(monkeypatch)
    events: list = []

    def fake_attempt(tag, hardware, pack, speed_runs):
        events.append("attempt")
        return _timeout_entry(tag) if events.count("attempt") == 1 else _success_entry(tag)

    monkeypatch.setattr(quality, "_evaluate_tag_once", fake_attempt)
    monkeypatch.setattr(
        quality, "ensure_model_unloaded", lambda tag, **k: events.append("unload_confirmed") or True
    )

    quality.collect_evidence(["big:latest"], _hardware(), confirm_performance_timeout=True)

    assert events == ["attempt", "unload_confirmed", "attempt"]


def test_confirm_mode_second_attempt_not_issued_before_unload_is_confirmed(monkeypatch):
    """1. The second (confirmation) request is never called until
    ensure_model_unloaded has returned - proving the two generation
    requests never overlap inside the daemon."""
    _patch_confirmation_plumbing(monkeypatch)
    order: list = []

    def fake_ensure_unloaded(tag, **k):
        order.append("ensure_model_unloaded_start")
        order.append("ensure_model_unloaded_done")
        return True

    def fake_attempt(tag, hardware, pack, speed_runs):
        order.append("attempt")
        return _timeout_entry(tag) if order.count("attempt") == 1 else _success_entry(tag)

    monkeypatch.setattr(quality, "ensure_model_unloaded", fake_ensure_unloaded)
    monkeypatch.setattr(quality, "_evaluate_tag_once", fake_attempt)

    quality.collect_evidence(["big:latest"], _hardware(), confirm_performance_timeout=True)

    # The unload confirmation fully completes between the two attempts -
    # never interleaved with, or skipped before, the second attempt.
    assert order == [
        "attempt", "ensure_model_unloaded_start", "ensure_model_unloaded_done", "attempt",
    ]


def test_confirm_mode_unload_not_confirmed_is_transient_error_and_skips_second_attempt(monkeypatch):
    """1, 5 & 9. If the model can't be confirmed unloaded within the bounded
    wait, the second request is never issued, the result is
    transient_error (never model_unfit/performance_unfit - unload failure
    says nothing about the model itself), and exactly one attempt ran."""
    _patch_confirmation_plumbing(monkeypatch, unload_confirmed=False)
    calls = {"n": 0}

    def fake_attempt(tag, hardware, pack, speed_runs):
        calls["n"] += 1
        return _timeout_entry(tag)

    monkeypatch.setattr(quality, "_evaluate_tag_once", fake_attempt)

    report = quality.collect_evidence(["big:latest"], _hardware(), confirm_performance_timeout=True)

    entry = report["models"][0]
    assert entry["outcome"] == "transient_error"
    assert entry["outcome"] not in ("model_unfit", "performance_unfit")
    assert calls["n"] == 1  # the confirmation attempt itself never ran
    assert "confirmation_attempts" not in entry


def test_ensure_model_unloaded_is_called_with_the_correct_tag_before_confirmation(monkeypatch):
    _patch_confirmation_plumbing(monkeypatch)
    seen = {}

    def fake_ensure_unloaded(tag, **k):
        seen["tag"] = tag
        return True

    monkeypatch.setattr(quality, "ensure_model_unloaded", fake_ensure_unloaded)
    monkeypatch.setattr(quality, "_evaluate_tag_once", lambda tag, hardware, pack, speed_runs: _timeout_entry(tag))

    quality.collect_evidence(["big:latest"], _hardware(), confirm_performance_timeout=True)

    assert seen["tag"] == "big:latest"


@pytest.mark.parametrize(
    "second_outcome_fn",
    [_success_entry, _oom_entry, _timeout_entry],
    ids=["success", "model_unfit", "performance_unfit"],
)
def test_confirm_mode_cleans_up_after_the_confirmation_attempt_regardless_of_outcome(
    monkeypatch, second_outcome_fn
):
    """8 & cleanup tests. After the confirmation attempt finishes, the model
    is unloaded again as best-effort final cleanup - for every possible
    verdict, not just performance_unfit."""
    _patch_confirmation_plumbing(monkeypatch)
    calls = {"n": 0}
    unload_calls = []

    def fake_attempt(tag, hardware, pack, speed_runs):
        calls["n"] += 1
        return _timeout_entry(tag) if calls["n"] == 1 else second_outcome_fn(tag)

    monkeypatch.setattr(quality, "_evaluate_tag_once", fake_attempt)
    monkeypatch.setattr(quality, "unload_model", lambda tag: unload_calls.append(tag) or True)

    quality.collect_evidence(["big:latest"], _hardware(), confirm_performance_timeout=True)

    assert unload_calls == ["big:latest"]


def test_confirm_mode_final_cleanup_failure_does_not_change_the_verdict(monkeypatch):
    """Cleanup failure must never flip or corrupt an already-decided
    outcome - unload_model already swallows its own errors and returns a
    bool, so a False here must be silently ignored."""
    _patch_confirmation_plumbing(monkeypatch)
    calls = {"n": 0}

    def fake_attempt(tag, hardware, pack, speed_runs):
        calls["n"] += 1
        return _timeout_entry(tag)

    monkeypatch.setattr(quality, "_evaluate_tag_once", fake_attempt)
    monkeypatch.setattr(quality, "unload_model", lambda tag: False)  # final cleanup "fails"

    report = quality.collect_evidence(["big:latest"], _hardware(), confirm_performance_timeout=True)

    entry = report["models"][0]
    assert entry["outcome"] == "performance_unfit"
    assert entry["failure_reason"] == "confirmed_generation_timeout"
    assert entry["confirmation_attempts"] == 2


# --- ensure_model_unloaded / _model_is_loaded: bounded polling, not a sleep


def test_ensure_model_unloaded_confirms_immediately_without_sleeping(monkeypatch):
    monkeypatch.setattr(quality, "unload_model", lambda tag: True)
    polls = {"n": 0}

    def fake_is_loaded(tag):
        polls["n"] += 1
        return False

    monkeypatch.setattr(quality, "_model_is_loaded", fake_is_loaded)
    slept = []
    monkeypatch.setattr(quality.time, "sleep", lambda seconds: slept.append(seconds))

    assert quality.ensure_model_unloaded("big:latest") is True
    assert polls["n"] == 1
    assert slept == []


def test_ensure_model_unloaded_polls_until_confirmed_gone(monkeypatch):
    monkeypatch.setattr(quality, "unload_model", lambda tag: True)
    remaining = [True, True, False]
    monkeypatch.setattr(quality, "_model_is_loaded", lambda tag: remaining.pop(0))
    slept = []
    monkeypatch.setattr(quality.time, "sleep", lambda seconds: slept.append(seconds))

    result = quality.ensure_model_unloaded("big:latest", max_wait_seconds=10, poll_interval_seconds=1)

    assert result is True
    assert slept == [1, 1]


def test_ensure_model_unloaded_never_polls_indefinitely(monkeypatch):
    """4. Bounded polling: gives up at max_wait_seconds rather than looping
    forever when the model stays (or appears to stay) loaded."""
    monkeypatch.setattr(quality, "unload_model", lambda tag: True)
    monkeypatch.setattr(quality, "_model_is_loaded", lambda tag: True)  # never confirms
    slept = []
    monkeypatch.setattr(quality.time, "sleep", lambda seconds: slept.append(seconds))

    result = quality.ensure_model_unloaded("big:latest", max_wait_seconds=3, poll_interval_seconds=1)

    assert result is False
    assert slept == [1, 1, 1]  # exactly bounded, not unbounded


def test_ensure_model_unloaded_treats_unreachable_daemon_as_not_confirmed(monkeypatch):
    """An /api/ps that can't even be queried is never treated as proof the
    model is gone - that would defeat the whole point of confirming."""
    monkeypatch.setattr(quality, "unload_model", lambda tag: True)
    monkeypatch.setattr(quality, "_model_is_loaded", lambda tag: None)
    monkeypatch.setattr(quality.time, "sleep", lambda seconds: None)

    assert quality.ensure_model_unloaded("big:latest", max_wait_seconds=2, poll_interval_seconds=1) is False


def test_ensure_model_unloaded_calls_ollamas_own_stop_api_not_a_process_kill(monkeypatch):
    calls = []
    monkeypatch.setattr(quality, "unload_model", lambda tag: calls.append(tag) or True)
    monkeypatch.setattr(quality, "_model_is_loaded", lambda tag: False)

    quality.ensure_model_unloaded("big:latest")

    assert calls == ["big:latest"]  # only Ollama's own keep_alive=0 endpoint, never a subprocess signal


def test_model_is_loaded_true_when_tag_present_in_api_ps(monkeypatch):
    monkeypatch.setattr(
        quality, "_request_json",
        lambda method, path, payload=None, timeout=10: {"models": [{"name": "big:latest"}]},
    )
    assert quality._model_is_loaded("big:latest") is True
    assert quality._model_is_loaded("other:latest") is False


def test_model_is_loaded_returns_none_when_daemon_unreachable(monkeypatch):
    def raising(method, path, payload=None, timeout=10):
        raise quality.QualityEvaluationError("down", failure_reason=quality.FAILURE_REASON_OLLAMA_UNAVAILABLE)

    monkeypatch.setattr(quality, "_request_json", raising)

    assert quality._model_is_loaded("big:latest") is None


def test_performance_unfit_entry_has_no_stray_or_speed_fields(monkeypatch):
    """9 & 15. A performance_unfit event carries only the documented v7
    failure fields plus the two confirmation fields - nothing else leaks
    in from the underlying attempt dicts."""
    _patch_confirmation_plumbing(monkeypatch)

    def fake_attempt(tag, hardware, pack, speed_runs):
        return _timeout_entry(tag)

    monkeypatch.setattr(quality, "_evaluate_tag_once", fake_attempt)

    report = quality.collect_evidence(["big:latest"], _hardware(), confirm_performance_timeout=True)

    entry = report["models"][0]
    assert entry["outcome"] == "performance_unfit"
    assert set(entry.keys()) <= {
        "tag", "outcome", "failure_reason", "measurement_isolation", "model_metadata",
        "attempted_runtime", "confirmation_attempts", "timeout_seconds",
    }


def test_outcome_for_confirmed_generation_timeout_is_performance_unfit():
    assert quality.outcome_for_failure_reason(
        quality.FAILURE_REASON_CONFIRMED_GENERATION_TIMEOUT
    ) == "performance_unfit"
    assert quality.FAILURE_REASON_CONFIRMED_GENERATION_TIMEOUT in quality.PERFORMANCE_UNFIT_REASONS
    assert quality.FAILURE_REASON_CONFIRMED_GENERATION_TIMEOUT not in quality.MODEL_UNFIT_REASONS
    assert quality.FAILURE_REASON_CONFIRMED_GENERATION_TIMEOUT not in quality.TRANSIENT_ERROR_REASONS


def test_write_evidence_raises_quality_evaluation_error_on_write_failure(tmp_path, monkeypatch):
    from pathlib import Path

    bad_path = tmp_path / "does-not-exist-parent" / "evidence.json"
    monkeypatch.setattr(
        Path, "mkdir", lambda self, parents=True, exist_ok=True: (_ for _ in ()).throw(OSError("permission denied"))
    )

    try:
        quality.write_evidence({"models": []}, bad_path)
        assert False, "expected QualityEvaluationError"
    except quality.QualityEvaluationError:
        pass
