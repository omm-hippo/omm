import json
import subprocess
import sys

from omm.featurize import FEATURE_ORDER
from scripts.benchmark_coverage import build_coverage, markdown
from scripts.recommendation_diagnostics import build_report


def row(**values):
    fields = dict(ram_gb=16, vram_gb=0, param_count_b=3, quant_bits=4, cpu_threads=4, context_length=1024)
    fields.update(values)
    return [fields.get(key, 0) for key in FEATURE_ORDER]


def test_repetition_and_device_diversity_are_not_interchanged():
    report = build_coverage(FEATURE_ORDER, [row(), row()], [row(engine_lmstudio=1)], audit={"raw_rows": 100, "valid_rows": 100, "duplicates_collapsed": 98})
    assert report["raw_measurement_rows"] == 100
    assert report["real_training_configurations"] == 1
    assert report["real_holdout_configurations"] == 1
    assert report["encoded_hardware_profiles"] == 1
    assert report["independent_device_count"] is None
    assert any(x["group"] == "llamacpp" for x in report["collection_priorities"])


def test_fixed_inputs_produce_reproducible_report_without_source_identifiers():
    rows = [row(unified_memory=1), row(ram_gb=32, vram_gb=8)]
    a = build_coverage(FEATURE_ORDER, rows)
    assert a == build_coverage(FEATURE_ORDER, list(reversed(rows)))
    assert a["synthetic_training_rows"] is None
    assert "independent devices" in markdown(a)
    assert {x["group"] for x in a["slices"] if x["dimension"] == "memory_type"} == {"unified", "separate_vram"}


def test_training_diagnostics_keep_holdout_separate_from_synthetic_prior():
    artifact = {"feature_order": FEATURE_ORDER, "model_version": 4, "trees": [{"leaf": True, "value": 10}], "synthetic_row_count": 500}
    report = build_report(artifact, artifact, [row()], [10], [row(ram_gb=32)], [12], telemetry_audit={})
    assert report["observed_coverage"]["synthetic_training_rows"] == 500
    assert report["observed_coverage"]["real_training_configurations"] == 1
    assert report["observed_coverage"]["real_holdout_configurations"] == 1


def test_standalone_report_writes_reopenable_json_and_markdown(tmp_path):
    source = tmp_path / "empty.json"
    source.write_text("[]")
    output, text = tmp_path / "coverage.json", tmp_path / "coverage.md"
    result = subprocess.run([sys.executable, "scripts/benchmark_coverage.py", "--telemetry-file", str(source), "--output", str(output), "--markdown", str(text)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text())
    assert report["raw_measurement_rows"] == report["real_training_configurations"] == 0
    assert "Benchmark coverage" in text.read_text()
