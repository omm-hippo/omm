# Installer checks and model wiki verification, 2026-10-23

Related issues: #423 and #425. Starting main: `c0b2b9a`.

## Implemented

- Mandatory preflight and post-install/update checks on OMM-managed runner
  routes. macOS uses official fixed-hash casks, quarantine, codesign and
  Gatekeeper; Windows uses the official WinGet source, active/current Defender,
  Authenticode and a non-remediating custom scan; Linux checks Flathub GPG
  configuration and current official ClamAV signatures/scanning.
- Failed/unavailable checks do not produce an installation success result.
  Reinstall and update paths use the same gates. Removal remains available.
  Postcheck failures are displayed as failures even when installed files remain.
- Local timestamped installer-check records appear in status JSON and the GUI
  connections view. They are historic named-check evidence, not ongoing safety
  certification or an upload channel.
- Wiki expanded from seven curated descriptions to **114 exact checkpoints**
  across **11 publishers**. The original descriptions remain unchanged.
  Added offline-rebuildable reviewed facts with pinned official sources.
  Unknown context/tool/license values remain unknown. There are 39 entries
  without a confirmed context limit; no other version's limit was copied.
- Publisher/task filters, Korean alias search, 20-row pagination and factual
  model details. No recommendation-rank changes or invented quality results.
- Updated Vite 8.3.2 to 8.3.5 after npm audit reported an existing high-severity
  development-tool dependency issue. Targeted patch update; no force upgrade.

## Unit-verified

The full Python suite passed in an isolated HOME/OMM_HOME:

`4236 passed, 37 skipped, 1 warning in 124.82s`, exit **0**.

The warning is the installed FastAPI/Starlette TestClient's existing httpx
deprecation. Platform/optional-tool skips are not counted as successful native
checks. The earlier focused run was `303 passed, 13 skipped`.

Tests include source/checksum/protection failures, bypass attempts, failed
preflight preventing installer execution, a failing postcheck preventing success,
native command contracts, empty Linux scans, actual temporary-file receipts,
model identity/schema/provenance and preserving the original curated content.
Legacy package fixtures explicitly mock native gates; separate gate tests use
the real entry points. Fixture binaries never execute.

Command-document synchronization passed for 51 commands. The separate omm.run
command copy remains stale. npm audit after the update reported **0** known
vulnerabilities. This result covers that dependency graph and audit snapshot,
not every component on the machine. Production web assets built with Vite 8.3.5.

After pinning publisher-document revisions and updating package metadata, the
wiki suite passed again: **8 passed**, exit 0. Every reviewed source URL includes
its exact 40-character Git revision. The final **0.4.2** wheel was built and
installed into a fresh task-specific virtual environment. It imports the safety
module and loads all **114** entries; wheel contents include the bundled web
assets. No personal OMM environment was installed over or replaced.

## Browser verification

Browser plugin/skill was unavailable; bundled headless Playwright was used.
The flow was local wiki load → filter/search → paginate → exact model detail →
unknown-context/tool-format checks → connections view showing failed check.

A real loopback OMM HTTP server served the actual 114-entry bundle and built
assets. Machine information, recommendations and native check status were
fixtures, so the browser run is not an installation/backend-scanner E2E test.
Verified desktop 1280×900 and mobile 390×844 rendering without horizontal
overflow, meaningful page content, source links, empty results and six target
interactions. No page errors and no non-loopback request attempts were observed.

## Native macOS evidence

Read-only inspection of the existing `/Applications/Ollama.app` passed:
Gatekeeper enabled, `codesign --verify --deep --strict`, and Gatekeeper artifact
assessment. All exited **0**. The app was not launched/reinstalled and security
settings were not changed. This does not establish a clean-device installation.

## Not verified / incomplete acceptance paths

- Real WinGet/Defender and Flatpak/ClamAV runs: only command/fixture contracts
  were exercised on this Mac; those operating systems were unavailable.
- A new, clean Mac/Windows click-install through first model response was not
  exercised. This work adds mandatory managed-runner checks; it does not close
  the entire beginner-installer issue #423.
- Checks do not prove absence of all vulnerabilities/malicious behavior.
  Post-install artifact scans do not scan every installer instruction before
  it runs; native security/package-manager protections are still dependencies.
- Universal audits of Python/git/package-manager bootstrapping, arbitrary
  external software, shared dependencies and manual installs are not implemented.
- No claim that every wiki entry has a compatible single-file GGUF, fits a
  particular machine or has passed an actual local inference/agent test.
- No public site sync, deployment, package publication, issue closure or
  personal installation changes.

The public model-hub collection stopped immediately on HTTP 429 after 93
metadata snapshots and 71 configs; it was not retried. Publisher-document
snapshots were independently verified and pinned. No weights or remote Python
code were downloaded/executed for this research.

Intentional requests to OMM production hosts: **0**.
