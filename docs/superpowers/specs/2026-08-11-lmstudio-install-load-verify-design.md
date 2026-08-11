# LM Studio install-time load verification design

## Problem

`omm install` links a GGUF into LM Studio's models directory
(`linker.link_lmstudio`, `linker.py:459`), but never confirms the model
actually loads there. Ollama gets this for free the first time a user runs
`omm benchmark` (`quality.py`'s `/api/generate` call forces a real load),
but LM Studio has no benchmark path at all (`benchmark.py:3`: "LM Studio
benchmarking can be added later") and no equivalent check ever runs. A
linked-but-unloadable LM Studio model (bad format, drifted directory
layout) currently looks identical to a working one — this is GitHub issue
#19.

## Findings from real-device testing (LM Studio 0.4.20, macOS)

- `lms server status --json` → `{"running": bool, "port": int}`. Reliable
  source of truth for both reachability and port (port is user-configurable,
  never hardcode 1234).
- `lms` ships inside the app bundle
  (`<App>/Contents/Resources/app/.webpack/lms`) and bootstraps itself into
  `<lmstudio_home_dir>/bin/lms` the first time LM Studio is run. Not
  guaranteed to be on `PATH` in a non-interactive shell.
- `POST /v1/chat/completions` with `"model": "<repo>"` **JIT-loads the
  model** if it isn't already loaded — confirmed against a real symlinked
  GGUF (`tinyllama-test`). No explicit `lms load` call needed, mirroring
  Ollama's `/api/generate` auto-load behavior.
- The model identifier LM Studio expects is exactly the `repo` value omm
  already computes via `_lmstudio_publisher_repo()` (`linker.py:452`) —
  confirmed via `/v1/models` returning `"id": "tinyllama-test"` for a
  `models/local/tinyllama-test/tinyllama-1.1b-...gguf` layout.
- `lms unload <repo>` unloads a specific model by that same identifier.
- The background LM Studio service answers `lms` commands even without the
  GUI window focused.

## Solution

Add a best-effort, non-blocking load probe in `linker.py`, run once after
`omm install` successfully links into LM Studio. Failure never fails the
install — it only prints a warning, matching the fact that LM Studio (unlike
Ollama) has no native-create fallback to fall back to.

### New functions (`linker.py`)

- `_lms_cli_path() -> str | None`
  `shutil.which("lms")` first, else `lmstudio_home_dir() / "bin" / "lms"`
  if that file exists. `None` if neither is found.
- `_lmstudio_server_status(lms_path: str, timeout: float = 5) -> dict | None`
  Runs `lms server status --json`, parses `{"running": bool, "port": int}`.
  `None` on any subprocess/parse failure (never raises).
- `_start_lmstudio_server(lms_path: str, timeout: float = 30) -> bool`
  Runs `lms server start`, then polls `_lmstudio_server_status` (bounded by
  `timeout`) until `running` is `True`. Returns `False` on timeout/failure.
- `_stop_lmstudio_server(lms_path: str) -> None`
  Best-effort `lms server stop`; swallows failures.
- `_probe_lmstudio_generate(port: int, repo: str, timeout: float = 120) -> bool | None`
  `POST http://127.0.0.1:{port}/v1/chat/completions` with a short fixed
  prompt and a small `max_tokens`. `True` if the response contains
  non-empty message content, `False` on an HTTP error or empty/malformed
  response, `None` on a network error or timeout (inconclusive — the
  generous timeout budgets for first-load time on a large model).
- `_lms_unload(lms_path: str, repo: str) -> None`
  Best-effort `lms unload <repo>`; swallows failures. Always attempted
  after a probe, isolating the loaded model the same way
  `quality.unload_model` does for Ollama.
- `verify_lmstudio_load(gguf_path: Path, repo_id: str | None) -> bool | None`
  Orchestrator, called once per successful `link_lmstudio`:
  1. Resolve `repo` via the existing `_lmstudio_publisher_repo`.
  2. `lms_path = _lms_cli_path()`; return `None` immediately if missing.
  3. `status = _lmstudio_server_status(lms_path)`; return `None` if
     unreachable.
  4. If `not status["running"]`: attempt `_start_lmstudio_server`; return
     `None` on failure. Remember that *this call* started the server.
  5. Run `_probe_lmstudio_generate(status["port"], repo)`.
  6. Always run `_lms_unload(lms_path, repo)` afterward, regardless of the
     probe result.
  7. If this call started the server in step 4, `_stop_lmstudio_server`
     it now — never touch a server that was already running before this
     call.
  8. Return the probe's `bool | None` result.

### Wiring (`cli.py`)

In `install()`, after the existing `outcome.linked.get("ollama")` block
(`cli.py:1962`), add:

```python
if outcome.linked.get("lmstudio"):
    result = linker.verify_lmstudio_load(MODELS_DIR / outcome.filename, outcome.repo_id)
    if result is False:
        console.print(
            "[yellow]Warning: LM Studio linked this model but it did not "
            "load successfully in a live test.[/yellow]"
        )
```

`result is None` (lms not found, server unreachable, or inconclusive
timeout) prints nothing — matches the existing silent-skip convention used
by `_ollama_accepts_manifest` for the same class of "nothing to compare
against" case. `result is True` also prints nothing, consistent with the
current install output already being silent on Ollama's own successful
compat check.

`InstallOutcome` (`cli.py:1448`) already carries `filename` and `repo_id` —
no new field needed. `MODELS_DIR / outcome.filename` reconstructs the same
central-hub path `link_lmstudio` was originally given.

## Out of scope

- No LM Studio benchmark support (separate, larger effort per
  `benchmark.py:3`).
- No explicit `lms load` / manual context-length or GPU-offload control —
  JIT defaults are used, matching how Ollama's own probe never overrides
  runtime options either.
- No handling for LM Studio server port changes made *during* a probe
  (status is read once per `verify_lmstudio_load` call).
- No retry loop on probe failure — one attempt, warn once.

## Testing

- Unit tests in the LM Studio test module, mocking `subprocess.run` (for
  `lms` calls) and the HTTP client (for `/v1/chat/completions`,
  `/v1/models`/status), covering:
  - `lms` missing → `None`, no server calls attempted.
  - Server already running → probe runs, server never stopped afterward.
  - Server not running → started, probed, then stopped again.
  - Server start failure → `None`, no probe attempted.
  - Successful generation → `True`, `lms unload` called.
  - Empty/error response → `False`, `lms unload` still called.
  - Timeout during probe → `None`, `lms unload` still called, server still
    stopped if we started it.
- `cli.py` install-flow test: `outcome.linked["lmstudio"] = True` +
  `verify_lmstudio_load` returning `False` prints the yellow warning but
  `install` still exits 0.
- No new real-device verification needed beyond what's already recorded
  above — the empirical findings this design is based on were captured
  directly against a real LM Studio 0.4.20 install during design.
