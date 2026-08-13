# Catalog Signing Infrastructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn on Ed25519 signature verification for the recommendation catalog by default, by wiring the already-existing `scripts/sign_catalog.py` into the nightly training workflow and flipping the client's `catalog_manifest_url`/`catalog_public_key` config defaults from `None` to real values.

**Architecture:** No new services. The signed manifest is published to the same repo, same `raw.githubusercontent.com` path pattern already used for `recommend-model.json`. `scripts/sign_catalog.py` (existing, previously unused for this purpose) signs the artifact in CI; `src/omm/catalog.py`'s `verify_signed_artifact` (existing, previously dead code because defaults were `None`) verifies it on the client. One real gap gets fixed along the way: `search.local_candidate_pool` doesn't forward manifest/key to `predictor.load_model`, so `omm search`'s candidate pool would silently skip verification even with defaults on.

**Tech Stack:** Python 3.11, `cryptography>=43` (already a core dependency, no new install needed), pytest, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-13-catalog-signing-infra-design.md`

## Global Constraints

- Branch: work happens on `beta` (current checkout). Do not touch `main` directly.
- No new dependencies — `cryptography` is already in `pyproject.toml`.
- No new hosting/service — manifest is served from `https://raw.githubusercontent.com/omm-hippo/omm/main/published/recommend-model.manifest.json`, same pattern as the existing `model_url`.
- Manifest schema `catalog.verify_signed_artifact` requires exactly: `schema_version == 1`, `artifact_sha256` (hex sha256 of the raw artifact bytes), `signature` (base64 Ed25519 signature over those same raw bytes). `scripts/sign_catalog.py sign` already emits this shape plus two extra fields (`artifact`, `signed_at`) that the verifier ignores — do not change the verifier to require them.
- The Ed25519 private key must never be committed to the repo or left on disk after Task 1. It is handed to the user once (chat output) for them to register as the `LOCALFIT_CATALOG_SIGNING_KEY` GitHub Actions secret themselves — this plan does not run `gh secret set` or trigger `train.yml`.
- Every task that touches `src/omm/` or `scripts/` ends with the relevant test file(s) green before moving on.

---

## Task 1: Generate the Ed25519 signing keypair

**Files:** none (operational task; no repo files change)

**Interfaces:**
- Consumes: `scripts/sign_catalog.py`'s existing `generate` subcommand (`python scripts/sign_catalog.py generate --private P --public K`) and `catalog.load_public_key` for a smoke check.
- Produces: a public key string (base64) that Task 3 bakes into `config.py`, and a private key string (base64) shown to the user for `gh secret set`.

- [ ] **Step 1: Generate the keypair in a scratch directory**

```bash
keydir=$(mktemp -d)
python scripts/sign_catalog.py generate --private "$keydir/catalog-signing.key" --public "$keydir/catalog-signing.pub"
cat "$keydir/catalog-signing.pub"
```

- [ ] **Step 2: Smoke-verify the keypair round-trips through the existing verifier**

```bash
python - "$keydir/catalog-signing.key" "$keydir/catalog-signing.pub" <<'EOF'
import sys
sys.path.insert(0, "src")
from pathlib import Path
from scripts import sign_catalog
from omm import catalog

private_path, public_path = Path(sys.argv[1]), Path(sys.argv[2])
artifact = Path(sys.argv[1]).parent / "fixture.json"
artifact.write_text('{"schema_version":1}')
manifest_path = artifact.with_suffix(".manifest.json")
sign_catalog.sign(artifact, private_path, manifest_path)

import json
manifest = json.loads(manifest_path.read_text())
public_key = public_path.read_text().strip()
assert catalog.verify_signed_artifact(artifact.read_bytes(), manifest, public_key) == manifest
print("round-trip OK")
EOF
```

Expected: prints `round-trip OK`. If this fails, stop — do not proceed to Task 3 with a broken key.

- [ ] **Step 3: Hand off the keys and destroy the local copy**

Print `$keydir/catalog-signing.key`'s contents once in your response so the user can run
`gh secret set LOCALFIT_CATALOG_SIGNING_KEY --repo omm-hippo/omm` themselves (paste the value at
the prompt). Record the public key string from Step 1 — Task 3 needs it verbatim. Then:

```bash
rm -rf "$keydir"
```

No commit — nothing in the repo changed.

---

## Task 2: Lock in the sign→verify round trip as a regression test

`scripts/sign_catalog.py` already works (it predates this feature), but nothing in the test
suite proves its output satisfies `catalog.verify_signed_artifact` — that handshake is the
entire point of this feature, so it needs a permanent test, not just the one-off smoke check
from Task 1.

**Files:**
- Create: `tests/test_sign_catalog.py`

**Interfaces:**
- Consumes: `scripts.sign_catalog.generate_keys(private_path: Path, public_path: Path) -> None`, `scripts.sign_catalog.sign(artifact: Path, private_path: Path, manifest_path: Path) -> None`, `omm.catalog.verify_signed_artifact(content: bytes, manifest: dict, encoded_public_key: str) -> dict`.

- [ ] **Step 1: Write the test**

```python
import json

from scripts import sign_catalog
from omm import catalog


def test_sign_catalog_output_is_accepted_by_the_catalog_verifier(tmp_path):
    private_path = tmp_path / "signing.key"
    public_path = tmp_path / "signing.pub"
    sign_catalog.generate_keys(private_path, public_path)

    artifact = tmp_path / "recommend-model.json"
    artifact.write_text('{"candidates": []}')
    manifest_path = tmp_path / "recommend-model.manifest.json"

    sign_catalog.sign(artifact, private_path, manifest_path)

    manifest = json.loads(manifest_path.read_text())
    public_key = public_path.read_text().strip()

    verified = catalog.verify_signed_artifact(artifact.read_bytes(), manifest, public_key)

    assert verified["artifact_sha256"] == manifest["artifact_sha256"]


def test_sign_catalog_rejects_verification_with_the_wrong_key(tmp_path):
    sign_catalog.generate_keys(tmp_path / "a.key", tmp_path / "a.pub")
    sign_catalog.generate_keys(tmp_path / "b.key", tmp_path / "b.pub")

    artifact = tmp_path / "recommend-model.json"
    artifact.write_text('{"candidates": []}')
    manifest_path = tmp_path / "recommend-model.manifest.json"
    sign_catalog.sign(artifact, tmp_path / "a.key", manifest_path)

    manifest = json.loads(manifest_path.read_text())
    wrong_public_key = (tmp_path / "b.pub").read_text().strip()

    try:
        catalog.verify_signed_artifact(artifact.read_bytes(), manifest, wrong_public_key)
        raised = False
    except catalog.CatalogVerificationError:
        raised = True
    assert raised
```

- [ ] **Step 2: Run it**

Run: `pytest tests/test_sign_catalog.py -v`
Expected: both tests PASS (this locks in already-correct behavior — there is no red step here
because `sign_catalog.py` and `catalog.py` both predate this task; the point is to make sure
they never silently drift apart).

- [ ] **Step 3: Commit**

```bash
git add tests/test_sign_catalog.py
git commit -m "test: lock sign_catalog.py output to the catalog verifier's expected shape"
```

---

## Task 3: Bake the real manifest URL + public key into client defaults

**Files:**
- Modify: `src/omm/config.py:50-51`
- Test: `tests/test_cli_catalog.py`

**Interfaces:**
- Consumes: the public key string generated in Task 1, Step 1.
- Produces: `config.DEFAULT_CONFIG["catalog_manifest_url"]` and `config.DEFAULT_CONFIG["catalog_public_key"]` as non-`None` strings — Task 5's TUI label (`cli.py:2606`) and `_load_recommendation_with_change_note`/`_refresh_data` (`cli.py:175-182`, `431-460`) already branch on these being truthy, so no code there changes.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli_catalog.py` (`catalog` and `config` are both already imported at the top
of this file — `from omm import catalog, cli, config` — no new imports needed):

```python
def test_catalog_signing_is_on_by_default():
    assert config.DEFAULT_CONFIG["catalog_manifest_url"] == (
        "https://raw.githubusercontent.com/omm-hippo/omm/main/published/recommend-model.manifest.json"
    )
    public_key = config.DEFAULT_CONFIG["catalog_public_key"]
    assert public_key is not None
    # Must be a valid Ed25519 public key, not just a non-empty string.
    catalog.public_key_fingerprint(public_key)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli_catalog.py::test_catalog_signing_is_on_by_default -v`
Expected: FAIL — `catalog_manifest_url` is `None`, or `public_key_fingerprint(None)` raises.

- [ ] **Step 3: Flip the defaults**

In `src/omm/config.py`, replace:

```python
    "catalog_manifest_url": None,
    "catalog_public_key": None,
```

with:

```python
    "catalog_manifest_url": "https://raw.githubusercontent.com/omm-hippo/omm/main/published/recommend-model.manifest.json",
    "catalog_public_key": "<PASTE THE TASK 1 PUBLIC KEY HERE>",
```

Use the exact public key string captured in Task 1, Step 1 — not a placeholder.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cli_catalog.py -v`
Expected: all PASS, including the pre-existing `test_catalog_trust_saves_verified_public_key` (unaffected — it explicitly overrides both values via the CLI command).

- [ ] **Step 5: Commit**

```bash
git add src/omm/config.py tests/test_cli_catalog.py
git commit -m "feat: verify the official recommendation catalog's signature by default"
```

---

## Task 4: Sign the published catalog in the nightly training workflow

**Files:**
- Modify: `.github/workflows/train.yml`

**Interfaces:**
- Consumes: `scripts/sign_catalog.py sign <artifact> --private P --manifest M` (Task 1/2 already proved this produces a verifiable manifest); the `LOCALFIT_CATALOG_SIGNING_KEY` secret (registered by the user after Task 1, not by this task).
- Produces: `published/recommend-model.manifest.json`, committed alongside `published/recommend-model.json` in the same nightly-retrain commit.

- [ ] **Step 1: Add the signing step**

In `.github/workflows/train.yml`, insert a new step immediately after the existing
`Train and quality-gate candidate model` step (which ends with
`cp "$RUNNER_TEMP/localfit-candidate.json" published/recommend-model.json`) and before the
`Open a PR with the retrained model and auto-merge it` step:

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

- [ ] **Step 2: Add the manifest to the commit**

In the same file, in the `Open a PR with the retrained model and auto-merge it` step, change:

```bash
          git add published/recommend-model.json published/candidates.json
```

to:

```bash
          git add published/recommend-model.json published/candidates.json published/recommend-model.manifest.json
```

- [ ] **Step 3: Validate the YAML**

Run: `python -c "import yaml; yaml.safe_load(open('.github/workflows/train.yml'))"`
Expected: no output, exit code 0 (confirms the edit didn't break YAML indentation — this
workflow only actually runs on the nightly cron or `workflow_dispatch`, so this is the
available pre-merge check).

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/train.yml
git commit -m "feat: sign the published recommendation catalog in the nightly train workflow"
```

---

## Task 5: Fix `omm search`'s candidate pool skipping signature verification

**Files:**
- Modify: `src/omm/search.py:184-186`
- Modify: `src/omm/cli.py:2701`, `src/omm/cli.py:2790`
- Test: `tests/test_search.py`, `tests/test_cli_search.py`, `tests/test_cli_hardware_fit.py`, `tests/test_cli_install_suggestions.py`

**Interfaces:**
- Consumes: `predictor.load_model(url: str | None, manifest_url: str | None = None, public_key: str | None = None) -> dict | None` (existing signature, `src/omm/predictor.py:184-188` — unchanged).
- Produces: `search.local_candidate_pool(model_url: str | None, manifest_url: str | None = None, public_key: str | None = None) -> list[dict]` — the two new parameters are additive and default to `None`, so any caller that only passes `model_url` still behaves exactly as before.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_search.py`:

```python
def test_local_candidate_pool_forwards_manifest_and_public_key_to_load_model(monkeypatch):
    captured = {}

    def fake_load_model(url, manifest_url=None, public_key=None):
        captured["url"] = url
        captured["manifest_url"] = manifest_url
        captured["public_key"] = public_key
        return {"candidates": []}

    monkeypatch.setattr(search_mod.predictor, "load_model", fake_load_model)

    search_mod.local_candidate_pool(
        "https://example.com/model.json",
        manifest_url="https://example.com/manifest.json",
        public_key="the-key",
    )

    assert captured == {
        "url": "https://example.com/model.json",
        "manifest_url": "https://example.com/manifest.json",
        "public_key": "the-key",
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_search.py::test_local_candidate_pool_forwards_manifest_and_public_key_to_load_model -v`
Expected: FAIL with `TypeError: local_candidate_pool() got an unexpected keyword argument 'manifest_url'`.

- [ ] **Step 3: Update `local_candidate_pool`'s signature**

In `src/omm/search.py`, change:

```python
def local_candidate_pool(model_url: str | None) -> list[dict]:
    pool = _curated_as_candidates()
    artifact = predictor.load_model(model_url)
```

to:

```python
def local_candidate_pool(
    model_url: str | None,
    manifest_url: str | None = None,
    public_key: str | None = None,
) -> list[dict]:
    pool = _curated_as_candidates()
    artifact = predictor.load_model(model_url, manifest_url, public_key)
```

- [ ] **Step 4: Run the new test to verify it passes**

Run: `pytest tests/test_search.py::test_local_candidate_pool_forwards_manifest_and_public_key_to_load_model -v`
Expected: PASS.

- [ ] **Step 5: Run the full test_search.py file — fix the one break**

Run: `pytest tests/test_search.py -v`

`test_local_candidate_pool_merges_curated_and_cached_and_dedupes` will now fail, because its
mock (`lambda url: {...}`) doesn't accept the two extra positional arguments
`local_candidate_pool` now always passes to `predictor.load_model`. In `tests/test_search.py`,
change:

```python
    monkeypatch.setattr(
        search_mod.predictor,
        "load_model",
        lambda url: {
```

to:

```python
    monkeypatch.setattr(
        search_mod.predictor,
        "load_model",
        lambda url, *args, **kwargs: {
```

Run: `pytest tests/test_search.py -v` again. Expected: all PASS.

- [ ] **Step 6: Wire the two `cli.py` call sites to pass config's catalog values**

In `src/omm/cli.py:2701` (inside the `search` command), change:

```python
    pool = search_mod.local_candidate_pool(config.get("model_url"))
```

to:

```python
    pool = search_mod.local_candidate_pool(
        config.get("model_url"),
        manifest_url=config.get("catalog_manifest_url"),
        public_key=config.get("catalog_public_key"),
    )
```

In `src/omm/cli.py:2790` (inside `_print_install_suggestions`), apply the identical change:

```python
    pool = search_mod.local_candidate_pool(
        config.get("model_url"),
        manifest_url=config.get("catalog_manifest_url"),
        public_key=config.get("catalog_public_key"),
    )
```

- [ ] **Step 7: Fix the now-broken call-site mocks**

Both `cli.py` call sites now pass `manifest_url=`/`public_key=` keyword arguments, which breaks
every test that monkeypatches `local_candidate_pool` with a single-parameter lambda. Fix all
occurrences the same way — add `**kwargs` to accept and ignore the new keyword arguments:

In `tests/test_cli_search.py`, at lines 15, 45, 70, 99, 126 — change every
`lambda model_url: [` and `lambda model_url: []` to `lambda model_url, **kwargs: [` /
`lambda model_url, **kwargs: []` respectively (5 occurrences).

In `tests/test_cli_hardware_fit.py`, at lines 14, 42, 70, 97, 128 — same change, all 5
occurrences of `lambda model_url:` become `lambda model_url, **kwargs:`.

In `tests/test_cli_install_suggestions.py`, at lines 13 and 28 — same change, both
occurrences of `lambda model_url:` become `lambda model_url, **kwargs:`.

- [ ] **Step 8: Run the full affected test files**

Run: `pytest tests/test_search.py tests/test_cli_search.py tests/test_cli_hardware_fit.py tests/test_cli_install_suggestions.py tests/test_cli_catalog.py -v`
Expected: all PASS.

- [ ] **Step 9: Commit**

```bash
git add src/omm/search.py src/omm/cli.py tests/test_search.py tests/test_cli_search.py tests/test_cli_hardware_fit.py tests/test_cli_install_suggestions.py
git commit -m "fix: verify catalog signature for omm search's candidate pool too"
```

---

## Task 6: Remove "Catalog status" from the interactive `omm setting` menu

Keeps the `omm setting catalog-status` CLI command (unchanged, still tested by the existing
`test_setting_catalog_status_shows_configured_state` in `tests/test_cli_setting.py`) but drops
it from the TUI list — its manifest-URL summary already appears on the "Catalog trust" line
(`cli.py:2606`, `2621`), and the full table view is more useful as a scriptable/CI command than
as one more interactive menu hop.

**Files:**
- Modify: `src/omm/cli.py:2620-2624` (the `questionary.Choice` list) and `:2678-2679` (the
  `elif` branch)
- Test: `tests/test_cli_setting.py`

**Interfaces:**
- Consumes: none new.
- Produces: none new — pure removal.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli_setting.py` (follows the existing `fake_select` capture pattern already
used by `test_setting_bare_menu_upload_submenu_has_back_option` in the same file):

```python
def test_setting_bare_menu_no_longer_offers_catalog_status(isolated_omm_home, monkeypatch):
    captured_choices: list = []

    def fake_select(message, choices=None, **kwargs):
        captured_choices.append(choices)
        return None

    monkeypatch.setattr(questionary, "select", fake_select)
    monkeypatch.setattr(cli, "_ask_select", lambda question: None)

    result = runner.invoke(cli.app, ["setting"])

    assert result.exit_code == 0, result.stdout
    labels = [choice.title for choice in captured_choices[0]]
    assert not any("Catalog status" in label for label in labels)
    assert any("Catalog trust" in label for label in labels)
    assert any("Catalog rollback" in label for label in labels)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli_setting.py::test_setting_bare_menu_no_longer_offers_catalog_status -v`
Expected: FAIL — `"Catalog status"` is still present in `labels`.

- [ ] **Step 3: Remove the menu entry**

In `src/omm/cli.py`, inside `setting_menu`'s `choices=[...]` list, delete this line:

```python
                    questionary.Choice("Catalog status", value="catalog-status"),
```

(Leave the `"Catalog trust (current: {catalog_manifest})"` and `"Catalog rollback"` choices
directly above and below it untouched.)

Then delete the corresponding branch further down in the same function:

```python
        elif choice == "catalog-status":
            catalog_status()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cli_setting.py -v`
Expected: all PASS. Note: `test_setting_bare_menu_can_change_catalog_trust` and
`test_setting_bare_menu_declining_another_change_exits_after_one_action` both pick
`"catalog-status"` as their mocked `_ask_select` return value, but since `_ask_select` is
monkeypatched to return that literal string directly (bypassing the real `choices` list
entirely), removing the choice doesn't affect them — the picked value simply matches no `elif`
branch and the loop falls through to the "change another setting?" prompt, same as any other
unrecognized value. Both keep passing unmodified.

- [ ] **Step 5: Commit**

```bash
git add src/omm/cli.py tests/test_cli_setting.py
git commit -m "refactor: drop catalog-status from the interactive setting menu, keep the CLI command"
```

---

## Task 7: Full suite sanity pass

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `pytest -q`
Expected: all tests pass (no regressions outside the files touched above).

- [ ] **Step 2: Report the manual follow-ups that remain outside this repo**

These are **not** part of this plan (see spec section 6 — they touch account-level GitHub
secrets and trigger a real CI run, both excluded from automation here):

1. User runs `gh secret set LOCALFIT_CATALOG_SIGNING_KEY --repo omm-hippo/omm` with the
   private key printed in Task 1.
2. User triggers `train.yml` once (`gh workflow run train.yml --repo omm-hippo/omm` or waits
   for the nightly cron) and confirms `published/recommend-model.manifest.json` lands on
   `main` with a signature that verifies.
3. Until step 2 completes, every `omm update`/`omm search` on this code will log a caught
   verification/fetch error and fall back to the cached catalog — expected, not a bug.
