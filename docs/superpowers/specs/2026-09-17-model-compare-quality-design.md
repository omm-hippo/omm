# Model comparison and measured quality design

## Goal

Add a read-only `omm compare` command for two to five exact catalog packages,
then attach versioned, signed quality evidence without replacing provider-declared
`BEST FOR` labels or treating missing measurements as low quality.

## Product contract

- `BEST FOR` remains provider/catalog intent.
- `MEASURED FOR` comes only from a validated quality artifact.
- Compare never downloads, installs, runs, or removes a model.
- Hardware fit, predicted speed, declared purpose, and measured quality remain
  separate facts. The output may name `BEST FIT`, `FASTEST`, and
  `BEST MEASURED QUALITY` separately.
- An unresolved, ambiguous, or duplicate input fails before any mutation.
- Models without matching evidence show `Not measured` and are not scored as zero.

## Quality evidence

The existing arithmetic pack stays supported. A v2 pack contract adds explicit
task/evaluator/provenance fields. The first executable pack is a small Python
coding smoke pack with generation and repair tasks. Generated code is untrusted:
it runs only through a Docker/Podman sandbox with no network, a read-only bind
mount, dropped capabilities, no-new-privileges, bounded CPU/RAM/PIDs/output, and
a hard timeout. The generated source is not stored in evidence or telemetry.

Official quality is distributed as a bounded JSON object signed with the same
Ed25519 trust anchor as the recommendation catalog. Exact provider/repository/
filename/digest/quantization and pack version identify an observation. Community
measurements are not automatically promoted to official scores.

## Initial scope

- `omm compare MODEL MODEL [MODEL ...] --profile ... --for ... --json`
- Hardware-only comparison works without a quality artifact.
- Quality artifact parser, signature verification, caching, and fixture-backed
  measured comparison.
- `omm evaluate MODEL --pack PATH --output PATH` for an installed Ollama model
  and a Docker/Podman Python coding pack.
- No new hosted service, translation/writing packs, automatic install, or
  quality-based changes to the default `omm recommend` ranking in this batch.

## Verification boundaries

Unit tests cover resolution, ranking, schema bounds, signatures, generated-code
parsing, sandbox argv, malicious fixtures, JSON, and missing evidence. Container
and physical-device runs are reported separately. A test double is not a live
model or destination-side quality publication.
