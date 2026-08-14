from __future__ import annotations

from fastapi.testclient import TestClient

from localfit_server import app as server_app


def _event():
    return {
        "ram_gb": 16,
        "vram_gb": 16,
        "unified_memory": True,
        "model_installed": "model-3b-q4.gguf",
        "model_repo_id": "org/model",
        "model_size_bytes": 2_000_000_000,
        "engine": "llama.cpp",
        "benchmark_version": 4,
        "recorded_at": "2026-07-21T00:00:00Z",
        "tokens_per_sec": 19.2,
        "sample_count": 1,
        "tokens_per_sec_min": 19.2,
        "tokens_per_sec_max": 19.2,
    }


def test_self_hosted_collector_stores_and_exports_with_admin_token(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALFIT_DB_PATH", str(tmp_path / "benchmarks.sqlite3"))
    monkeypatch.setenv("LOCALFIT_ADMIN_TOKEN", "secret")
    server_app.get_store.cache_clear()
    client = TestClient(server_app.app)

    response = client.post("/v1/benchmarks", json=_event())
    assert response.status_code == 201
    duplicate = client.post("/v1/benchmarks", json=_event())
    assert duplicate.status_code == 201
    assert duplicate.json() == {"id": response.json()["id"], "status": "duplicate"}
    assert client.get("/v1/stats").json() == {"count": 1, "engines": {"llama.cpp": 1}}
    assert client.get("/v1/benchmarks/export").status_code == 401
    export = client.get(
        "/v1/benchmarks/export", headers={"Authorization": "Bearer secret"}
    )
    assert export.status_code == 200
    assert export.json()["benchmarks"][0]["tokens_per_sec"] == 19.2
    server_app.get_store.cache_clear()


def test_self_hosted_collector_rejects_unknown_or_private_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALFIT_DB_PATH", str(tmp_path / "benchmarks.sqlite3"))
    server_app.get_store.cache_clear()
    client = TestClient(server_app.app)
    event = _event()
    event["cpu_name"] = "private raw hardware name"

    assert client.post("/v1/benchmarks", json=event).status_code == 422
    server_app.get_store.cache_clear()


def test_self_hosted_collector_rejects_future_unknown_benchmark_version(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LOCALFIT_DB_PATH", str(tmp_path / "benchmarks.sqlite3"))
    server_app.get_store.cache_clear()
    client = TestClient(server_app.app)
    event = _event()
    event["benchmark_version"] = 9

    assert client.post("/v1/benchmarks", json=event).status_code == 422
    server_app.get_store.cache_clear()


def test_self_hosted_collector_rejects_inconsistent_sample_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALFIT_DB_PATH", str(tmp_path / "benchmarks.sqlite3"))
    server_app.get_store.cache_clear()
    client = TestClient(server_app.app)
    event = _event()
    event["tokens_per_sec_min"] = 30
    event["tokens_per_sec_max"] = 40

    assert client.post("/v1/benchmarks", json=event).status_code == 422
    server_app.get_store.cache_clear()


def _quality_fields():
    return {
        "quality_pack_id": "localfit-gsm8k-bilingual-smoke",
        "quality_pack_version": "1.1.0",
        "quality_correct": 6,
        "quality_total": 8,
        "quality_accuracy": 0.75,
    }


def _v5_event():
    event = _event()
    event.update(
        benchmark_version=5,
        model_installed="opaque-model-name.gguf",
        model_filename="opaque-model-name.gguf",
        model_digest="A" * 64,
        parameter_count_b=7.0,
        active_parameter_count_b=3.0,
        quant_bits=4.0,
        engine_version="0.3.1",
        client_version="1.2.3",
        runtime_profile="throughput",
        context_length=4096,
        gpu_offload_percent=100,
        cpu_threads=8,
        num_batch=512,
        sample_count=3,
        tokens_per_sec_min=18.0,
        tokens_per_sec_max=20.0,
    )
    return event


def _v7_success_event():
    event = _v5_event()
    event.update(
        benchmark_version=7,
        engine="ollama",
        outcome="success",
        model_provider="huggingface",
        cpu_model="Apple M4 Pro",
        cpu_arch="arm64",
        cpu_physical_cores=12,
        cpu_logical_cores=12,
    )
    return event


def _v8_success_event():
    event = _v7_success_event()
    event.update(
        benchmark_version=8,
        cpu_score=4100.0,
        cpu_tier=1.0,
        gpu_score=3200.0,
        gpu_tier=0.0,
    )
    event.pop("cpu_model")
    return event


def test_self_hosted_collector_accepts_optional_quality_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALFIT_DB_PATH", str(tmp_path / "benchmarks.sqlite3"))
    server_app.get_store.cache_clear()
    client = TestClient(server_app.app)
    event = _event()
    event.update(_quality_fields())

    response = client.post("/v1/benchmarks", json=event)

    assert response.status_code == 201
    server_app.get_store.cache_clear()


def test_self_hosted_collector_rejects_partial_quality_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALFIT_DB_PATH", str(tmp_path / "benchmarks.sqlite3"))
    server_app.get_store.cache_clear()
    client = TestClient(server_app.app)
    event = _event()
    event["quality_correct"] = 6

    assert client.post("/v1/benchmarks", json=event).status_code == 422
    server_app.get_store.cache_clear()


def test_self_hosted_collector_rejects_correct_over_total(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALFIT_DB_PATH", str(tmp_path / "benchmarks.sqlite3"))
    server_app.get_store.cache_clear()
    client = TestClient(server_app.app)
    event = _event()
    event.update(_quality_fields())
    event["quality_correct"] = 9

    assert client.post("/v1/benchmarks", json=event).status_code == 422
    server_app.get_store.cache_clear()


def test_v5_event_stores_normalized_metadata_and_exact_retries_deduplicate(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALFIT_DB_PATH", str(tmp_path / "benchmarks.sqlite3"))
    monkeypatch.setenv("LOCALFIT_ADMIN_TOKEN", "secret")
    server_app.get_store.cache_clear()
    client = TestClient(server_app.app)
    event = _v5_event()

    first = client.post("/v1/benchmarks", json=event)
    second = client.post("/v1/benchmarks", json=event)
    exported = client.get("/v1/benchmarks/export", headers={"Authorization": "Bearer secret"})

    assert first.status_code == second.status_code == 201
    assert second.json() == {"id": first.json()["id"], "status": "duplicate"}
    assert exported.json()["benchmarks"][0]["model_digest"] == "a" * 64
    server_app.get_store.cache_clear()


def test_v5_rejects_missing_or_invalid_requirements_and_quality_mismatch(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALFIT_DB_PATH", str(tmp_path / "benchmarks.sqlite3"))
    server_app.get_store.cache_clear()
    client = TestClient(server_app.app)
    for field, value in (
        ("parameter_count_b", None),
        ("cpu_threads", 0),
        ("sample_count", 2),
        ("engine_version", ""),
        ("model_filename", "C:\\private\\model.gguf"),
    ):
        event = _v5_event()
        event[field] = value
        assert client.post("/v1/benchmarks", json=event).status_code == 422
    event = _v5_event()
    event.update(_quality_fields(), quality_accuracy=0.751)
    assert client.post("/v1/benchmarks", json=event).status_code == 422
    event["quality_accuracy"] = 0.75005
    assert client.post("/v1/benchmarks", json=event).status_code == 201
    server_app.get_store.cache_clear()


def test_self_hosted_collector_accepts_current_v7_success_contract(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LOCALFIT_DB_PATH", str(tmp_path / "benchmarks.sqlite3"))
    monkeypatch.setenv("LOCALFIT_ADMIN_TOKEN", "secret")
    server_app.get_store.cache_clear()
    client = TestClient(server_app.app)

    response = client.post("/v1/benchmarks", json=_v7_success_event())
    exported = client.get(
        "/v1/benchmarks/export", headers={"Authorization": "Bearer secret"}
    )

    assert response.status_code == 201
    assert exported.status_code == 200
    assert exported.json()["benchmarks"][0]["outcome"] == "success"
    assert exported.json()["benchmarks"][0]["model_provider"] == "huggingface"
    server_app.get_store.cache_clear()


def test_self_hosted_collector_accepts_current_v7_failure_without_fake_speed(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LOCALFIT_DB_PATH", str(tmp_path / "benchmarks.sqlite3"))
    monkeypatch.setenv("LOCALFIT_ADMIN_TOKEN", "secret")
    server_app.get_store.cache_clear()
    client = TestClient(server_app.app)
    event = {
        "ram_gb": 16,
        "vram_gb": 8,
        "unified_memory": False,
        "model_installed": "model.gguf",
        "engine": "ollama",
        "benchmark_version": 7,
        "outcome": "model_unfit",
        "failure_reason": "out_of_memory",
        "recorded_at": "2026-07-31T00:00:00Z",
    }

    response = client.post("/v1/benchmarks", json=event)
    exported = client.get(
        "/v1/benchmarks/export", headers={"Authorization": "Bearer secret"}
    )

    assert response.status_code == 201
    stored = exported.json()["benchmarks"][0]
    assert stored["outcome"] == "model_unfit"
    assert "tokens_per_sec" not in stored
    server_app.get_store.cache_clear()


def test_self_hosted_collector_accepts_current_v8_success_contract(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LOCALFIT_DB_PATH", str(tmp_path / "benchmarks.sqlite3"))
    monkeypatch.setenv("LOCALFIT_ADMIN_TOKEN", "secret")
    server_app.get_store.cache_clear()
    client = TestClient(server_app.app)

    response = client.post("/v1/benchmarks", json=_v8_success_event())
    exported = client.get(
        "/v1/benchmarks/export", headers={"Authorization": "Bearer secret"}
    )

    assert response.status_code == 201
    stored = exported.json()["benchmarks"][0]
    assert stored["benchmark_version"] == 8
    assert stored["cpu_score"] == 4100.0
    assert "cpu_model" not in stored
    server_app.get_store.cache_clear()


def test_self_hosted_collector_accepts_current_v8_failure_without_raw_cpu(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LOCALFIT_DB_PATH", str(tmp_path / "benchmarks.sqlite3"))
    monkeypatch.setenv("LOCALFIT_ADMIN_TOKEN", "secret")
    server_app.get_store.cache_clear()
    client = TestClient(server_app.app)
    event = {
        "ram_gb": 16,
        "vram_gb": 8,
        "unified_memory": False,
        "model_installed": "model.gguf",
        "engine": "ollama",
        "benchmark_version": 8,
        "outcome": "model_unfit",
        "failure_reason": "out_of_memory",
        "recorded_at": "2026-08-15T00:00:00Z",
        "cpu_score": 4100.0,
        "cpu_tier": 1.0,
        "cpu_arch": "arm64",
        "cpu_physical_cores": 10,
        "cpu_logical_cores": 10,
    }

    response = client.post("/v1/benchmarks", json=event)
    exported = client.get(
        "/v1/benchmarks/export", headers={"Authorization": "Bearer secret"}
    )

    assert response.status_code == 201
    stored = exported.json()["benchmarks"][0]
    assert stored["outcome"] == "model_unfit"
    assert "tokens_per_sec" not in stored
    assert "cpu_model" not in stored
    server_app.get_store.cache_clear()


def test_self_hosted_collector_rejects_raw_cpu_model_in_v8(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALFIT_DB_PATH", str(tmp_path / "benchmarks.sqlite3"))
    server_app.get_store.cache_clear()
    client = TestClient(server_app.app)
    event = _v8_success_event()
    event["cpu_model"] = "Apple M4 Pro"

    assert client.post("/v1/benchmarks", json=event).status_code == 422
    server_app.get_store.cache_clear()


def test_self_hosted_collector_rejects_invalid_v7_outcome_combinations(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LOCALFIT_DB_PATH", str(tmp_path / "benchmarks.sqlite3"))
    server_app.get_store.cache_clear()
    client = TestClient(server_app.app)

    success = _v7_success_event()
    success["failure_reason"] = "out_of_memory"
    assert client.post("/v1/benchmarks", json=success).status_code == 422

    failure_with_speed = {
        "ram_gb": 16,
        "unified_memory": True,
        "model_installed": "model.gguf",
        "engine": "ollama",
        "benchmark_version": 7,
        "outcome": "model_unfit",
        "failure_reason": "out_of_memory",
        "recorded_at": "2026-07-31T00:00:00Z",
        "tokens_per_sec": 1,
    }
    assert client.post("/v1/benchmarks", json=failure_with_speed).status_code == 422
    server_app.get_store.cache_clear()
