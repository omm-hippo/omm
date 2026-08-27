# Telemetry plausibility calibration — 2026-08-27

This report records the issue #216 decision against one read-only production
snapshot. Raw telemetry is not stored in the repository.

## Snapshot and method

- Snapshot rows: 638
- Snapshot SHA-256: `f15d60301ab498d9f5321febf0be6e2edc9c3ab492451365126e3770ce2364f6`
- Accepted before the statistical policy: 429 rows / 308 configurations
- Hardware buckets: 18
- Matrix: GPU/CPU bandwidth `4000/800`, `3000/600`, `2000/400` GB/s ×
  fence `1.5`, `2.0`, `3.0` × minimum sample `8`, `12`, `16` × low-side
  `remove`, `relaxed (2× lower fence)`, `report_only` = 81 combinations
- Privacy: configuration IDs below hash only the rounded training feature
  vector. Firebase IDs, timestamps, endpoints, tokens and user identifiers
  were not included.

All three bandwidth pairs rejected zero physical-ceiling rows. Minimum sample
8/12/16 changed how many of the 18 buckets used their own quartiles (10/9/8),
but did not change which configurations were flagged. The decisive variables
were the fence and low-side action:

| Fence | Low policy | High flags | Low flags | Configurations retained |
| ---: | --- | ---: | ---: | ---: |
| 1.5 | remove | 2 | 13 | 293 |
| 1.5 | report only | 2 | 13 | 306 |
| 2.0 | remove | 0 | 10 | 298 |
| 2.0 | report only | 0 | 10 | 308 |
| 3.0 | remove | 0 | 6 | 302 |
| 3.0 | report only | 0 | 6 | 308 |

The selected policy keeps the conservative `4000/800` physical ceilings,
fence `3.0`, and minimum sample `8`, while changing low-side handling to
`report_only`. It retains every one of the 308 configurations across
benchmark version, engine, hardware tier, dense/MoE family, and model-size
bins. High-side poisoning remains rejected.

## Review of the previously blocked ten configurations

The table reports only non-identifying configuration hashes and the fields
needed to review the gate. `BW` is implied memory bandwidth in GB/s.

| Hash | Direction | Family / quant | Speed | BW | Applied fence | Review |
| --- | --- | --- | ---: | ---: | --- | --- |
| `255e3b1fc5e2` | high | Qwen1.5 dense Q4 | 48.66 | 194.64 | bucket upper 56.19 | Metadata error: 1.8B was reported as 8B |
| `13072156c4b9` | high | Qwen1.5 dense Q4 | 73.56 | 294.24 | bucket upper 130.2205 | Metadata error: 1.8B was reported as 8B |
| `14c0dbd6be80` | high | Qwen1.5 dense Q4 | 132.75 | 331.875 | bucket upper 130.2205 | Metadata error: 0.5B was reported as 5B |
| `d880e3d5651c` | high | Qwen1.5 dense Q4 | 148.99 | 372.475 | bucket upper 56.19 | Metadata error: 0.5B was reported as 5B |
| `7a21670fd8d5` | low | Qwen3-Coder 30B-A3B IQ1 | 23.20 | 8.8218 | bucket lower 23.0275 | Likely honest: clean, stable 3-sample run |
| `c45fbae4b21c` | low | Qwen3-Coder 30B-A3B IQ1 | 23.24 | 8.8370 | bucket lower 23.0275 | Likely honest: clean, stable 3-sample run |
| `78e4ab63c264` | low | Qwen3-Coder 30B-A3B TQ1 | 23.62 | 8.9815 | bucket lower 23.0275 | Likely honest: clean, stable 3-sample run |
| `26c66cef8bd3` | low | Qwen3-Coder 30B-A3B IQ2 | 18.25 | 13.8791 | bucket lower 23.0275 | Likely honest: clean, stable 3-sample run |
| `a9f023c1742a` | low | Qwen3-Coder 30B-A3B Q2 | 20.68 | 15.7271 | bucket lower 23.0275 | Likely honest: clean, stable 3-sample run |
| `7ddbc2b4702d` | low | Qwen3-Coder 30B-A3B IQ2 | 23.68 | 18.0086 | bucket lower 23.0275 | Likely honest: clean, stable 3-sample run |

The four high flags were caused by a decimal separator lost in Ollama's
human-readable parameter label, not by implausible speed. OMM now takes the
exact GGUF `general.parameter_count` for new telemetry and narrowly repairs
the historical `0_5B → 5B` / `1_8B → 8B` shape when the file size confirms
the decimal interpretation. Re-evaluating the snapshot then produces zero
high flags. The six low rows remain visible in the redacted audit but are no
longer deleted.
