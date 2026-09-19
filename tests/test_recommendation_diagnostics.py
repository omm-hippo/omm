from omm.featurize import FEATURE_ORDER
from scripts.recommendation_diagnostics import build_report, context_hash


def row(**values):
    features = dict(ram_gb=16, vram_gb=0, param_count_b=3, active_param_count_b=3,
                    quant_bits=4, model_size_gb=2, context_length=1024,
                    cpu_threads=4, num_batch=128)
    features.update(values)
    return [features.get(name, 0) for name in FEATURE_ORDER]


def artifact(value):
    return {"model_version": 4, "feature_order": FEATURE_ORDER, "trees": [{"leaf": True, "value": value}]}


def test_regression_report_locates_engine_and_memory_slices_without_model_names():
    report = build_report(artifact(20), artifact(10), [row(ram_gb=32)], [15],
                          [row(), row(param_count_b=7), row(engine_lmstudio=1)], [10, 12, 20],
                          telemetry_audit={"unique_configurations": 4})
    engines = {item["group"]: item for item in report["slices"] if item["dimension"] == "engine"}
    assert engines["ollama"]["mae_change"] == 8
    assert engines["lmstudio"]["mae_change"] == -10
    assert report["baseline_holdout_exposure"]["status"] == "unknown"
    assert report["collection_priorities"][0]["engine"] == "ollama"


def test_prior_training_overlap_is_reported_and_not_claimed_unseen():
    baseline = artifact(10)
    baseline["training_provenance"] = {"training_context_hashes": [context_hash(FEATURE_ORDER, row())]}
    report = build_report(artifact(20), baseline, [row(ram_gb=32)], [15], [row()], [10], telemetry_audit={})
    assert report["baseline_holdout_exposure"]["status"] == "known_overlap"
    assert len(report["baseline_holdout_exposure"]["overlapping_contexts"]) == 1


def test_changed_runtime_settings_are_visible_in_ranking_contexts():
    report = build_report(artifact(20), artifact(10), [row(ram_gb=32)], [15],
                          [row(), row(context_length=8192)], [10, 5], telemetry_audit={})
    assert report["worst_contexts"][0]["runtime_setting_count"] == 2
