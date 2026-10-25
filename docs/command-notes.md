# Command notes

Details behind the command list in the [README](../README.md#usage): what each
command checks, what it stores, and where its limits are.

## omm recommend

`omm recommend` keeps compatible models visible when they are already present,
marks whether OMM or another supported local runner owns them, and never
reinstalls a selected installed model. In JSON output, `installed`,
`managed_by_omm`, `installed_engines`, and `installation_match` expose the same
state. `installation_match` distinguishes an exact artifact from the same
model-and-parameter-size identity in a manifest-based runner, whose local
quantization may differ. With `--yes`, the highest-ranked model that is not
installed is selected; if every displayed recommendation is already present,
the command exits without downloading.

## omm web

`omm web` prints a loopback-only URL for the local model manager. Open it to
search the existing model wiki by purpose and description, select an exact GGUF
package, review hardware, choose a memory budget, and
inspect or remove OMM-managed models and runner links. Each file operation
requires confirmation in the page. Installation verifies the downloaded file
and registry; it does not automatically load the model, run a benchmark, or
upload measurements. Recommendation evidence distinguishes bundled rules,
signed catalog data, and local speed correction; missing environment-specific
support is shown without a confidence percentage.

The page initially uses the cached catalog or bundled rules. Its catalog-update
button explicitly downloads and verifies the configured catalog. Job history
persists across restarts; an unconfirmed result is marked interrupted. Stop
the server with Ctrl+C. To build the interface from source, run `npm ci` and
`npm run build` in `web-ui` before starting the server; `npm run dev` rebuilds
assets on changes while the same-origin Python server serves them.

After linking a model, the page can separately confirm a short real response
through Ollama or LM Studio's local API, preserve a preloaded model's context,
and release only its own test load. The response and compatibility outcome are
shown separately from installation. See [local manager guidance](local-web.md)
for click launchers, description sources, memory checks and verification limits.

The bundled [model wiki](model-wiki-expansion.md) covers 114 exact
checkpoints with publisher filtering and explicit unknown values. Descriptions
do not imply a compatible GGUF or a verified runtime/quality result.

Secondary screens show runner/API and explicit external connectivity checks,
read-only diagnostics, storage and external-file previews, and sharing policies.
The comparison screen measures selected local models with an eight-question
arithmetic smoke check and three speed samples; evidence is saved locally and
can be downloaded as JSON. It is not a general quality ranking. Per-model runtime
settings support real baseline/proposed trials, saving and restoring verified
profiles; busy runtimes are left unchanged.

Use **내 모델 → 대화** or the **채팅** screen to converse with a linked local
Ollama or LM Studio model. Replies appear incrementally, completed turns are
sent as conversation context, and **응답 중단** interrupts the current reply.
The GUI stores text history locally, supports reopening and deleting it, and
releases only its own model load when the connection ends. Chat and model
management jobs are serialized; no external AI provider or tool execution is
enabled. See the local manager guide for runtime and persistence limits.

## omm install, uninstall, and upgrade

`install`, `uninstall`, `info`, and `upgrade` accept either a model name/reference or the numeric index shown by the last `omm search` or `omm list` run in that terminal. `omm info` and `omm fit` both work on a model that is not installed yet, so a search result can be inspected before downloading several GB; `omm info` describes the model, `omm fit` answers whether it runs on this machine. `search`/`install` mark models predicted not to run on this machine's hardware in red.

Before a normal interactive install, OMM shows a source card with the
provider, repository, file, expected size/location, GGUF format, and the
HTTPS/size/SHA-256 checks it plans to perform. Afterward it reports the checks
actually completed. “SHA-256 matched” means the bytes match the provider or
pinned digest; it does not claim the file is non-malicious. `--quiet`, piped/
non-interactive use, and internal contribution flows keep this extra
presentation out of scripted output.

Interrupted installs keep checkpoints under `OMM_HOME/install-journal`. Re-run
the original install command to recheck the file and repair links; `omm doctor`
lists incomplete attempts. See [install recovery](install-recovery.md).

`omm install org/repo --quant Q4_K_M` installs that quantization in one step
instead of opening the quant picker (case-insensitive). If the repo has no such
quant, it exits 1 and lists the quants it does have; if several files share that
quant, name the file instead (`omm install org/repo:<file>.gguf`).

`omm install --skip-unfit` is a scripting-friendly skip, not a successful
installation: it prints `Skipped` and leaves the model hub unchanged. If an
uninstall cannot remove the managed model file, OMM exits with status 1 and
keeps the registry and any still-live links so the same command can be retried.

`omm upgrade` no longer re-downloads a model against its own source. To
re-check an installed file and replace it only if the source actually
changed, use `omm install <name> --force`.

## omm verify

`omm verify` checks more than a link: it asks before starting a stopped local
runtime or loading an unloaded model, sends one short deterministic prompt,
requires a non-empty answer, and stops or unloads only what OMM started for
the check. It never deletes the model or stores the generated answer. Use
`--yes` for unattended confirmation and `--keep-loaded` to preserve a model
OMM loaded for the check. LM Studio API authentication reads
`LM_API_TOKEN` from the process environment and never writes it to
`config.json`. Compatibility status is stored locally in `models.json` and is
shown by `omm info`.

## omm info

`omm info MODEL_A MODEL_B` compares installed and remote model packages side by
side, without requiring membership in the recommendation catalog. Source,
version, size, license, architecture and local links stay separate facts;
unreported values remain unknown. One model keeps the detailed view and flat
JSON; multiple models return a `models` array with `--json`. Use `omm fit` for
hardware suitability and `omm recommend` for ranked suggestions.

## omm evaluate

`omm evaluate` currently supports installed Ollama models and the versioned
Python coding smoke pack. Model-generated source runs only through Docker or
Podman with no network, a read-only workspace, bounded resources, dropped
capabilities, and a timeout. Generated source is neither stored nor uploaded;
`--output` contains only task outcomes and exact model/pack identity. This pack
is a small reproducibility check, not a leaderboard. OMM never pulls the
container image implicitly; if it is absent, the error prints the exact
digest-pinned `docker pull`/`podman pull` command for the user to run explicitly.

## omm benchmark

`omm benchmark` runs a versioned eight-item bilingual arithmetic smoke pack
against models already installed in the selected Ollama or LM Studio runtime.
It stores parsed answers,
correctness, pinned model metadata, and fixed-length timings under
`OMM_HOME/evaluations/` (default `~/.omm/evaluations/`); it stores no generated text. Opt-in telemetry sends a locally
computed CPU chip score (and GPU chip score, when a GPU is present) plus
architecture and core counts — never the raw CPU/GPU model name — so speed
predictions can distinguish otherwise identical Linux `x86_64` machines.
Results are uploaded only after explicit opt-in. The pack is intentionally
small and is not a leaderboard. With Ollama, each measured speed also updates
a per-machine speed correction used by `omm recommend` (kept in `~/.omm`, never
uploaded; skipped when other programs were already using the CPU).

## omm contribute

```sh
omm contribute --max-minutes 30 --max-download-gb 10 --max-models 3
```

`omm contribute` keeps models and partial downloads already on your computer;
it removes only the temporary models created by that session. Choose an optional
time, model-data download, or new-model count limit, or combine them as above.
The download limit is in GiB and includes retries; metadata and HTTP/TLS overhead
are excluded. A time limit requests cancellation and then allows safe cleanup
to finish. Without these flags, the loop continues until Esc or candidate exhaustion.
The startup notice explains what is sent and whether the configured collector is
public. The final summary separates successful measurements, accepted uploads,
failed sends saved for retry, and the files kept or removed. The
[recommendation data contribution guide](contribution-sessions.md) gives a
one-model starting point, cross-computer instructions, and the current quality-data
limits.

`omm contribute` performs a 10 GiB startup free-space preflight. Before each
download it separately budgets the central GGUF, a worst-case full runner copy,
any required Windows cross-volume copies, and safety headroom. Each model
evaluation prints a heartbeat every 30 seconds and is terminated after an
absolute 10-minute deadline instead of hanging an unattended session
indefinitely.

## Memory budget

Localfit does not assume all installed memory belongs to the model. A live
scan subtracts memory currently used by other applications, keeps at least
2 GB (or 10% of RAM) for the OS and newly opened apps, and applies total-memory
caps. Recommendation fit and `omm tune` use this safe budget, so rerunning a
command adapts after memory-heavy applications are opened or closed.

## Windows: Ollama detection and file locks

On Windows, Ollama is detected by its HTTP API first, so a freshly installed
tray app works even before the current terminal receives the new `PATH`.
When the daemon is stopped, omm also checks Ollama's documented
`%LOCALAPPDATA%\Programs\Ollama` location. It only stops daemon processes it
started itself. Before deleting a contribution model, omm requests an Ollama
unload, waits for `/api/ps` to confirm handle release, and uses bounded retries
for Windows file locks. Real-time antivirus can still delay a first load; the
benchmark uses repeated samples and reports their median. Do not disable your
antivirus for omm.

## Automatic import

Automatic import is off by default. Enabling it registers a per-user
background service that adopts newly discovered models without a prompt.
It requires the optional `watch` dependencies (`watchdog` and `plyer`);
for a pip installation, use `python -m pip install "omm-model[watch]"` in
that installation's environment. The status command reports configuration
and service registration; it does not prove that a particular file has
been imported. Check `omm list` after the file has finished downloading.

## Runtime profiles

Saved runtime profiles are tied to the exact model file and engine. `omm verify`
and supported `omm run` paths use them on the next owned load; running models
keep their current settings. See [runtime profiles](runtime-profiles.md)
for engine capabilities, memory checks, cleanup, and verification limits.
