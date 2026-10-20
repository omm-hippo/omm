from __future__ import annotations

import json

import pytest

pytest.importorskip("sklearn")

from scripts import measurement_balance, measurement_policy_compare as compare_mod


def _row(speed, **overrides):
    row = {
        "engine": "ollama", "benchmark_version": 8, "outcome": "success", "tokens_per_sec": speed,
        "ram_gb": 16, "vram_gb": 8, "gpu_tflops": 20, "unified_memory": False,
        "model_installed": "model-7B-Q4.gguf", "model_repo_id": "org/model-7B",
        "model_size_bytes": 4 * 1024**3, "context_length": 4096, "gpu_offload_percent": 100,
        "cpu_threads": 8, "num_batch": 512, "parameter_count_b": 7.0, "active_parameter_count_b": 7.0,
        "quant_bits": 4.0, "engine_version": "1.0", "client_version": "1.0",
        "runtime_profile": "throughput", "cpu_score": 7950.0, "cpu_tier": 1.0, "cpu_arch": "x86_64",
        "cpu_physical_cores": 8, "cpu_logical_cores": 16, "gpu_score": 4090.0, "gpu_tier": 0.0,
        "sample_count": 3, "tokens_per_sec_min": speed * 0.95, "tokens_per_sec_max": speed * 1.05,
    }
    row.update(overrides)
    return row


def _size(params):
    return {"parameter_count_b": float(params), "active_parameter_count_b": float(params),
            "model_installed": f"model-{params}B-Q4.gguf", "model_repo_id": f"org/model-{params}B",
            "model_size_bytes": int(params * 0.6 * 1024**3)}


def fixture_rows():
    rows = []
    # Normal configurations: a few honest repeats each.
    for params, speeds in ((1, [60, 62, 61]), (3, [35, 36]), (7, [20, 20.5, 19.8]), (13, [11, 12])):
        rows += [_row(s, **_size(params)) for s in speeds]
    # Rare: a single measurement on unusual hardware.
    rows.append(_row(14.0, ram_gb=64, vram_gb=24, gpu_score=4090.0, **_size(14)))
    # Slow but plausible: CPU-heavy offload.
    rows.append(_row(1.5, gpu_offload_percent=10, **_size(13)))
    # Repeated-manipulated: three honest values plus one plausible value
    # repeated many times on the 8B configuration.
    rows += [_row(s, **_size(8)) for s in (10, 15, 20)]
    rows += [_row(40.0 + i / 1000, **_size(8)) for i in range(40)]
    return rows


@pytest.fixture
def snapshot(tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(fixture_rows()), encoding="utf-8")
    return path


def test_report_compares_retention_targets_and_rankings(snapshot):
    from scripts import train_model

    report = compare_mod.compare(train_model.load_telemetry_file(snapshot))
    old, new = report["retention"]["old_median"], report["retention"]["repetition_balance"]
    assert old["raw_rows"] == new["raw_rows"] == len(fixture_rows())
    # Rare and slow rows are kept under both policies.
    assert new["rows_retained"] == old["rows_retained"] == len(fixture_rows())
    assert new["row_retention_rate"] == 1.0
    assert new["configurations"] == old["configurations"] == 7

    targets = report["targets"]
    assert targets["shared_configurations"] == 7
    [changed] = targets["changes"]  # only the manipulated configuration moves
    assert changed["old_target"] == pytest.approx(40.0185, abs=1e-3)
    assert changed["new_target"] == 17.5
    assert report["repetition_balance_audit"]["repeat_samples_collapsed"] > 0

    predictions = report["predictions"]
    assert predictions["status"] == "compared"
    assert predictions["candidates"] == 7
    ids = {item["id"] for item in predictions["items"]}
    assert changed["config"] in ids
    item = next(i for i in predictions["items"] if i["id"] == changed["config"])
    assert item["new_prediction"] < item["old_prediction"]


def test_output_is_deterministic_and_does_not_leak_rows(snapshot, tmp_path, capsys):
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    assert compare_mod.main(["--telemetry-file", str(snapshot), "--output", str(first)]) == 0
    assert compare_mod.main(["--telemetry-file", str(snapshot), "--output", str(second)]) == 0
    assert first.read_bytes() == second.read_bytes()
    text = first.read_text(encoding="utf-8")
    assert "org/model" not in text and "model_installed" not in text
    assert measurement_balance.balanced_speed.__name__ == "balanced_speed"  # policy swap restored


def test_fixed_candidate_set_ranks_within_groups(snapshot, tmp_path):
    from scripts import train_model

    rows = train_model.load_telemetry_file(snapshot)
    features, _targets, _audit = compare_mod.build_dataset(rows, "repetition_balance")
    keys = sorted(features)
    candidates = [{"id": f"c{i}", "group": "g1" if i % 2 else "g2", "features": features[k]} for i, k in enumerate(keys)]
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps(candidates), encoding="utf-8")
    report = compare_mod.compare(rows, path)["predictions"]
    assert report["groups"] == 2
    for group in ("g1", "g2"):
        members = [i for i in report["items"] if i["group"] == group]
        assert sorted(i["new_rank"] for i in members) == list(range(1, len(members) + 1))
        assert sorted(i["old_rank"] for i in members) == list(range(1, len(members) + 1))
