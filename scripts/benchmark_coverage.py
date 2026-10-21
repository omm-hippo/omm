"""Produce a reproducible, local-only benchmark coverage inventory (#426)."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from omm.atomic import atomic_write_text
from omm.featurize import FEATURE_ORDER


def _dimensions(features: dict) -> dict[str, str]:
    def number(name):
        value = features.get(name)
        return f"{value:g}" if isinstance(value, (int, float)) else "unknown"

    return {
        "engine": "lmstudio" if features.get("engine_lmstudio") else "llamacpp" if features.get("engine_llamacpp") else "ollama",
        "memory_type": "unified" if features.get("unified_memory") else "separate_vram" if features.get("vram_gb", 0) > 0 else "cpu_only",
        "memory": "ram=" + number("ram_gb") + ",vram=" + number("vram_gb"),
        "cpu_profile": "score=" + number("cpu_score") + ",tier=" + number("cpu_tier"),
        "gpu_profile": "score=" + number("gpu_score") + ",tier=" + number("gpu_tier"),
        "quant_bits": number("quant_bits"),
        "context_length": number("context_length"),
        "model_parameters_b": number("param_count_b"),
    }


def build_coverage(order, training_X, holdout_X=(), *, audit=None, synthetic_rows=None) -> dict:
    groups = defaultdict(lambda: {"training": [], "holdout": []})
    hardware = set()
    for split, rows in (("training", training_X), ("holdout", holdout_X)):
        for row in rows:
            features = dict(zip(order, row))
            for dimension, value in _dimensions(features).items():
                groups[(dimension, value)][split].append(tuple(row))
            hardware.add(tuple(features.get(key) for key in (
                "ram_gb", "vram_gb", "unified_memory", "cpu_score", "cpu_tier", "gpu_score", "gpu_tier"
            )))
    slices = []
    for (dimension, label), members in sorted(groups.items()):
        slices.append({
            "dimension": dimension, "group": label,
            "training_rows": len(members["training"]),
            "training_configurations": len(set(members["training"])),
            "holdout_rows": len(members["holdout"]),
            "holdout_configurations": len(set(members["holdout"])),
            "validation_status": "holdout_present" if members["holdout"] else "not_validated",
        })
    engines = {label for dimension, label in groups if dimension == "engine"}
    priorities = [
        {"dimension": "engine", "group": engine, "reason": "no_observed_configurations"}
        for engine in ("ollama", "lmstudio", "llamacpp") if engine not in engines
    ]
    priorities.extend(
        {"dimension": item["dimension"], "group": item["group"],
         "reason": "no_holdout_evidence" if item["holdout_configurations"] == 0 else "few_observed_configurations"}
        for item in slices if item["holdout_configurations"] == 0 or item["training_configurations"] + item["holdout_configurations"] < 3
    )
    data = audit or {}
    return {
        "schema_version": 1,
        "raw_measurement_rows": data.get("raw_rows"),
        "accepted_measurement_rows": data.get("valid_rows"),
        "rejected_measurement_rows": data.get("rejected_rows"),
        "repeated_rows_collapsed": data.get("duplicates_collapsed"),
        "real_training_configurations": len(set(map(tuple, training_X))),
        "real_holdout_configurations": len(set(map(tuple, holdout_X))),
        "synthetic_training_rows": synthetic_rows,
        "encoded_hardware_profiles": len(hardware),
        "independent_device_count": None,
        "slices": slices,
        "collection_priorities": priorities,
        "limits": [
            "Feature configurations and encoded hardware profiles are not independent physical devices.",
            "The benchmark schema has no authenticated device identity; independent device count is unknown.",
            "Holdout presence is not a confidence interval or a measurement of prediction accuracy.",
            "Synthetic prior rows are excluded from all observed coverage counts.",
        ],
    }


def markdown(report: dict) -> str:
    rows = ["# Benchmark coverage", "", "Observed configurations are not independent devices.", "",
            "| Dimension | Group | Training configurations | Holdout configurations |",
            "| --- | --- | ---: | ---: |"]
    rows.extend(f"| {x['dimension']} | {x['group']} | {x['training_configurations']} | {x['holdout_configurations']} |" for x in report["slices"])
    rows.extend(["", "## Collection priorities", ""])
    rows.extend(f"- {x['dimension']}: {x['group']} ({x['reason']})" for x in report["collection_priorities"])
    rows.extend(["", "## Limits", "", *[f"- {x}" for x in report["limits"]], ""])
    return "\n".join(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--telemetry-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args()
    from scripts.train_model import load_telemetry_file, real_rows_to_training_data_with_audit

    try:
        source = load_telemetry_file(args.telemetry_file)
        X, _y, audit = real_rows_to_training_data_with_audit(source)
        report = build_coverage(FEATURE_ORDER, X, audit=audit, synthetic_rows=0)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(args.output, json.dumps(report, indent=2, allow_nan=False) + "\n")
        if args.markdown:
            args.markdown.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(args.markdown, markdown(report))
    except (OSError, ValueError) as error:
        parser.exit(1, f"Coverage report failed: {error}\n")
    print(f"Wrote coverage for {len(X)} observed configurations; independent device count is unknown.")


if __name__ == "__main__":
    main()
