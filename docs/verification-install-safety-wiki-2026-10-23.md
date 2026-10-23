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

## Integration with current main before PR

Merged `origin/main` at `2306137` (the #506 workspace/chat UI) locally, preserving
the sidebar, Markdown rendering, appearance controls and runtime license bundle.
Resolved package metadata/generated-asset conflicts and rebuilt using Vite 8.3.5.
The integrated branch version is 0.4.3.

Post-merge validation: **347 passed, 14 skipped** in the affected Python suite;
**4 passed** frontend tests; web build, dependency audit (0 reported issues),
command-doc synchronization and diff checks passed. Repeated the six browser
flows against the merged interface, including mobile sidebar navigation: no
page errors, non-loopback request attempts or horizontal overflow. The earlier
full-suite and 0.4.2 wheel results above remain scoped to their recorded tree;
they are not presented as a second full-suite run of the merge.

## CI installation environment repair

The hosted macOS runner inherited HOMEBREW_CASK_OPTS=--no-quarantine, which
correctly failed the new mandatory preflight. The real-install workflows now
remove that runner default instead of bypassing the gate. The Linux Jan workflow
installs ClamAV and updates official signatures before exercising installation.
Focused workflow/engine verification tests: 10 passed, exit 0. Remote real-install
results remain separate from this local contract-test result.

The official Msty Studio 2.9.12 and AnythingLLM 1.18.0 casks currently use
sha256=no_check with mutable latest URLs. Managed installation is intentionally
blocked. CI exercises the real mandatory refusal for that exact metadata and
prints installation/link/import NOT VERIFIED. If upstream supplies a pinned
hash, CI instead exercises the full real install/link/import path. Other errors
remain failures. This is safety refusal evidence, not native installation proof.

The Linux scanner now receives the resolved, scope-checked deployment directory
when Flatpak reports an active symlink; it still refuses links inside the app.
The Linux-path fixture uses Path.home explicitly on Windows too. Focused safety,
workflow and verifier checks after these changes: 46 passed, exit 0.

## Further native CI findings

The real Jan scan reached its GPU libraries and failed with
Heuristics.Limits.Exceeded.MaxFileSize (0 infected files, 2 errors). The scanner
now permits up to 1024 MiB per file and 2048 MiB per input with the existing
300-second command limit. Exceeded limits still fail closed. Native CI must
confirm these explicit limits cover the actual package.

A Windows restart test also exposed shutdown releasing the job-directory lock
before its worker reader finished a final JSON replacement. Shutdown now joins
those readers before releasing ownership. A deterministic regression test keeps
exclusive ownership through the final save and reloads the persisted result.
Focused safety/verifier/workflow and real local HTTP/worker tests: 81 passed.
