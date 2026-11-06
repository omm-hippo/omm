# omm cluster: poster art worker (dshs.dev AI posters drawn on the cluster Macs)

Date: 2026-11-06. Status: design, awaiting approval.
Site-side design (the contract this worker must keep): DSHS_Developer_Platform repo,
`docs/superpowers/specs/2026-11-06-poster-mac-art-design.md` §5-3 and §7. The site side is
already deployed to production (DSHS main `d02748d`).

## Why

dshs.dev's AI poster feature draws its picture with Cloudflare Workers AI, whose free quota
allows about **6 pictures a day for the whole site**. The school has four Mac minis already
joined into an omm cluster. If those Macs draw the pictures, the limit becomes "whatever the
Macs can draw". The school network must stay closed: the Macs open no listening port and no
tunnel; each Mac only makes outbound HTTPS calls to pull work and push the result.

When no Mac is drawing, the site falls back to Workers AI by itself, so the worker can be
turned on late, crash, or be switched off without breaking the site.

## Decisions already made

- **User:** the token is set up **per Mac** (ssh into each Mac and run one command), not
  pushed from the head. The member link signs but does not encrypt, so sending a token over
  it would need new crypto.
- **User:** the cluster's own LLM (exo) gets memory **before** the poster drawer. See
  "Memory gate".
- **User:** no dshs.dev / Supabase address is hard-coded in omm (public code). `enable` takes
  all four values.
- **User + site session, from the measurement below:** the worker runs on **all four Macs**,
  not only on the idle one.
- **Site contract (not ours to change):** claim = `POST {SUPABASE_URL}/rest/v1/rpc/claim_poster_art_job`,
  headers `apikey: <anon>` + `Authorization: Bearer <anon>`, body `{"p_token": "<token>"}`, reply
  `[{"id", "prompt", "width", "height", "job_secret"}]` or `[]` (treat `null` the same). Ping =
  `rpc/poster_art_ping` with the same headers and body `{"p_token": ...}`; it only refreshes the
  Mac's `last_seen_at`: it does **not** extend the lease and does **not** report a cancelled job
  (a cancelled or re-queued job just answers the upload with 404). Jobs are always pictures
  (`kind` is `'art'` only; the claim reply has no `kind`). Other contract points: headers, status codes, lease 180 s,
  ping every 15 s, claim every 3 s, one job per Mac at a time, result body ≤ 4 MB with a
  real `Content-Length` (chunked upload → 413), prompt is untrusted (student text rewritten by
  Gemini) and is only ever data.

## Measured on the real rig (2026-11-06, read-only + a scratch venv on Node1)

- All four Macs are **M4 Pro, 64 GiB** (the site doc said 32, now corrected there).
- Other accounts run their own ML jobs on these Macs and they are not ours to touch:
  Node1/Node3/Node4 GPUs were 76-99 % busy; Node2 runs CPU-only simulations (GPU 3 %).
  Free memory was 25-38 GiB per Mac.
- `mflux 0.24.0` installs and imports on Python 3.15 (the omm pipx venv's Python); it pulls
  `torch` too, the venv is 1.4 GB.
- `FLUX.1-schnell` is **gated** on Hugging Face (needs an HF login on every Mac).
  `FLUX.2-klein-4B` is ungated, Apache-2.0, and supported by mflux (`flux2-klein-4b`).
- klein-4B, `quantize=8`, 1024×768, 4 steps, on Node1 with its GPU 87-96 % busy:
  **30 s to draw** (7.6 s/step), 35.6 s for a fresh process including model load, 99 s the
  first time ever (cold disk). **Peak memory footprint 13.8 GB.** The files mflux needs (`model_index.json`, `scheduler/`,
  `text_encoder/`, `tokenizer/`, `transformer/`, `vae/`) are about 15 GB; the repo also has a
  7.2 GB single-file copy and sample images that are not needed.
  Picture quality is fine. Garbled fake lettering appeared because the test prompt said
  "poster"; real site prompts already carry "no text, no letters, no numbers, no logos".
- The first download stalled for 15 minutes with two `.incomplete` blobs frozen; restarting
  needed the stale `~/.cache/huggingface/hub/.locks/...` files removed.

## Goals

- Each cluster Mac (head or member) can run one background service that pulls poster jobs,
  draws them with mflux, and uploads the image, exactly per the site contract.
- A Mac that cannot take a job right now (not enough memory, model not downloaded yet) is
  invisible to the site: it sends neither claim nor ping.
- The token never appears in any output, log, run log, bug report, or command line.
- `omm cluster status` on a Mac shows whether the service is on, whether the model is
  loaded, and the last job's outcome.

## Non-goals

- Text/layout generation on the Macs (the site's future `kind: "text"`).
- Setting up all four Macs from the head.
- Showing every Mac's poster state on the head's status (each Mac shows its own; see
  "Left to the builder").
- Any admin UI on the site.

## Anti-goals (failure even if it "works")

- Any listening socket, tunnel, pf change, ALF change, or change to the gateway (52440) or
  to exo (reading exo's loopback state is allowed; no write calls).
- Outbound traffic from the running worker to anything except the Supabase URL, the site URL,
  and Hugging Face for the one-time model download. (`enable` itself installs the drawing venv
  from PyPI once; that is an admin action, not the worker. Designer's call.)
- The prompt reaching a shell, or being written to a log.
- Killing, renicing, or starving other accounts' jobs. The worker only ever backs off.
- Non-commercial model weights (FLUX.1-dev, FLUX.2-klein-9B).

## Shape

### Commands

```
omm cluster poster enable [--env-file PATH|-]   # set up and start the poster art worker on this Mac
omm cluster poster disable                      # stop it and remove its token, settings, venv and model
```

- Nested under `omm cluster` (CLI minimalism: no new top-level verb). Refuses with a plain
  error when this Mac is not in a cluster (`cluster/settings.load()` is None), so it never
  runs on an employee PC.
- `enable` takes four values: `SUPABASE_URL`, `SUPABASE_ANON_KEY` (public), `SITE_URL`, and
  this Mac's `TOKEN`. Interactive: prompts for each, the token with hidden input.
  `--env-file PATH` (or `-` for stdin) reads `KEY=VALUE` lines with those four keys instead,
  so a token can be piped over ssh without ever being on a command line or a screen:
  `ssh mac 'omm cluster poster enable --env-file -' < file`. Both URLs must be `https://`.
  The token must match `^[A-Za-z0-9_-]{20,200}$` (the site issues 32-byte base64url).
- `enable` is safe to re-run (replaces the token and settings, reinstalls the agent).
- `omm cluster leave` also runs `disable`.
- The site URLs are **not** hard-coded in omm; they are entered at `enable` (user decision).

### Where things live

- Token: macOS login keychain, generic password, service `omm-cluster-poster`, account
  `worker-token`. Written by feeding `security -i` the `add-generic-password -U ...` line on
  **stdin** (argv would show it in `ps`); read by the worker with
  `security find-generic-password -s ... -a ... -w` (captured stdout, never logged).
- Non-secret settings: `OMM_HOME/cluster/poster/settings.json` (both URLs, the anon key,
  model id + revision).
- Drawing runtime: its own venv `OMM_HOME/cluster/poster/venv` with a pinned
  `mflux==0.24.0` (only mflux is pinned, its own bounds pick torch etc.; designer's call), created by `enable` from omm's own Python (`sys._base_executable` or
  equivalent). Not a pipx inject and not an omm extra: torch + mflux stay out of omm's own
  venv, and an omm reinstall/update cannot drop them.
- Model files: `HF_HOME=OMM_HOME/cluster/poster/hf`, so `disable` removes everything it
  added and nothing in the user's own `~/.cache/huggingface` is touched. Model
  `black-forest-labs/FLUX.2-klein-4B` pinned to revision
  `e7b7dc27f91deacad38e78976d1f2b499d76a294`, mflux model name `flux2-klein-4b`,
  `quantize=8`, 4 steps. Downloaded with `allow_patterns` limited to the six entries above, then
  loaded from that local snapshot path (`model_path=`) with `HF_HUB_OFFLINE=1`, so the drawer
  itself never goes online and the pinned revision is what runs.
- State for status: `OMM_HOME/cluster/poster/state.json` written by the worker (phase,
  model loaded or not, last job id / outcome / seconds / time). No token, no prompt.
- Service: LaunchAgent `com.omm.cluster-poster`, hidden subcommand `_cluster-poster-run`,
  wrapper shim `OMM_HOME/bin/omm-cluster-poster`, log `OMM_HOME/logs/cluster-poster.log`,
  `KeepAlive`, `ThrottleInterval` — a copy of `member_service.py`'s pattern.

### Two processes

1. **Worker** (omm's Python, the LaunchAgent): HTTP, keychain, memory gate, timing, state
   file. Imports nothing heavy.
2. **Drawer** (the poster venv's Python, child of the worker): a small standalone script
   shipped inside the omm package (stdlib + mflux + Pillow only), run as
   `<venv>/bin/python -I <script path>`. It loads the model once and then reads one JSON
   request per line on stdin (`prompt`, `width`, `height`, `seed`, `out` path) and answers
   one JSON line on stdout (`ok`, `seconds` or `error`). It writes a JPEG (quality 90) to
   `out`. The prompt only ever travels inside that JSON value.
   - Unloading the model = ending the drawer process, so the memory really goes back.
   - The drawer is started for the first job and stopped after **15 minutes** with no job,
     on any drawer error, or when a job runs out of time.

### Worker loop

```
start: read token (keychain) + settings; missing -> log once, sleep 60 s, retry (no claim)
if model not downloaded: download it (snapshot, pinned revision, retries; a download
    that stops growing for 5 min is restarted with the stale locks cleared); phase
    "downloading", no claim, no ping
loop:
  memory gate (below) fails  -> phase "waiting for memory", sleep 3 s, no claim/ping
  claim (POST rpc/claim_poster_art_job {"p_token"})
      network error / 5xx    -> backoff 3,6,12,...,60 s
      [] or null             -> sleep 3 s
      [job]                  -> run_job(job), then claim again at once
  drawer idle 15 min         -> stop drawer
```

`run_job`:

1. `deadline = (local time when the claim reply arrived) + 180 s` (the server set its lease
   before replying, so local time errs on the safe side). Start a ping thread (`rpc/poster_art_ping` every 15 s)
   that runs until the job ends.
2. Size: take the job's width/height, floor each to a multiple of 16 (mflux FLUX.2 needs
   multiples of 16 — builder verifies; use 64 if mflux demands it), and if
   `width*height > 786432` scale both down by the same factor first. Never round up.
3. Start the drawer if not running, send the request with a random seed. If the drawing is
   not back by `deadline - 30 s`, kill the drawer and send `fail` (`reason: "timeout"`).
4. Read the JPEG bytes; if over 4 MB re-encode at a lower quality once, else `fail`.
5. Upload: `POST {SITE_URL}/api/poster-art/jobs/<id>/result`, `Authorization: Bearer
   <token>`, `X-Job-Secret`, `Content-Type: image/jpeg`, body sent as one `bytes` object so
   `Content-Length` is set.
   - 200 → done. 404 / 413 / 415 → drop, no retry, no fail.
   - 502 → wait 5 s, retry; wait 15 s, retry; still failing → `fail`. A retry that would
     start after the deadline is skipped and goes straight to `fail`.
   - Network error or any other status: handled like 502 (builder's call, contract is
     silent).
6. `fail` = `POST .../jobs/<id>/fail {"reason": "..."}`, reason ≤ 200 chars, a fixed short
   phrase (timeout / drawer error / upload failed), never the prompt or an exception dump.
7. Log one line: job id, seconds, outcome. Update `state.json`.

### Memory gate

- Drawer running → the model's memory is already ours: pass, unless macOS memory pressure
  is critical, in which case stop the drawer and fail the gate.
- Drawer not running → pass only if `hardware.calculate_memory_budget(...).model_budget_gb`
  (the same live-free-RAM budget the memory guard uses) is ≥ **16 GB** (13.8 GB measured
  peak + margin). Checked every loop, so the Mac reappears to the site on its own when
  another account's job ends.
- **The cluster LLM comes first (user decision).** Each loop the worker reads exo's state on
  loopback (`GET /state`, read-only, the same data `exo_client` already parses). If exo is
  bringing up a model instance that includes this Mac (an instance for this node that is not
  running yet, or a placement refused for memory), then:
  - an idle drawer is stopped at once (not after 15 min);
  - the gate fails (no claim, no ping) until exo's instance is running or gone;
  - a drawing in progress finishes its one job first, then the drawer stops.
  Exact detection from exo's state is the builder's, verified against `tests/fixtures/exo/`
  and the live rig. exo unreachable (stopped, restarting) = no exo demand, gate as above.
- The worker never asks the memory guard to unload anything and never writes to exo.

### Power, network, security

- Idle sleep is already off on every cluster Mac (`power.keep_awake` in init/join). Nothing
  new.
- No listening port. Outbound only to the two configured https URLs + Hugging Face during
  download. pf, ALF, the gateway and exo are untouched.
- `requests` with default TLS verification. Only the configured hosts are ever called; job
  ids are validated as UUIDs before being put in a URL path.
- Token handling: in memory in the worker only; passed to `requests` headers; never in
  `state.json`, the log, the run log (`runlog.py`), exceptions shown to the user, or
  `omm bug-report` (nothing writes it anywhere that collects).

### Keeping up with omm updates

- After `omm update` on the head (`_after_update_on_cluster_head`) and after a member's link
  follows the head's commit, restart `com.omm.cluster-poster` if installed so it runs the new
  code. A job in progress may be lost; the site re-queues it after the lease.

### Status

`omm cluster status` (and `--json`) gains one block for **this** Mac:

```
Poster art: on — model loaded, last job 3f2a… done in 32 s (4 min ago)
Poster art: on — waiting for memory (12 GiB free, needs 16)
Poster art: on — downloading the model
Poster art: off
```

The token and the URLs' secrets never appear (the anon key is not shown either).

## Docs and tests

- `docs/commands.json` regenerated, README `## Usage` lines for both commands,
  CLAUDE.md architecture paragraph ("Poster art worker"), `scripts/check_docs_sync.py`.
- Tests with a stdlib fake server for the four endpoints (status codes scripted per test) and
  a fake drawer script: size flooring, lease timeout → fail, 502 retry schedule, 404/413/415
  drop, `[]` / null / network backoff, memory gate on/off (no request sent when it fails),
  `Content-Length` present, token absent from every log record (`caplog`) and from
  `state.json`, `enable` refusing on a non-cluster Mac and on http:// URLs, env-file parsing.
- Real-rig check before calling it done: install on the four Macs (user confirms before each
  remote command), watch the site's `poster-art-worker.mts list` "last seen" move, and have the
  site session confirm one picture end to end.

## Left to the builder

- Module names and splitting inside `src/omm/cluster/`.
- Exact backoff, how memory pressure is read, how the download-stall watchdog is done.
- Whether `enable` waits for the venv build (it should show progress) and whether the model
  download happens in `enable` or in the worker (the design above puts it in the worker so
  `enable` over ssh returns quickly).
- Whether to also save a pre-quantized model with `mflux-save` to cut load time / peak memory
  (not measured).
- Adding the poster state to the member report so the head's status lists every Mac (nice to
  have; it touches the signed member-link body).

## Known risks to verify on the rig

- Writing to the login keychain over ssh may fail with "User interaction is not allowed" if
  that keychain is locked in the ssh session. Designer's call: **no file fallback** for the
  token. `enable` stops with a message to run `security unlock-keychain` first (asks the login
  password in the ssh terminal) or to run `enable` once in the Mac's own Terminal.
- The worker in the GUI LaunchAgent must read the item without a prompt (item created by
  `/usr/bin/security`, read by the same binary).
