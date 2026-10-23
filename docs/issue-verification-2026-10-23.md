# GitHub issue implementation and verification inventory

Repository: `omm-hippo/omm`. Reviewed open issues authored by
`fakeminjun7321` and `minigu5`; starting main commit
`c0b2b9a1e13344dea538230fec7e941fb57e6971`. No issues were closed and no
production settings, installed user packages or personal harness settings were
changed by this work. This is a verification inventory, not a release approval.

## New implementation

### #502 — local Ollama harness settings

Implemented: `omm setting agent local/cloud/status` for Claude Code, Codex and
pi; loopback-only capability preflight, local-model guard, exact original-byte
restoration, unrelated-edit preservation, conflicting-routing refusal, private
write-ahead recovery, directory overrides and POSIX permission restoration.
No model or harness is installed or launched by these commands.

Unit/contract verification uses actual temporary files, CLI calls, and a local
HTTP receiver. The installed Claude Code 2.1.306 consumed the generated user
settings, sent requests to a synthetic loopback Messages endpoint and received
the fixture response. Codex CLI 0.150.0 accepted the generated configuration,
sent a request to the synthetic `/v1/responses` endpoint and consumed its
streaming fixture response with exit 0.
Both were then restored, with newly created routing files removed.

**Not verified:** real model file-reading/editing tool workflows; pi is not on
PATH; restored cloud account sessions; Windows/Linux installed harnesses;
cluster employee-token routing (#503); MCP registration (#504). A synthetic
API response is not a live-model result. User-level routing does not override
all project, managed or shell settings.

### #431 — reviewed runtime extension contract

Implemented: a versioned manifest, explicit caller-approved Python factory
registry, reserved built-in adapters, capability and result-type validation,
classified failures, removal, documentation and executable Ollama-based example.
Tests run a fixture extension through the existing load/generate/unload
compatibility lifecycle and preserve built-ins after removal. The shipped
example registers, constructs and removes without network requests.

**Not verified:** a new external runtime against its live service. The contract
is for runtime compatibility adapters; download/linker/benchmark CLI extension,
model-source and parser contracts remain separate future work. It does not
implement MLX (#29) or sandbox imported Python. There is no package installer,
automatic discovery or shell-command plugin API.

## Existing implementation checked, not newly created

| Issues | Existing behavior exercised | Remaining user-path boundary |
| --- | --- | --- |
| #421 | Local web API authentication/origin rules, persisted management operations, cancellation/error paths, owned-load probe contracts | Real browser installation of a genuine model and runner recognition across restart; actual deletion in each supported OS |
| #423 | Click launcher generation, optional auto-start enable/disable/status contracts | Clean Finder/Windows click install and first response on physical devices |
| #424 | Recommendation evidence, old/missing metadata, configuration counts, benchmark coverage reports | Environment coverage and calibrated intervals depend on real measurements; no invented accuracy |
| #425 | Exact checkpoint/reviewed conversion mapping, missing models, stale bundled information, provenance | Broader description coverage and deployed website/user-path checks |
| #427 | Normal/rare/slow measurements, repeated plausible poisoning fixtures, before/after retention and predictions | Fabricated distinct configurations, distributed submissions and unauthenticated identities remain unproven |
| #428 | Real local usage/crash receiver, opt-out/consent behavior, minimized payloads and diagnostic counts | Entire live production collection path not exercised; no production policy changed |
| #406 | Installed/remote multi-model `info` behavior | Additional live provider/runner combinations |
| #390 | Existing multi-candidate deferred-memory regression and bounded no-progress exit | Real Windows 600-second benchmark timing remains unresolved; no timeout or thread policy changed without measurements |

## Dependencies and deferred work

- #503: requires four actual Thunderbolt 5 Macs, RDMA configuration, exo
  installation decisions and LAN token/port tests.
- #504: collection permissions and MCP depend on #503 identity/gateway. Existing
  `documents.py` is left unchanged, as required by the issue's ownership boundary.
- #411: placeholder requires design from established real battle data; no new
  training label or quality-gate relaxation is invented.
- #29: arbitrary MLX runtime management is a separate adapter/catalog effort;
  an extension contract alone does not implement it. ONNX remains excluded.
- #88: Microsoft review and real-device public package upgrade paths are not
  replaced by local package tests.

## #433 — reproducible demonstration and honest evaluation record

Use a disposable `OMM_HOME` and dedicated model/runtime. Keep actual user
models and occupied runtime instances untouched. The operator should show:

1. Start `omm web` (which does not open a browser by default), then open the printed loopback address.
2. Compare displayed hardware with actual system information. Show missing
   measurement support and wiki claim sources instead of a confidence score.
3. Select a real model and runner; confirm installation. Inspect downloaded
   bytes, persisted registry entry and runner-side recognition.
4. Request a first response and confirm actual generated text. Distinguish
   install success from runtime compatibility and release only owned loads.
5. Refresh and restart the manager; confirm the installed entry remains.
6. Preview deletion, confirm it, and check the exact owned file, registry and
   runner connection. Exercise cancellation/failure with a disposable transfer.
7. Separately demonstrate agent connection and restoration in an isolated
   harness home. Verify the effective endpoint and actual file tool result.

Record each run with: OS/hardware, version/commit, installation method,
model/runner/version, elapsed time, failed stage, help requested, recovery and
observable destination result. For a human evaluation add participant consent
and their actual feedback; leave fields empty until observed. No participants
were recruited, user feedback invented, speed/savings metrics claimed or
presentation recordings made by this change.

## Verification notes

Focused existing/new issue suite: 302 passed before the final command-document
and file-permission additions. Tests enforce an external-network guard and
allow local fixture receivers. Initial missing optional PDF dependency was
supplied in a task-specific temporary dependency directory; the focused rerun
had no skips. The user's existing development environment was not modified.

The full Python suite then passed: **4256 passed, 37 skipped, 1 warning in
123.59 seconds**, exit 0. The skipped platform/optional-tool paths are not
treated as real-device verification. The warning is an existing FastAPI/
Starlette TestClient deprecation for the installed httpx version. An initial
full run exposed missing optional watchdog dependencies and two omissions in
this change (UTF-8 declarations and purge ownership); those were corrected
before this passing run. Test-only watchdog/plyer dependencies were installed
only into the task's temporary dependency directory.

The uninstallers now refuse purge while a connection journal exists, before
removing the CLI, and include the restored empty connection directory in the
owned-data allowlist. A real POSIX subprocess test checks refusal and unchanged
backup/source files. The corresponding Windows branch remains platform-skipped
on this Mac.

The final targeted rerun, including the last routing-type guard, passed:
**74 passed in 6.76 seconds**, exit 0. Both real installed harnesses consumed
synthetic responses and restored their temporary routing files. A wheel was
built, installed with resolved runtime dependencies into a fresh temporary
virtual environment, and its modules and nested agent help commands ran
successfully. This packaging smoke does not validate Windows standalone
bundles, public package upgrades, live models or production deployment.

README/CLI/command JSON synchronization passes. The generator now includes
third-level commands and their arguments/options. The separate `omm.run` command
copy is stale and needs a reviewed site sync; no site deployment was performed.

Intentional OMM production HTTP/browser requests: **0**. No live telemetry
uploads or production training publications were performed.

## PR #507 CI repair

CI used umask 022 and exposed that the lock helper created the backup directory
before the private-directory setup. Connection and restoration now tighten that
directory to 0700 before locking, including a preexisting 0755 directory. Backup
files remain 0600. POSIX permissions are not a Windows ACL verification.

With umask explicitly set to 022 in an isolated HOME/OMM_HOME, the focused agent
suite passed 41 tests; the full Python suite passed 4258 tests with 37 skipped
and one existing Starlette warning (exit 0). Current main was merged locally.
