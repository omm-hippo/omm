# Cluster Poster Art Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every omm cluster Mac can run a background service that pulls dshs.dev poster jobs, draws them with mflux (FLUX.2-klein-4B) and uploads the JPEG, keeping the site's contract.

**Architecture:** A worker process (omm's Python, LaunchAgent `com.omm.cluster-poster`) does HTTP, keychain, memory/exo gating and timing; it drives a drawer child process (a separate venv with mflux) over JSON lines on stdin/stdout. Ending the drawer is how the model is unloaded. `omm cluster poster enable|disable` set it up; `omm cluster status` shows it.

**Tech Stack:** Python 3.10+ (rig: 3.15), Typer, `requests` (lazy import), macOS `security`, `launchctl`, mflux 0.24.0 in its own venv, pytest with a stdlib `http.server` fake.

**Spec:** `docs/superpowers/specs/2026-11-06-cluster-poster-art-worker-design.md`
(site contract: DSHS repo `docs/superpowers/specs/2026-11-06-poster-mac-art-design.md` §5-3, §7)

## Global Constraints

- Contract: claim `POST {SUPABASE_URL}/rest/v1/rpc/claim_poster_art_job`, ping `.../rpc/poster_art_ping`, both with headers `apikey: <anon>`, `Authorization: Bearer <anon>`, body `{"p_token": <token>}`. Claim reply `[{"id","prompt","width","height","job_secret"}]`, `[]` or `null`.
- Result `POST {SITE_URL}/api/poster-art/jobs/<id>/result`, headers `Authorization: Bearer <token>`, `X-Job-Secret`, `Content-Type: image/jpeg`, body one `bytes` object (real `Content-Length`), ≤ 4 MB (4 * 1024 * 1024). Fail `POST .../jobs/<id>/fail` JSON `{"reason": <≤200 chars>}`.
- Timing: claim idle sleep 3 s; network backoff 3, 6, 12, 24, 48, 60 s cap; ping every 15 s during a job; lease 180 s from local claim-reply time; drawing cut at deadline − 30 s; 502 retries after 5 s then 15 s, skipped if past deadline; drawer idle stop 15 min.
- 200 → done; 404/413/415 → drop (no retry, no fail); 502, other status, network error → retry schedule then fail.
- Size: scale down to area ≤ 786432 keeping ratio, then floor each side to a multiple of 16; never round up.
- Model: repo `black-forest-labs/FLUX.2-klein-4B`, revision `e7b7dc27f91deacad38e78976d1f2b499d76a294`, allow_patterns `["model_index.json", "scheduler/*", "text_encoder/*", "tokenizer/*", "transformer/*", "vae/*"]`, mflux name `flux2-klein-4b`, `quantize=8`, 4 steps, guidance 1.0, JPEG quality 90. Venv requirement `mflux==0.24.0`.
- Memory gate: drawer not running → `hardware.calculate_memory_budget(hardware.scan_hardware()).model_budget_gb >= 16.0`.
- Paths: `OMM_HOME/cluster/poster/{settings.json,state.json,venv/,hf/,tmp/}`, wrapper `OMM_HOME/bin/omm-cluster-poster`, log `OMM_HOME/logs/cluster-poster.log`. Keychain service `omm-cluster-poster`, account `worker-token`.
- Token: never on argv, never in any log/state/exception text/output. Token regex `^[A-Za-z0-9_-]{20,200}$`. URLs must be `https://`.
- The prompt is never in a shell string, never logged, never in a fail reason.
- No listening socket; no pf/ALF/gateway changes; exo is read-only (`GET /state` via `ExoClient().raw_state()`).
- `requests`, `questionary` stay lazy-imported in `cli.py` (startup speed rule).
- Every new hidden subcommand `_cluster-poster-run` goes in all four skip sets in `cli.py` next to `_cluster-member-run`.

## Review Focus

1. Site down / DNS failure for hours → worker keeps backing off at 60 s, never crashes out of the loop, never claims while its gate is closed. Test in Task 5 (`test_loop_survives_repeated_network_errors`).
2. Drawer process dies mid-job (OOM kill by another account's job) → job gets `fail("drawer error")`, drawer restarts on the next job, no hang. Test in Task 3 (`test_drawer_crash_mid_request_raises`) and Task 5 (`test_run_job_drawer_crash_sends_fail`).
3. Malformed claim reply (missing field, width as string, non-UUID id, huge prompt) → never crashes; with a valid id the job is failed `"bad job"`, otherwise ignored. Test in Task 2 (`test_parse_job_rejects_bad_fields`).
4. `enable` run twice, or `disable` with nothing installed → idempotent, no traceback. Test in Task 6 (`test_enable_twice_replaces`, `test_disable_when_nothing_installed`).
5. Token leaks through error text (`security` echoing its input line, `requests` exception) → scrubbed. Test in Task 1 (`test_store_token_failure_scrubs_token`) and Task 5 (`test_token_never_logged`).

---

### Task 1: Poster settings, env parsing, keychain

**Files:**
- Create: `src/omm/cluster/poster_config.py`
- Test: `tests/test_cluster_poster_config.py`

**Interfaces:**
- Produces:
  - constants `KEYCHAIN_SERVICE = "omm-cluster-poster"`, `KEYCHAIN_ACCOUNT = "worker-token"`, `MODEL_REPO`, `MODEL_REVISION`, `MODEL_ALLOW_PATTERNS: tuple[str, ...]`, `MFLUX_MODEL = "flux2-klein-4b"`, `QUANTIZE = 8`, `STEPS = 4`, `JPEG_QUALITY = 90`, `MFLUX_REQUIREMENT = "mflux==0.24.0"`, `MODEL_NEED_GB = 16.0`
  - `poster_dir() -> Path` (reads `config.OMM_HOME` at call time)
  - `@dataclass(frozen=True) class PosterSettings: supabase_url: str; anon_key: str; site_url: str`
  - `parse_env(text: str) -> tuple[PosterSettings, str]` (settings, token); raises `ClusterError`
  - `validate(settings: PosterSettings, token: str) -> PosterSettings` (returns normalized: trailing `/` stripped); raises `ClusterError`
  - `save_settings(s: PosterSettings) -> None`, `load_settings() -> PosterSettings | None`, `delete_settings() -> None`
  - `store_token(token: str, *, run=subprocess.run) -> None`, `read_token(*, run=subprocess.run) -> str | None`, `delete_token(*, run=subprocess.run) -> None`

- [ ] **Step 1: Write the failing tests**

```python
def test_parse_env_reads_four_keys():
    text = "# c\nSUPABASE_URL=https://x.supabase.co/\nSUPABASE_ANON_KEY=anon\nSITE_URL=https://dshs.dev\nTOKEN=" + "a" * 43 + "\n"
    settings, token = poster_config.parse_env(text)
    assert settings == poster_config.PosterSettings("https://x.supabase.co", "anon", "https://dshs.dev")
    assert token == "a" * 43

@pytest.mark.parametrize("bad", ["SUPABASE_URL=http://x", "TOKEN=short", "TOKEN=" + "a" * 30 + " b"])
def test_parse_env_rejects(bad): ...  # each: ClusterError, and the token value is not in str(error)

def test_missing_key_names_it():  # ClusterError mentions "SITE_URL", never the token

def test_store_token_feeds_stdin_not_argv():
    calls = []
    def run(argv, **kw): calls.append((argv, kw)); return subprocess.CompletedProcess(argv, 0, "", "")
    poster_config.store_token("t" * 43, run=run)
    argv, kw = calls[0]
    assert argv == ["/usr/bin/security", "-i"] and "t" * 43 not in " ".join(argv)
    assert "add-generic-password -U -s omm-cluster-poster -a worker-token -w " + "t" * 43 in kw["input"]

def test_store_token_failure_scrubs_token():
    run = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "add-generic-password ... -w " + "t" * 43 + "\nUser interaction is not allowed.")
    with pytest.raises(ClusterError) as e: poster_config.store_token("t" * 43, run=run)
    assert "t" * 43 not in str(e.value) and "unlock-keychain" in str(e.value)

def test_read_token_none_when_missing():  # returncode 44 -> None
def test_settings_round_trip(isolated_omm_home):  # save/load/delete; file has no "token" key
```

- [ ] **Step 2: Run, expect failure**

Run: `PYTHONPATH=src python -m pytest tests/test_cluster_poster_config.py -q`
Expected: FAIL, `ModuleNotFoundError: omm.cluster.poster_config`

- [ ] **Step 3: Implement `poster_config.py`**

`parse_env`: ignore blank and `#` lines, split on first `=`, strip whitespace and one pair of surrounding quotes; required keys `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SITE_URL`, `TOKEN`; then `validate`. `validate`: `urllib.parse.urlsplit` scheme `https`, non-empty netloc, no query/fragment. `store_token`: validate token regex first; `run(["/usr/bin/security", "-i"], input=line + "\n", capture_output=True, text=True, check=False)`; on non-zero raise `ClusterError` whose text replaces the token with `***` and says to run `security unlock-keychain` first or run enable in the Mac's own Terminal. `read_token`: `security find-generic-password -s ... -a ... -w`, stripped stdout or None. `delete_token`: `security delete-generic-password ...`, ignore "not found". Settings written with `omm.atomic.atomic_write_text`, mode 0600.

- [ ] **Step 4: Run, expect pass** (same command) — PASS

- [ ] **Step 5: Commit**

```bash
git add src/omm/cluster/poster_config.py tests/test_cluster_poster_config.py
git commit -m "feat(cluster): poster worker settings and keychain token"
```

### Task 2: Site contract client

**Files:**
- Create: `src/omm/cluster/poster_contract.py`
- Test: `tests/test_cluster_poster_contract.py` (stdlib `http.server.ThreadingHTTPServer` fake on 127.0.0.1; tests pass its `http://` base directly to `SiteClient`, which does not re-validate scheme)

**Interfaces:**
- Consumes: `PosterSettings` (Task 1)
- Produces:
  - `MAX_AREA = 786_432`, `SIZE_MULTIPLE = 16`, `MAX_IMAGE_BYTES = 4 * 1024 * 1024`, `RETRY_DELAYS = (5, 15)`
  - `@dataclass(frozen=True) class Job: id: str; prompt: str; width: int; height: int; job_secret: str`
  - `class TransientError(Exception)`
  - `class BadJob(Exception): job_id: str | None`
  - `fit_size(width: int, height: int) -> tuple[int, int]`
  - `parse_job(body) -> Job | None` (None for `[]`/`null`; raises `BadJob`)
  - `class SiteClient(settings: PosterSettings, token: str, *, session=None, timeout: float = 30)` with `claim() -> Job | None` (raises `TransientError` on network error / 5xx / non-JSON), `ping() -> bool` (never raises), `upload(job: Job, jpeg: bytes) -> int` (HTTP status, 0 on network error), `fail(job_id: str, reason: str) -> None` (never raises)
  - `upload_with_retries(client: SiteClient, job: Job, jpeg: bytes, deadline: float, *, now=time.monotonic, sleep=time.sleep) -> str` returning `"done" | "dropped" | "failed"` (sends `fail("upload failed")` itself on `"failed"`)

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("w,h,expected", [(1024, 768, (1024, 768)), (1000, 750, (992, 736)),
                                          (2048, 1536, (1024, 768)), (17, 17, (16, 16))])
def test_fit_size(w, h, expected): assert poster_contract.fit_size(w, h) == expected
def test_fit_size_area_never_exceeds(): ...  # loop w,h in range(64, 4096, 97): area <= 786432, both %16 == 0, both >= 16

def test_claim_sends_contract_headers(fake): ...  # path /rest/v1/rpc/claim_poster_art_job, apikey + Bearer anon, json {"p_token": TOKEN}
def test_claim_empty_and_null(fake): ...          # "[]" and "null" -> None
def test_claim_5xx_and_connection_refused_raise_transient(fake): ...
def test_parse_job_rejects_bad_fields():          # non-UUID id -> BadJob(job_id=None); width "1024" or 0 -> BadJob(job_id=<id>); missing job_secret -> BadJob(job_id=<id>)
def test_upload_has_content_length_and_headers(fake):  # Content-Length == len(jpeg), no Transfer-Encoding, Bearer TOKEN, X-Job-Secret, image/jpeg
@pytest.mark.parametrize("code", [404, 413, 415])
def test_upload_drop_codes_no_retry_no_fail(fake, code): ...  # one POST, no /fail, returns "dropped"
def test_upload_502_retries_5_then_15_then_fail(fake): ...    # sleeps == [5, 15], 3 POSTs, then /fail {"reason": "upload failed"}
def test_upload_retry_past_deadline_goes_to_fail(fake): ...   # now() past deadline after first 502 -> no sleep, fail
def test_fail_reason_truncated_to_200(fake): ...
```

- [ ] **Step 2: Run, expect failure** — `PYTHONPATH=src python -m pytest tests/test_cluster_poster_contract.py -q` → ModuleNotFoundError

- [ ] **Step 3: Implement `poster_contract.py`**

`fit_size`: if area > MAX_AREA scale by `sqrt(MAX_AREA / area)`, then `max(16, floor(x / 16) * 16)` per side; if flooring to 16 minimum still exceeds area (cannot happen for sane inputs) clamp. `requests` imported inside `SiteClient.__init__` (lazy). `upload` posts `data=jpeg` (bytes). Job id validated with `uuid.UUID(id)` and reformatted canonical before use in a URL path.

- [ ] **Step 4: Run, expect pass** — PASS

- [ ] **Step 5: Commit** — `git add src/omm/cluster/poster_contract.py tests/test_cluster_poster_contract.py && git commit -m "feat(cluster): poster art site contract client"`

### Task 3: Drawer script and its parent-side handle

**Files:**
- Create: `src/omm/cluster/poster_draw.py` (standalone; imports only stdlib + mflux + PIL; never imports `omm`)
- Create: `src/omm/cluster/poster_drawer.py`
- Test: `tests/test_cluster_poster_drawer.py` (uses a fake drawer script written to `tmp_path` that speaks the same protocol, run with `sys.executable`)

**Interfaces:**
- Produces:
  - Protocol: drawer argv `[<python>, "-I", <poster_draw.py>, "--model-path", <snapshot>, "--model", "flux2-klein-4b", "--quantize", "8", "--steps", "4"]`, env adds `HF_HUB_OFFLINE=1`. After load it prints `{"ready": true, "seconds": <float>}`. Per request line `{"prompt", "width", "height", "seed", "out", "quality"}` it prints `{"ok": true, "seconds": <float>}` or `{"ok": false, "error": "<short class name>"}`. EOF on stdin → exit 0.
  - `class DrawerError(Exception)`, `class DrawerTimeout(DrawerError)`
  - `class Drawer(argv: list[str], env: dict[str, str], *, popen=subprocess.Popen)` with `running: bool` property, `start(timeout: float) -> float` (load seconds), `draw(prompt: str, width: int, height: int, seed: int, out: Path, timeout: float) -> float`, `stop() -> None` (close stdin, wait 10 s, then kill), `idle_seconds(now: float) -> float`
  - `drawer_argv(venv: Path, snapshot: Path) -> list[str]` and `drawer_env(home: Path) -> dict[str, str]` (HF_HOME=`poster_dir()/hf`, HF_HUB_OFFLINE=1, PATH minimal)

- [ ] **Step 1: Write the failing tests**

```python
def test_draw_round_trip(fake_drawer): ...           # start -> ready; draw writes file `out`; returns seconds
def test_prompt_travels_only_in_json(fake_drawer):   # prompt "x; rm -rf ~ $(id)" reaches the fake as-is via stdin JSON; argv has no prompt
def test_draw_timeout_kills_process(fake_drawer_slow):  # DrawerTimeout within timeout+2 s; running is False after
def test_drawer_crash_mid_request_raises(fake_drawer_crash):  # DrawerError; running False
def test_error_reply_raises(fake_drawer_error):      # {"ok": false} -> DrawerError
def test_stop_is_idempotent(fake_drawer): ...
```

- [ ] **Step 2: Run, expect failure** — `PYTHONPATH=src python -m pytest tests/test_cluster_poster_drawer.py -q` → ModuleNotFoundError

- [ ] **Step 3: Implement `poster_drawer.py` and `poster_draw.py`**

Parent reads stdout lines with a timeout using a reader thread + `queue.Queue` (no `select` on pipes, keeps it simple and portable). `poster_draw.py`: build `Flux2Klein(model_config=<flux2-klein-4b config>, quantize=8, model_path=<snapshot>)` and call `generate_image(seed=, prompt=, width=, height=, guidance=1.0, num_inference_steps=4, scheduler="flow_match_euler_discrete")`, save via PIL as JPEG quality 90 to `out`. Verify the exact constructor/config call against `mflux/models/flux2/cli/flux2_generate.py` in mflux 0.24.0 (on the rig: `~/poster-spike/venv315` on Node1) — config resolution there is `ConfigResolution.resolve_restricted(...)`; `ModelConfig.from_name("flux2-klein-4b")` exists at `mflux/models/common/config/model_config.py:327`. Any exception in a request → `{"ok": false, "error": type(e).__name__}` (never the message, which can include the prompt).

- [ ] **Step 4: Run, expect pass** — PASS

- [ ] **Step 5: Commit** — `git add src/omm/cluster/poster_draw.py src/omm/cluster/poster_drawer.py tests/test_cluster_poster_drawer.py && git commit -m "feat(cluster): mflux drawer process for poster art"`

### Task 4: Drawing runtime (venv + model download)

**Files:**
- Create: `src/omm/cluster/poster_runtime.py`
- Test: `tests/test_cluster_poster_runtime.py`

**Interfaces:**
- Consumes: Task 1 constants, `poster_dir()`
- Produces:
  - `venv_dir() -> Path`, `venv_python() -> Path`, `snapshot_marker() -> Path` (`poster_dir()/hf/ready.json` holding `{"revision", "path"}`)
  - `build_venv(*, run=subprocess.run, out=print) -> None` — `<base python> -m venv <venv>` then `<venv>/bin/python -m pip install --disable-pip-version-check mflux==0.24.0`; raises `ClusterError` with the last 20 lines of pip output on failure
  - `venv_ready() -> bool` (venv python exists and `-I -c "import mflux"` exits 0)
  - `model_snapshot() -> Path | None` (from marker, only if revision matches and dir exists)
  - `download_model(*, popen=subprocess.Popen, now=time.monotonic, sleep=time.sleep, stall_seconds: float = 300) -> Path` — runs the venv python with a small `-I -c` program calling `huggingface_hub.snapshot_download(MODEL_REPO, revision=MODEL_REVISION, allow_patterns=list(MODEL_ALLOW_PATTERNS), cache_dir=poster_dir()/"hf")` that prints the path; watchdog: if total bytes under `poster_dir()/hf` do not grow for `stall_seconds`, kill it, delete `*.lock` under `hf/.locks`, retry (max 5 attempts); writes the marker on success
  - `remove_all() -> None` (rmtree `poster_dir()/venv`, `/hf`, `/tmp`)

- [ ] **Step 1: Write the failing tests**

```python
def test_build_venv_pins_mflux(isolated_omm_home): ...      # second run argv ends with ["install", "--disable-pip-version-check", "mflux==0.24.0"]
def test_download_writes_marker(isolated_omm_home): ...      # fake popen prints a path -> model_snapshot() == that path
def test_download_stall_restarts_and_clears_locks(isolated_omm_home): ...  # fake popen never grows; fake now jumps 301 s; lock file removed; second attempt succeeds
def test_snapshot_ignored_when_revision_differs(isolated_omm_home): ...
def test_remove_all_is_idempotent(isolated_omm_home): ...
```

- [ ] **Step 2: Run, expect failure** — `PYTHONPATH=src python -m pytest tests/test_cluster_poster_runtime.py -q`

- [ ] **Step 3: Implement `poster_runtime.py`** — base python: `getattr(sys, "_base_executable", sys.executable)`; the download program receives repo/revision/patterns/cache_dir through `argv` (not string-formatted into the code).

- [ ] **Step 4: Run, expect pass** — PASS

- [ ] **Step 5: Commit** — `git add src/omm/cluster/poster_runtime.py tests/test_cluster_poster_runtime.py && git commit -m "feat(cluster): poster drawing venv and pinned model download"`

### Task 5: Worker loop

**Files:**
- Create: `src/omm/cluster/poster_worker.py`
- Test: `tests/test_cluster_poster_worker.py` (fake `SiteClient`, fake `Drawer`, fake exo state dicts, injected clock)

**Interfaces:**
- Consumes: Tasks 1-4; `exo_client.ExoClient().raw_state()`, `exo_client.ExoUnavailable`; `hardware.scan_hardware`, `hardware.calculate_memory_budget`
- Produces:
  - `IDLE_SECONDS = 3`, `PING_SECONDS = 15`, `LEASE_SECONDS = 180`, `DRAW_MARGIN_SECONDS = 30`, `DRAWER_IDLE_SECONDS = 900`, `BACKOFF = (3, 6, 12, 24, 48, 60)`
  - `exo_wants_memory(raw_state: dict | None, own_node_id: str | None) -> bool` — True when an instance has this node in its shard assignments and not every runner of it is `RunnerReady`, or a runner on this node reports a failed/insufficient-memory state. `None` state → False. Verify field names against `tests/fixtures/exo/` and the live rig.
  - `gate(drawer_running: bool, exo_busy: bool, budget_gb: float, pressure_critical: bool) -> tuple[bool, str]` — pure; returns `(open, phase)` with phases `"ready"`, `"waiting for memory"`, `"cluster model loading"`
  - `@dataclass class State` + `write_state(state) -> None` / `read_state() -> dict | None` (`poster_dir()/state.json`; keys `phase`, `model_loaded`, `last_job_id`, `last_outcome`, `last_seconds`, `last_at`, `updated_at`)
  - `run_job(job: Job, client: SiteClient, drawer: Drawer, claimed_at: float, *, now=time.monotonic) -> str` — outcome `"done" | "dropped" | "failed"`
  - `run_worker() -> int` (entry for `_cluster-poster-run`; never returns on its own except when settings/token vanish → returns 0 after logging)
  - `status_line(state: dict | None, installed: bool) -> str` — the four spec phrasings ("Poster art: off", "on — downloading the model", "on — waiting for memory (12 GiB free, needs 16)", "on — model loaded, last job 3f2a… done in 32 s (4 min ago)")

- [ ] **Step 1: Write the failing tests**

```python
def test_gate_closed_when_memory_short(): assert worker.gate(False, False, 12.0, False) == (False, "waiting for memory")
def test_gate_open_when_drawer_running_even_if_budget_low(): assert worker.gate(True, False, 2.0, False)[0]
def test_gate_closed_when_exo_loading(): assert worker.gate(True, True, 40.0, False) == (False, "cluster model loading")
def test_gate_closed_on_critical_pressure(): assert not worker.gate(True, False, 40.0, True)[0]
def test_exo_wants_memory_from_fixture(): ...      # a loading instance on own node -> True; all RunnerReady -> False; None -> False
def test_no_claim_until_model_downloaded(): ...  # model_snapshot() None -> phase "downloading", download_model called, zero client calls before it returns
def test_no_claim_or_ping_while_gate_closed(): ... # one loop iteration with budget 12 -> fake client saw zero calls
def test_idle_drawer_stopped_when_exo_loading(): ...
def test_run_job_pings_every_15s(): ...            # fake drawer takes 40 fake seconds -> >= 2 pings
def test_run_job_timeout_sends_fail(): ...         # drawer timeout passed == claimed_at + 180 - 30 - now(); DrawerTimeout -> fail("timeout")
def test_run_job_drawer_crash_sends_fail(): ...    # fail("drawer error"); drawer restarted on next job
def test_run_job_uses_fit_size(): ...              # job 1000x750 -> drawer got 992x736
def test_run_job_oversize_jpeg_reencoded_once_then_fail(): ...
def test_loop_survives_repeated_network_errors(): ...  # sleeps follow 3,6,12,24,48,60,60
def test_bad_job_with_id_is_failed(): ...          # BadJob(job_id=X) -> fail(X, "bad job")
def test_token_never_logged(caplog): ...           # full happy path + errors; token absent from every record and from state.json
def test_state_has_no_prompt_or_token(isolated_omm_home): ...
def test_status_line_phrasings(): ...              # the four spec strings exactly
```

- [ ] **Step 2: Run, expect failure** — `PYTHONPATH=src python -m pytest tests/test_cluster_poster_worker.py -q`

- [ ] **Step 3: Implement `poster_worker.py`** — logger `logging.getLogger("omm.cluster.poster")`; one line per job: `poster job %s: %s in %.0f s` (id, outcome, seconds). Memory pressure: `sysctl -n kern.memorystatus_vm_pressure_level` (4 = critical). Own node id: `ExoClient().node_id()` cached, refreshed when exo restarts (unknown → treat exo as not loading here). Ping thread: `threading.Event`-driven, stops in `finally`. Fresh random seed per job (`secrets.randbelow(2**31)`). Temp JPEG under `poster_dir()/tmp`, deleted after upload. Before the loop: `model_snapshot()` or else phase "downloading" + `download_model()` (a download error logs, sleeps 60 s, retries; never claims meanwhile). Reload settings/token only at start; a missing token → log once, sleep 60 s, re-read.

- [ ] **Step 4: Run, expect pass** — PASS

- [ ] **Step 5: Commit** — `git add src/omm/cluster/poster_worker.py tests/test_cluster_poster_worker.py && git commit -m "feat(cluster): poster art worker loop"`

### Task 6: Service, CLI, status, leave, update hooks, docs

**Files:**
- Create: `src/omm/cluster/poster_service.py` (copy of `member_service.py` shape: `LAUNCHD_LABEL = "com.omm.cluster-poster"`, `_SUBCOMMAND = "_cluster-poster-run"`, wrapper `OMM_HOME/bin/omm-cluster-poster`, log `cluster-poster.log`, `install(*, run)`, `uninstall(*, run)`, `is_installed() -> bool`, `is_running(*, run) -> bool`, `restart_if_installed(*, run) -> None`)
- Modify: `src/omm/cli.py` — new `cluster_poster_app = typer.Typer(name="poster", help="Draw dshs-style poster art jobs on this cluster Mac with mflux (pulls work over HTTPS, opens no port).", ...)` added to `cluster_app`; commands `enable` (`--env-file`), `disable`; hidden `_cluster-poster-run` next to `_cluster-member-run:3389`; add `"_cluster-poster-run"` to the four skip sets (`:2585`, `:3468`, `:3600`, `:3613`); status printing after the document-search line (`:2764`); `_after_update_on_cluster_head` (`:5170`) also calls `poster_service.restart_if_installed()` — move the poster restart before the head-only return so it runs on any cluster Mac.
- Modify: `src/omm/cluster/commands.py` — `status()` report gains `"poster": {"installed": bool, "running": bool, "state": dict | None}` (for every role); `leave()` steps gain `poster_disable` (uninstall agent, delete token, delete settings, `poster_runtime.remove_all`).
- Modify: `src/omm/cluster/member_link.py:299` — before returning on `"updated"`/`"switched"`, `poster_service.restart_if_installed()` (errors logged, never raised).
- Modify: `README.md` `## Usage`, `docs/commands.json` (regenerate), `CLAUDE.md` (new "Poster art worker" paragraph under Cluster, ~8 lines: what, two-process shape, paths, keychain, gate incl. exo-first, contract doc pointer).
- Test: `tests/test_cli_cluster_poster.py`, additions in `tests/test_cluster_update_hook.py`

**Interfaces:**
- Consumes: everything above
- Produces: user commands `omm cluster poster enable [--env-file PATH|-]`, `omm cluster poster disable`

`enable` flow: require macOS and `cluster settings.load() is not None` (else `ClusterError("This Mac is not in a cluster. Run omm cluster first; the poster worker runs only on cluster Macs.")`); read values (env file / stdin / interactive with hidden token prompt via the existing lazy `questionary` helpers); `validate`; `store_token`; `save_settings`; `build_venv` unless `venv_ready()` (prints progress lines); `poster_service.install()`; print "Poster art worker is on. It downloads the model (~15 GB) in the background before taking jobs; omm cluster status shows progress." `disable` flow: the leave step above, then "Poster art worker is off; its token, settings and model files were removed."

- [ ] **Step 1: Write the failing tests**

```python
def test_enable_refuses_outside_cluster(isolated_omm_home): ...      # exit 1, message above, no keychain call
def test_enable_from_stdin_env(isolated_omm_home, fakes): ...         # input=env text; store_token got token; settings saved; agent installed; token not in result.output
def test_enable_rejects_http_url(isolated_omm_home, fakes): ...
def test_enable_twice_replaces(isolated_omm_home, fakes): ...         # second token replaces first, one agent install each time, no error
def test_disable_removes_everything(isolated_omm_home, fakes): ...
def test_disable_when_nothing_installed(isolated_omm_home, fakes): ...  # exit 0
def test_status_shows_poster_line(isolated_omm_home, fakes): ...      # "Poster art: off" and an "on — ..." case; --json has "poster"
def test_leave_runs_poster_disable(isolated_omm_home, fakes): ...
def test_update_restarts_poster_on_member(...): ...                    # in test_cluster_update_hook.py: role member + installed -> restart called with "com.omm.cluster-poster"
def test_hidden_run_command_in_skip_sets(): ...                        # "_cluster-poster-run" in all four sets
```

- [ ] **Step 2: Run, expect failure** — `PYTHONPATH=src python -m pytest tests/test_cli_cluster_poster.py tests/test_cluster_update_hook.py -q`

- [ ] **Step 3: Implement** the files listed above.

- [ ] **Step 4: Run the cluster suites and docs check**

Run: `env -u FORCE_COLOR PYTHONPATH=src python -m pytest tests/test_cluster_*.py tests/test_cli_cluster*.py tests/test_cluster_update_hook.py -q && python scripts/export_command_reference.py && python scripts/check_docs_sync.py`
Expected: all pass; docs-sync OK. (Do not run `tests/test_cli_update.py` without checking `~/.omm/src` caution in CLAUDE.md.)

- [ ] **Step 5: Smoke the entry point** — `PYTHONPATH=src python -m omm.cli cluster poster --help` lists `enable` and `disable`; `omm help` startup stays fast (no new top-level imports).

- [ ] **Step 6: Commit** — `git add src/omm/cluster/poster_service.py src/omm/cli.py src/omm/cluster/commands.py src/omm/cluster/member_link.py README.md docs/commands.json CLAUDE.md tests/test_cli_cluster_poster.py tests/test_cluster_update_hook.py && git commit -m "feat(cluster): omm cluster poster enable/disable and status"`

### Task 7: Real rig verification (user confirms before every remote command)

Not code. Each bullet is one remote action; ask the user before each.

- [ ] Install the branch on Node1 in a sandbox per CLAUDE.md (`pipx install --force --editable <clone> --suffix _verify`), or with the user's go-ahead update the real omm on the four Macs to the merged beta. Ask which.
- [ ] On Node1: pipe `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SITE_URL` and `TOKEN=<TOKEN_MINI_1>` from `~/.omm-poster-art-tokens.env` into `omm cluster poster enable --env-file -` over ssh, built in a local subshell so the token is never echoed (e.g. `awk` mapping `TOKEN_MINI_1` → `TOKEN`, piped straight into `ssh`). Check the keychain write works over ssh; if "User interaction is not allowed", ask the user to run `security unlock-keychain` on that Mac.
- [ ] Watch `omm cluster status` until "model loaded"/"ready"; ask the DSHS session to check `poster-art-worker.mts list` last-seen and to run one end-to-end poster.
- [ ] Repeat for Node2 (`TOKEN_MINI_2`), Node3, Node4.
- [ ] Remove `~/poster-spike` on Node1 (user confirms), delete `~/.omm-poster-art-tokens.env` locally once all four are in the keychains (user confirms).
- [ ] Report to the DSHS session.
