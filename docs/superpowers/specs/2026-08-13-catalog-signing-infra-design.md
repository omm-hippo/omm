# Catalog signing infrastructure — design

Date: 2026-08-13

## Problem

`omm` fetches the recommendation catalog (`recommend-model.json`) from
`raw.githubusercontent.com/omm-hippo/omm/main/published/recommend-model.json`.
The file already only reaches `main` through a PR-gated, SSH-signed-commit,
branch-protected pipeline (`train.yml` + `trusted-head.yml`). That protects
the *git history*, but the client trusts whatever bytes the CDN/GitHub
serves at fetch time with no independent verification of the payload
itself.

`omm setting catalog-trust/-status/-rollback` already implement an
Ed25519 signature-verification layer for exactly this
(`src/omm/catalog.py`, `src/omm/predictor.py:144-172`), but it is
opt-in and unused: `catalog_manifest_url`/`catalog_public_key` default
to `None` (`config.py:50-51`), and no manifest is ever published.

## Decisions (from chat brainstorming)

1. **Secure by default.** Ship a real public key + manifest URL as the
   default config, so a fresh install verifies the official catalog
   without the user running `catalog-trust` themselves. `catalog-trust`
   remains available to point at an alternate/self-hosted catalog.
2. **Key custody (Option A).** Generate the Ed25519 keypair locally,
   hand the private key to the user once to store as a GitHub Actions
   repo secret (`gh secret set`) themselves. The private key is never
   committed or stored in this working tree past generation.
3. **`catalog-status` stays CLI-only.** Remove it from the interactive
   `omm setting` TUI menu (its info is redundant with the "current: …"
   label already on the Catalog trust menu entry); keep the
   `omm setting catalog-status` command for scripting/CI.

## Components

### 1. Signing utility — reuse, not rebuild

`scripts/sign_catalog.py` already exists (added in #2, for an unrelated
self-hosted benchmark pipeline) and does exactly what's needed:

- `sign_catalog.py generate --private P --public K` — new Ed25519 keypair,
  base64-encoded, private file written `chmod 600`.
- `sign_catalog.py sign <artifact> --private P --manifest M` — writes
  `{"schema_version": 1, "artifact": ..., "artifact_sha256": ...,
  "signature": ..., "signed_at": ...}`.

This manifest shape is a superset of what `catalog.verify_signed_artifact`
checks (`schema_version == 1`, `artifact_sha256`, `signature`); the extra
`artifact`/`signed_at` fields are ignored by the verifier. No new script.

### 2. CI: `.github/workflows/train.yml`

Add a signing step after `cp "$RUNNER_TEMP/localfit-candidate.json"
published/recommend-model.json` and before the PR-commit step, mirroring
the existing SSH-key-to-tempfile handling already in that job:

```yaml
- name: Sign published catalog
  env:
    LOCALFIT_CATALOG_SIGNING_KEY: ${{ secrets.LOCALFIT_CATALOG_SIGNING_KEY }}
  run: |
    umask 077
    key_path="$RUNNER_TEMP/omm-catalog-signing-key"
    printf '%s\n' "$LOCALFIT_CATALOG_SIGNING_KEY" > "$key_path"
    python scripts/sign_catalog.py sign published/recommend-model.json \
      --private "$key_path" \
      --manifest published/recommend-model.manifest.json
```

`published/recommend-model.manifest.json` gets `git add`-ed alongside
`recommend-model.json` in the existing commit step, so it lands via the
same signed-commit + branch-protected PR flow, and is servable at
`https://raw.githubusercontent.com/omm-hippo/omm/main/published/recommend-model.manifest.json`
with no new hosting.

### 3. Client defaults — `src/omm/config.py`

```python
"catalog_manifest_url": "https://raw.githubusercontent.com/omm-hippo/omm/main/published/recommend-model.manifest.json",
"catalog_public_key": "<the generated public key, base64>",
```

`_load_recommendation_with_change_note` (`cli.py:175-182`) and
`_refresh_data` (`cli.py:431-460`) already branch on
`catalog_manifest_url`/`catalog_public_key` being present, so no wiring
change needed there — flipping the defaults from `None` to real values is
enough to turn verification on for those two paths.

### 4. Gap found: `search.local_candidate_pool`

`src/omm/search.py:184-186` calls `predictor.load_model(model_url)` with
no manifest/key arguments, and its two call sites
(`cli.py:2701`, `cli.py:2790`, both `search_mod.local_candidate_pool(config.get("model_url"))`)
don't pass them either. With defaults flipped on, `omm search`'s
candidate pool would silently skip verification while every other path
enforces it. Fix: add `manifest_url`/`public_key` parameters to
`local_candidate_pool`, thread them through from `config.get(...)` at
both call sites.

### 5. TUI: `cli.py` `setting_menu`

Remove the `questionary.Choice("Catalog status", value="catalog-status")`
entry (~line 2623) and its `elif choice == "catalog-status": catalog_status()`
branch (~line 2679) from the interactive loop. Leave the
`@setting_app.command(name="catalog-status")` Typer command untouched.

Note: `setting_menu` currently has an unrelated in-flight edit from a
concurrent session (a "change another setting?" loop-continue prompt).
This work is layered on top of the file as it exists at edit time, not
reverted.

### 6. Rollout order

Fail-soft (`_refresh_data` catches and prints, doesn't crash;
`predictor.load_model` falls back to cache), but sequencing still
matters to avoid every user seeing a red verification-failed line on
`omm update` day one. This implementation delivers *all* the code in
one pass (keypair, workflow step, client defaults, wiring fix), but
"code merged" and "verification actually live" are different moments:

1. This PR lands the workflow step + client defaults together, with
   the real generated public key baked in.
2. Two manual follow-ups outside this PR, done by the user (not
   automated here, since they touch account-level GitHub secrets and
   triggering CI runs is a shared-state action): register
   `LOCALFIT_CATALOG_SIGNING_KEY` via `gh secret set`, then trigger
   `train.yml` once (`gh workflow run train.yml` or wait for the
   nightly cron) to confirm a real signed manifest lands on `main`.
3. Until that first signing run completes, clients with the new
   defaults will log a fetch/verification failure and silently fall
   back to their cached catalog on every `omm update` — annoying but
   not broken. This is the accepted cost of shipping the code and the
   secret-registration step in parallel rather than gating the PR on
   a manual action.

### 7. Key rotation

No automation. If the key is ever rotated: new keypair → replace the
GitHub secret → next nightly signs with the new key → next omm release
carries the new default public key. Out of scope to build tooling for
this now given project scale.

## Out of scope

- Automated key rotation/expiry.
- Signing anything other than `recommend-model.json` (e.g. `rules.json`
  is not covered by this design).
- Any new hosting/service for the manifest — reuses `raw.githubusercontent.com`.
