# Self-hosted benchmark data

Benchmark results are never uploaded without explicit per-run consent or an
`always` policy. New installations use the hosted proof-of-work gateway as the
default destination, while existing local-only configurations stay local. To
run the bundled FastAPI + SQLite collector instead:

```sh
pip install -e ".[server]"
export LOCALFIT_DB_PATH="$PWD/localfit.db"
export LOCALFIT_ADMIN_TOKEN="replace-with-a-long-random-token"
localfit-server
```

Explicitly configure the endpoint and opt in before uploading:

```sh
omm setting telemetry --endpoint http://127.0.0.1:8000/v1/benchmarks
omm setting upload benchmark --enable
```

Loopback ingestion needs no token. Once `LOCALFIT_INGEST_TOKEN` is set, every
request needs it — including loopback ones, so export the same value for the
omm client on that machine. A same-host reverse proxy makes remote requests
look like loopback, so peer address alone is not treated as authentication.
If the collector listens on a non-loopback interface, set the same
`LOCALFIT_INGEST_TOKEN` on both the server and the omm client; remote
ingestion fails closed when it is missing. The client only attaches
`LOCALFIT_INGEST_TOKEN` to an `https` endpoint, so a loopback
(`http://127.0.0.1:…`) collector never receives it.

Training can consume the authenticated export directly:

```sh
export LOCALFIT_ADMIN_TOKEN="replace-with-a-long-random-token"
python scripts/train_model.py \
  --telemetry-url http://127.0.0.1:8000/v1/benchmarks/export
```

Firebase Realtime Database JSON endpoints remain supported. An official
`*.firebaseio.com` or
`*.firebasedatabase.app` `.json` URL can be read without an admin token;
self-hosted raw export requires `LOCALFIT_ADMIN_TOKEN`. Exact duplicate events
are ignored.

Automated retraining is fail-closed. Configure
`LOCALFIT_TELEMETRY_EXPORT_URL`; configure `LOCALFIT_ADMIN_TOKEN` as well for a
self-hosted export (it is optional for an official Firebase JSON URL). The
scheduled job otherwise stops without changing the published artifact. It
requires at least 100 distinct valid v6/v7 configurations with explicit
runtime and CPU metadata (legacy rows do not satisfy this minimum), rejects datasets with more
than 25% invalid rows, and reserves a deterministic 20% holdout. A 64-tree v4
candidate replaces the incumbent only when both holdout RMSLE and P90 absolute
percentage error stay within the configured regression limits. Selection is
evaluated on whole hardware/request contexts, so sibling model variants never
leak across training and holdout sets. Publishing requires at least three multi-model selection groups and complete
selection metrics. Fit-regression checks are only applied once the minimum
known-unfit sample count is met. A passed publication gate therefore does not
mean every check was evaluated: per-check `evaluation_details` and
`omm setting catalog-status` distinguish passed, failed, and insufficient data.
See [evaluation evidence](evaluation-evidence.md). The artifact records
the candidate/baseline evaluation report.

The same gate can validate an exported local dataset without contacting the
collector:

```sh
python scripts/train_model.py --offline \
  --telemetry-file benchmarks.jsonl \
  --quality-gate --minimum-real-configurations 100 \
  --baseline published/localfit-recommend-model.json \
  --output candidate.json --quality-report quality-report.json
```

Synthetic bootstrap training remains available for local development, but the
scheduled publishing workflow never uses it as a substitute for missing real
benchmark data.
