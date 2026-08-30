# Anonymous Usage Statistics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship an opt-in daily anonymous usage-stats batch (`omm/usage.py`) to a new Cloudflare-Worker `/usage` route and RTDB `usage` node, consolidate the three outbound-data settings under `omm setting upload {benchmark,usage,crash}`, and add a consent step to `omm setup`.

**Architecture:** `omm/usage.py` mirrors `omm/error_report.py` — atomic locked pending queue, `flush_pending()` on a later invocation, PoW-signed POST through the existing gateway. `cli.main()` records one row per run (no network); the root callback flushes. Consent is off by default and only an explicit interactive "yes" in `omm setup` enables it.

**Tech Stack:** Python 3.10+ (stdlib `json`/`uuid`/`platform` + `filelock` via `omm.atomic`), Typer CLI, `questionary`, Cloudflare Worker (TypeScript, vitest), Firebase RTDB rules.

**Spec:** `docs/superpowers/specs/2026-08-30-usage-stats-design.md`

## Global Constraints

- Python 3.10+, CI pins 3.11. No new runtime dependencies.
- Opt-in, off by default. `usage_stats_policy` is `None` (⇒ never) or `"enabled"`. No `"ask"`.
- `omm/usage.py` never raises to its caller. Every public function wraps its body in `try/except Exception` and returns a safe default. No network except inside `flush_pending()`.
- Never collected/sent: model names, search queries, repo ids, file paths, IP, hostname, username, exception messages, tracebacks. Hardware is bucketed strings, never exact numbers. GPU is a vendor class, never the model string.
- `_post()` refuses any endpoint other than `config.USAGE_GATEWAY_ENDPOINT`.
- Read `config.OMM_HOME` / config values at call time (module-attribute access), never bind at import — the `isolated_omm_home` fixture monkeypatches `config.*`.
- Per CLAUDE.md: when consolidating commands, **fully delete** the old ones — no back-compat aliases. Underlying config keys are unchanged so saved user choices survive.
- cf-worker: `cd cf-worker && npm ci && npm test && npx tsc -p tsconfig.json` must pass. Firebase rules: the `emulators:exec` command in CLAUDE.md must pass.
- Commit after every task. `git add` only the exact files the task changed — never `-A`/`.`. Re-check `git log -5` / `git status` right before committing (this checkout has concurrent sessions; commits from other work interleave — that is expected, not a conflict).
- Target branch `beta`. Do not push.

---

## File Structure

- **Create `src/omm/usage.py`** — the whole client: client-id-aware snapshot, pending queue, aggregation, `flush_pending`, PoW POST. Mirrors `error_report.py`. ~260 lines.
- **Modify `src/omm/config.py`** — `CLIENT_ID_PATH`, `client_id()`, `USAGE_GATEWAY_ENDPOINT`, `DEFAULT_CONFIG["usage_stats_policy"]`.
- **Modify `src/omm/cli.py`** — `main()` records a run; root callback flushes; `omm setting upload` becomes a group; delete `configure_upload` flat + `configure_error_reports`.
- **Modify `src/omm/onboarding.py`** — `run_data_sharing_step`, called from `run_wizard`.
- **Modify `cf-worker/src/{index,validate,rtdb}.ts`** — `/usage` route, `validateUsageEvent`, node union.
- **Modify `database.rules.json`** + **`scripts/test_firebase_rules.mjs`** — `usage` node rules + tests.
- **Create `PRIVACY.md`** (repo root). **Rename `docs/error-reports.md` → `docs/crash-reports.md`**. **Modify `README`** (+ `CLAUDE.md` architecture note).
- **Create** `tests/test_usage.py`, `tests/test_cli_setting_upload.py`, `cf-worker/test/usage.test.ts`. **Modify** `tests/test_onboarding*.py`.

---

### Task 1: client id + endpoint + config key

**Files:**
- Modify: `src/omm/config.py`
- Test: `tests/test_config_client_id.py` (create)

**Interfaces:**
- Produces:
  - `config.CLIENT_ID_PATH: Path` = `OMM_HOME / "client-id"`
  - `config.client_id() -> str` — stable uuid4 hex; created on first read; ephemeral fresh id (no raise) on I/O failure
  - `config.USAGE_GATEWAY_ENDPOINT: str`
  - `DEFAULT_CONFIG["usage_stats_policy"] = None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_client_id.py
import re
from omm import config


def test_client_id_stable_and_hex(isolated_omm_home):
    a = config.client_id()
    b = config.client_id()
    assert a == b
    assert re.fullmatch(r"[0-9a-f]{32}", a)
    assert config.CLIENT_ID_PATH.exists()


def test_client_id_regenerates_after_delete(isolated_omm_home):
    a = config.client_id()
    config.CLIENT_ID_PATH.unlink()
    b = config.client_id()
    assert a != b


def test_client_id_ephemeral_when_unwritable(isolated_omm_home, monkeypatch):
    def boom(*a, **k):
        raise OSError("nope")
    monkeypatch.setattr(config, "atomic_write_text", boom)
    config.CLIENT_ID_PATH.unlink(missing_ok=True)
    got = config.client_id()  # must not raise
    assert re.fullmatch(r"[0-9a-f]{32}", got)


def test_usage_policy_default_is_none(isolated_omm_home):
    assert config.load_config().get("usage_stats_policy") is None
```

- [ ] **Step 2: Run — expect FAIL** (`AttributeError: client_id`)

Run: `python -m pytest tests/test_config_client_id.py -q`

- [ ] **Step 3: Implement**

In `src/omm/config.py`, add near the other path constants:

```python
CLIENT_ID_PATH = OMM_HOME / "client-id"
```

Add the endpoint near `TELEMETRY_GATEWAY_ENDPOINT` / `ERROR_REPORTS_ENDPOINT`:

```python
# Anonymous usage-stats batch destination. Its own Worker route + RTDB node
# (`/usage`), gated by the same proof-of-work envelope as telemetry. Never
# derived from telemetry_endpoint: usage stats always go upstream, and the
# stream is opt-in and off by default (see omm.usage, DEFAULT_CONFIG
# below, and PRIVACY.md).
USAGE_GATEWAY_ENDPOINT = "https://omm-telemetry-gateway.seong381400.workers.dev/usage"
```

Add to `DEFAULT_CONFIG` (next to `error_report_send_policy`):

```python
    # Anonymous usage stats. Opt-in, off by default: None means "never",
    # "enabled" means the daily batch runs. Stored None (not "never") so an
    # unset state stays distinguishable from an explicit opt-out, matching
    # error_report_send_policy.
    "usage_stats_policy": None,
```

Add the function (after `ensure_omm_home` / near `load_config`):

```python
import uuid  # add to the stdlib imports at the top

def client_id() -> str:
    """Stable random per-install identifier (uuid4 hex), created on first
    read. Its own file, never config.json — config gets copied between
    machines and this must not travel with it. Best-effort: any I/O
    failure returns a fresh ephemeral id rather than raising, so callers
    (only omm.usage) never have to handle an exception."""
    try:
        if CLIENT_ID_PATH.exists():
            existing = CLIENT_ID_PATH.read_text(encoding="utf-8").strip()
            if len(existing) == 32 and all(c in "0123456789abcdef" for c in existing):
                return existing
        fresh = uuid.uuid4().hex
        CLIENT_ID_PATH.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(CLIENT_ID_PATH, fresh + "\n")
        return fresh
    except OSError:
        return uuid.uuid4().hex
```

- [ ] **Step 4: Run — expect PASS**

Run: `python -m pytest tests/test_config_client_id.py -q`

- [ ] **Step 5: Regression**

Run: `python -m pytest tests/test_config_omm_home_env.py tests/ -q -k config`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/omm/config.py tests/test_config_client_id.py
git commit -m "feat: add per-install client id and usage-stats config key"
```

---

### Task 2: `usage.py` — collection (no network)

**Files:**
- Create: `src/omm/usage.py`
- Test: `tests/test_usage.py`

**Interfaces:**
- Consumes: `config.client_id`, `config.OMM_HOME`, `config.load_config`, `package_metadata`, `hardware`.
- Produces:
  - `usage.record_run(subcommand: str, outcome: str, error_class: str | None) -> None` — no-op unless policy `"enabled"`; else append one row to `~/.omm/usage-pending.json`. Swallows all errors.
  - `usage.build_payload() -> dict` — snapshot fields + `commands` / `errors` tallies from pending rows.
  - `usage.pending_count() -> int`
  - `usage.discard_pending() -> int`
  - `usage.policy(config_data=None) -> str` — `"enabled"` or `"never"`.
  - Module constants: `SCHEMA_VERSION = 1`, `_PENDING_MAX = 5000`, `_TALLY_MAX_KEYS = 100`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_usage.py
import json
from omm import config, usage


def _enable(monkeypatch):
    monkeypatch.setattr(config, "load_config", lambda: {"usage_stats_policy": "enabled"})


def test_record_run_noop_when_unset(isolated_omm_home):
    usage.record_run("install", "ok", None)
    assert usage.pending_count() == 0


def test_record_run_appends_when_enabled(isolated_omm_home, monkeypatch):
    _enable(monkeypatch)
    usage.record_run("install", "ok", None)
    usage.record_run("install", "failed", "DownloadError")
    assert usage.pending_count() == 2


def test_build_payload_shape(isolated_omm_home, monkeypatch):
    _enable(monkeypatch)
    usage.record_run("install", "ok", None)
    usage.record_run("install", "failed", "DownloadError")
    usage.record_run("search", "ok", None)
    p = usage.build_payload()
    for key in ("schema_version", "client_id", "client_version", "install_source",
                "os_name", "cpu_arch", "ram_gb_bucket", "vram_gb_bucket",
                "gpu_vendor", "recorded_at", "update_channel"):
        assert key in p, key
    assert p["schema_version"] == 1
    assert isinstance(p["ram_gb_bucket"], str)  # bucket, not a number
    assert p["commands"]["install ok"] == 1
    assert p["commands"]["install failed"] == 1
    assert p["commands"]["search ok"] == 1
    assert p["errors"]["install DownloadError"] == 1


def test_error_class_never_leaks_message(isolated_omm_home, monkeypatch):
    _enable(monkeypatch)
    usage.record_run("install", "failed", type(RuntimeError("secret /home/x")).__name__)
    text = json.dumps(usage.build_payload())
    assert "secret" not in text and "/home/x" not in text


def test_tally_capped(isolated_omm_home, monkeypatch):
    _enable(monkeypatch)
    for i in range(250):
        usage.record_run(f"cmd{i}", "ok", None)
    p = usage.build_payload()
    assert len(p["commands"]) <= usage._TALLY_MAX_KEYS


def test_discard_pending(isolated_omm_home, monkeypatch):
    _enable(monkeypatch)
    usage.record_run("install", "ok", None)
    assert usage.discard_pending() == 1
    assert usage.pending_count() == 0
```

- [ ] **Step 2: Run — expect FAIL** (`ImportError: omm.usage`)

- [ ] **Step 3: Implement `src/omm/usage.py`**

```python
"""Strictly opt-in, best-effort anonymous usage statistics. Never raises.

One coarse snapshot per day (install identity, version, bucketed hardware)
plus a tally of which commands ran and whether they succeeded. Built from
purpose-made records only — never a dump of the local run log. Sent through
the same proof-of-work gateway as telemetry, to a separate write-only RTDB
node (`/usage`). Off unless `usage_stats_policy == "enabled"`.

Mirrors omm.error_report on purpose: same pending-queue / flush_pending /
attempt-log shape. See docs/../specs/2026-08-30-usage-stats-design.md and
PRIVACY.md.
"""

from __future__ import annotations

import json
import platform
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from filelock import Timeout as FileLockTimeout

from omm import config
from omm.atomic import atomic_write_text, locked
from omm.config import load_config

SCHEMA_VERSION = 1
_PENDING_MAX = 5000
_TALLY_MAX_KEYS = 100
_FLUSH_INTERVAL_S = 24 * 3600
_MAX_LOG_LINES = 500
_KEY_RE_OUTCOMES = ("ok", "failed", "usage-error", "interrupted")


def _pending_path():
    return config.OMM_HOME / "usage-pending.json"


def _state_path():
    return config.OMM_HOME / "usage-state.json"


def _backoff_path():
    return config.OMM_HOME / "usage-backoff.json"


def _log_path():
    return config.OMM_HOME / "usage.log"


def policy(config_data: dict[str, Any] | None = None) -> str:
    data = config_data if config_data is not None else load_config()
    return "enabled" if data.get("usage_stats_policy") == "enabled" else "never"


# --- collection ----------------------------------------------------------


def _read_pending() -> list[dict]:
    try:
        with locked(_pending_path(), timeout=10):
            path = _pending_path()
            if not path.exists():
                return []
            loaded = json.loads(path.read_text(encoding="utf-8"))
        return [r for r in loaded if isinstance(r, dict)][-_PENDING_MAX:] if isinstance(loaded, list) else []
    except (OSError, ValueError, FileLockTimeout):
        return []


def record_run(subcommand: str, outcome: str, error_class: str | None) -> None:
    """Append one row for this invocation. No-op unless opted in. No
    network. Swallows all errors."""
    try:
        if policy() != "enabled":
            return
        row = {"c": str(subcommand)[:40], "o": str(outcome)[:20]}
        if error_class and outcome == "failed":
            row["e"] = str(error_class)[:60]
        with locked(_pending_path(), timeout=10):
            path = _pending_path()
            rows = []
            if path.exists():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(loaded, list):
                        rows = [r for r in loaded if isinstance(r, dict)]
                except ValueError:
                    rows = []
            rows.append(row)
            atomic_write_text(path, json.dumps(rows[-_PENDING_MAX:]))
    except (OSError, FileLockTimeout):
        pass


def pending_count() -> int:
    return len(_read_pending())


def discard_pending() -> int:
    try:
        n = pending_count()
        with locked(_pending_path(), timeout=10):
            _pending_path().unlink(missing_ok=True)
        return n
    except (OSError, FileLockTimeout):
        return 0


# --- snapshot + aggregation --------------------------------------------


_RAM_BUCKETS = [(8, "<8"), (16, "8-16"), (32, "16-32"), (64, "32-64"), (128, "64-128")]
_VRAM_BUCKETS = [(4, "<4"), (8, "4-8"), (12, "8-12"), (16, "12-16"), (24, "16-24")]


def _bucket(value: float | None, table, top: str) -> str:
    if value is None:
        return "none"
    for edge, label in table:
        if value < edge:
            return label
    return top


def _gpu_vendor(gpu_name: str | None) -> str:
    if not gpu_name:
        return "none"
    low = gpu_name.lower()
    if "apple" in low or "m1" in low or "m2" in low or "m3" in low or "m4" in low:
        return "apple"
    if "nvidia" in low or "geforce" in low or "rtx" in low or "gtx" in low or "quadro" in low or "tesla" in low:
        return "nvidia"
    if "amd" in low or "radeon" in low or "rx " in low:
        return "amd"
    if "intel" in low or "arc" in low or "iris" in low or "uhd" in low:
        return "intel"
    return "other"


def _snapshot() -> dict:
    from omm import package_metadata

    try:
        source = package_metadata.install_source().name.lower()
    except Exception:
        source = "unknown"
    try:
        from omm import hardware

        hw = hardware.detect_hardware()
        ram = hw.ram_total_gb
        vram = hw.vram_total_gb
        gpu = hw.gpu_name
        arch = hw.cpu_arch or platform.machine()
    except Exception:
        ram = vram = gpu = None
        arch = platform.machine()
    cfg = load_config()
    return {
        "schema_version": SCHEMA_VERSION,
        "client_id": config.client_id(),
        "client_version": (package_metadata.version() if hasattr(package_metadata, "version") else None) or "unknown",
        "install_source": source,
        "os_name": platform.system() or "unknown",
        "os_version": (platform.release() or "")[:64],
        "cpu_arch": (arch or "unknown")[:64],
        "ram_gb_bucket": _bucket(ram, _RAM_BUCKETS, "128+"),
        "vram_gb_bucket": _bucket(vram, _VRAM_BUCKETS, "24+"),
        "gpu_vendor": _gpu_vendor(gpu),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "update_channel": "beta" if cfg.get("update_channel") == "beta" else "stable",
    }


def _aggregate(rows: list[dict]) -> tuple[dict, dict]:
    commands: Counter = Counter()
    errors: Counter = Counter()
    for r in rows:
        cmd = r.get("c", "unknown")
        out = r.get("o", "unknown")
        commands[f"{cmd} {out}"] += 1
        if r.get("e"):
            errors[f"{cmd} {r['e']}"] += 1
    return (
        dict(sorted(commands.items())[:_TALLY_MAX_KEYS]),
        dict(sorted(errors.items())[:_TALLY_MAX_KEYS]),
    )


def build_payload() -> dict:
    """Snapshot + aggregated tally of pending rows. Used by the sender and
    by `omm setting upload usage`'s dry-run preview."""
    payload = _snapshot()
    commands, errors = _aggregate(_read_pending())
    payload["commands"] = commands
    if errors:
        payload["errors"] = errors
    return payload
```

- [ ] **Step 4: Run — expect PASS**

Run: `python -m pytest tests/test_usage.py -q`

Note: adjust `hardware.detect_hardware` / `package_metadata.version` names in Step 3 to the real ones — `grep -n "def detect\|def version\|def install_source" src/omm/hardware.py src/omm/package_metadata.py` first, then fix the calls. The test only asserts payload shape, so any working hardware accessor is fine.

- [ ] **Step 5: Commit**

```bash
git add src/omm/usage.py tests/test_usage.py
git commit -m "feat: usage-stats collection — pending queue, snapshot, aggregation"
```

---

### Task 3: `usage.py` — flush + PoW POST + backoff

**Files:**
- Modify: `src/omm/usage.py`
- Test: `tests/test_usage.py`

**Interfaces:**
- Produces:
  - `usage.flush_pending(force: bool = False) -> bool` — if opted in and ≥ `_FLUSH_INTERVAL_S` since the last success (`usage-state.json`) and not in backoff: build payload, PoW-POST to `config.USAGE_GATEWAY_ENDPOINT`; on 2xx clear pending + stamp state; returns whether it sent. Swallows all errors.
  - `usage._post(payload: dict) -> bool`
  - `usage.log_attempt(outcome: str, detail: str = "") -> None`

- [ ] **Step 1: Write the failing test**

```python
class _Resp:
    def __init__(self, code): self.status_code = code; self.text = ""


def test_flush_noop_before_interval(isolated_omm_home, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(usage, "_state_path", lambda: config.OMM_HOME / "usage-state.json")
    # a fresh state stamp means "just sent"
    (config.OMM_HOME).mkdir(parents=True, exist_ok=True)
    (config.OMM_HOME / "usage-state.json").write_text(
        json.dumps({"last_sent": time.time()})
    )
    usage.record_run("install", "ok", None)
    calls = []
    monkeypatch.setattr(usage, "_post", lambda p: calls.append(p) or True)
    assert usage.flush_pending() is False
    assert calls == []


def test_flush_sends_and_clears_on_2xx(isolated_omm_home, monkeypatch):
    _enable(monkeypatch)
    usage.record_run("install", "ok", None)
    sent = []
    monkeypatch.setattr(usage, "_post", lambda p: sent.append(p) or True)
    assert usage.flush_pending(force=True) is True
    assert sent and sent[0]["commands"]["install ok"] == 1
    assert usage.pending_count() == 0


def test_flush_keeps_pending_on_failure(isolated_omm_home, monkeypatch):
    _enable(monkeypatch)
    usage.record_run("install", "ok", None)
    monkeypatch.setattr(usage, "_post", lambda p: False)
    assert usage.flush_pending(force=True) is False
    assert usage.pending_count() == 1


def test_post_refuses_non_gateway_endpoint(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(config, "load_config",
                        lambda: {"usage_stats_policy": "enabled"})
    monkeypatch.setattr(config, "USAGE_GATEWAY_ENDPOINT", "https://real.example/usage")
    import omm.telemetry as tele
    monkeypatch.setattr(tele, "_solve_proof_of_work", lambda j: (1, 1))
    posted = []
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: posted.append(1) or _Resp(200))
    # _post uses config.USAGE_GATEWAY_ENDPOINT as the only allowed target;
    # here that IS the patched value, so this asserts the happy path instead.
    # For the refusal path, call _post with the check inverted:
    monkeypatch.setattr(config, "USAGE_GATEWAY_ENDPOINT", "https://real.example/usage")
    # (kept simple — the refusal is a one-line guard; see Step 3)
```

Simplify `test_post_refuses_non_gateway_endpoint` to: monkeypatch nothing, call an internal `usage._post_to(endpoint, payload)` helper with a bad endpoint and assert it returns `False` without importing `requests`. Restructure Step 3 so `_post` delegates to `_post_to(config.USAGE_GATEWAY_ENDPOINT, payload)` and `_post_to` has the `endpoint != config.USAGE_GATEWAY_ENDPOINT → return False` guard.

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement** — append to `src/omm/usage.py`:

```python
_POW_SLICE = 300


def log_attempt(outcome: str, detail: str = "") -> None:
    try:
        with locked(_log_path(), timeout=10):
            path = _log_path()
            lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
            lines.append(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "outcome": outcome, "detail": detail[:_POW_SLICE],
            }))
            atomic_write_text(path, "\n".join(lines[-_MAX_LOG_LINES:]) + "\n")
    except (OSError, FileLockTimeout):
        pass


def _read_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _stamp_state() -> None:
    try:
        with locked(_state_path(), timeout=10):
            atomic_write_text(_state_path(), json.dumps({"last_sent": time.time()}))
    except (OSError, FileLockTimeout):
        pass


def _backoff_active() -> bool:
    try:
        until = _read_json(_backoff_path()).get("until", 0)
        return time.time() < float(until)
    except Exception:
        return False


def _read_json(path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _set_backoff(seconds: float) -> None:
    try:
        with locked(_backoff_path(), timeout=10):
            atomic_write_text(_backoff_path(), json.dumps({"until": time.time() + seconds}))
    except (OSError, FileLockTimeout):
        pass


def _clear_backoff() -> None:
    try:
        with locked(_backoff_path(), timeout=10):
            _backoff_path().unlink(missing_ok=True)
    except (OSError, FileLockTimeout):
        pass


def _post_to(endpoint: str, payload: dict) -> bool:
    if endpoint != config.USAGE_GATEWAY_ENDPOINT:
        log_attempt("skipped_bad_endpoint")
        return False
    import requests
    from omm.telemetry import _solve_proof_of_work

    wire = {k: v for k, v in payload.items() if v is not None}
    event_json = json.dumps(wire, sort_keys=True, separators=(",", ":"))
    timestamp_ms, nonce = _solve_proof_of_work(event_json)
    try:
        resp = requests.post(
            endpoint,
            json={"event_json": event_json, "timestamp": timestamp_ms, "nonce": nonce},
            timeout=10,
        )
    except requests.RequestException as e:
        log_attempt("send_failed_network", str(e))
        return False
    if 200 <= resp.status_code < 300:
        log_attempt("sent_ok")
        return True
    log_attempt(f"send_failed_http_{resp.status_code}", str(getattr(resp, "text", ""))[:_POW_SLICE])
    return False


def _post(payload: dict) -> bool:
    return _post_to(config.USAGE_GATEWAY_ENDPOINT, payload)


def flush_pending(force: bool = False) -> bool:
    """Send one batch if opted in, past the 24h interval, and not backing
    off. Clears pending + stamps state on success. Swallows all errors."""
    try:
        if policy() != "enabled":
            return False
        rows = _read_pending()
        if not rows and not force:
            return False
        if not force:
            if _backoff_active():
                return False
            last = float(_read_state().get("last_sent", 0))
            if time.time() - last < _FLUSH_INTERVAL_S:
                return False
        if _post(build_payload()):
            discard_pending()
            _stamp_state()
            _clear_backoff()
            return True
        _set_backoff(6 * 3600)
        return False
    except Exception:
        return False
```

- [ ] **Step 4: Run — expect PASS**

Run: `python -m pytest tests/test_usage.py -q`

- [ ] **Step 5: Commit**

```bash
git add src/omm/usage.py tests/test_usage.py
git commit -m "feat: usage-stats daily flush via the PoW gateway"
```

---

### Task 4: wire into `cli.py`

**Files:**
- Modify: `src/omm/cli.py` (`from omm import ... usage`; `main()` `finally`; root callback flush block ~line 624-639)
- Test: `tests/test_usage.py`

- [ ] **Step 1: Write the failing test**

```python
def test_cli_main_records_usage_run(isolated_omm_home, monkeypatch):
    from omm import cli
    monkeypatch.setattr(config, "load_config", lambda: {"usage_stats_policy": "enabled", "theme": "dark"})
    monkeypatch.setattr("sys.argv", ["omm", "--version"])
    try:
        cli.main()
    except SystemExit:
        pass
    rows = usage._read_pending()
    assert rows and rows[0]["o"] == "ok"
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement**

Add `usage` to the `from omm import (...)` block in `cli.py`.

In `main()` (already wraps `app()` from the run-log task), track the exception class and record after `runlog.finish`:

```python
    runlog.start(sys.argv[1:])
    exit_code, outcome, exc_name = 0, "ok", None
    try:
        app()
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        outcome = "ok" if exit_code == 0 else ("usage-error" if exit_code == 2 else "failed")
        raise
    except KeyboardInterrupt:
        exit_code, outcome = 130, "interrupted"
        raise
    # ... existing InsufficientDiskSpaceError / PermissionError / OSError branches,
    #     each already sets exit_code, outcome = 1, "failed"; add nothing ...
    except Exception as e:
        exit_code, outcome, exc_name = 1, "failed", type(e).__name__
        _queue_crash_report(e)
        raise
    finally:
        runlog.finish(exit_code, outcome)
        try:
            usage.record_run(runlog.subcommand_of(sys.argv[1:]), outcome, exc_name)
        except Exception:
            pass
```

In the root callback, in the `if ctx.invoked_subcommand != "setting":` block next to the telemetry / error-report flushes:

```python
        try:
            usage.flush_pending()
        except Exception:
            pass
```

- [ ] **Step 4: Run — expect PASS**

Run: `python -m pytest tests/test_usage.py tests/test_runlog.py -q`

- [ ] **Step 5: Regression**

Run: `python -m pytest tests/test_cli_main_entrypoint.py tests/test_cli_error_reports.py -q`

- [ ] **Step 6: Commit**

```bash
git add src/omm/cli.py tests/test_usage.py
git commit -m "feat: record a usage row per run and flush the batch opportunistically"
```

---

### Task 5: `omm setting upload` command group

**Files:**
- Modify: `src/omm/cli.py`
- Test: `tests/test_cli_setting_upload.py` (create); update any test that invoked `omm setting upload` / `omm setting error-reports`

**Interfaces:**
- `omm setting upload` (bare) → prints benchmark / usage / crash policies.
- `omm setting upload benchmark --enable/--disable/--ask` → `telemetry_send_policy` (old `configure_upload` body).
- `omm setting upload crash --enable/--disable/--ask` → `error_report_send_policy` (old `configure_error_reports` body).
- `omm setting upload usage --enable/--disable/--reset-id` → `usage_stats_policy`; bare prints policy + `client_id()` + `build_payload()`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_setting_upload.py
from typer.testing import CliRunner
from omm import cli, config

runner = CliRunner()


def test_upload_bare_lists_three_policies(isolated_omm_home):
    r = runner.invoke(cli.app, ["setting", "upload"])
    assert r.exit_code == 0
    assert "benchmark" in r.output.lower()
    assert "usage" in r.output.lower()
    assert "crash" in r.output.lower()


def test_upload_usage_enable_disable(isolated_omm_home):
    assert runner.invoke(cli.app, ["setting", "upload", "usage", "--enable"]).exit_code == 0
    assert config.load_config()["usage_stats_policy"] == "enabled"
    assert runner.invoke(cli.app, ["setting", "upload", "usage", "--disable"]).exit_code == 0
    assert config.load_config()["usage_stats_policy"] is None


def test_upload_usage_bare_shows_dry_run_payload(isolated_omm_home):
    runner.invoke(cli.app, ["setting", "upload", "usage", "--enable"])
    r = runner.invoke(cli.app, ["setting", "upload", "usage"])
    assert r.exit_code == 0
    assert "client_id" in r.output or "client id" in r.output.lower()
    assert "ram_gb_bucket" in r.output  # the actual payload is shown


def test_upload_usage_reset_id_changes_it(isolated_omm_home):
    first = config.client_id()
    runner.invoke(cli.app, ["setting", "upload", "usage", "--reset-id"])
    assert config.client_id() != first


def test_old_error_reports_command_is_gone(isolated_omm_home):
    r = runner.invoke(cli.app, ["setting", "error-reports", "--disable"])
    assert r.exit_code != 0


def test_upload_crash_still_works(isolated_omm_home):
    r = runner.invoke(cli.app, ["setting", "upload", "crash", "--disable"])
    assert r.exit_code == 0
    assert config.load_config()["error_report_send_policy"] == "never"
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement**

- Add near the top-level Typer setup (`setting_app = typer.Typer(...)`):

```python
upload_app = typer.Typer(name="upload", help="Choose what anonymous data omm may send: benchmark results, usage stats, crash reports. Each is off/ask by default.")
setting_app.add_typer(upload_app)
```

- Move the **body** of the current `configure_upload` into `@upload_app.command(name="benchmark")` (rename function `configure_upload_benchmark`), and the body of `configure_error_reports` into `@upload_app.command(name="crash")` (`configure_upload_crash`). Delete the two old `@setting_app.command(...)` decorators/functions entirely.

- Add the bare-group callback:

```python
@upload_app.callback(invoke_without_command=True)
def upload_group(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    cfg = load_config()
    table = _table(title="Outbound data", show_header=True)
    table.add_column("Channel", style="label")
    table.add_column("Policy")
    table.add_row("benchmark", cfg.get("telemetry_send_policy", "ask"))
    table.add_row("usage", "enabled" if cfg.get("usage_stats_policy") == "enabled" else "off (default)")
    table.add_row("crash", cfg.get("error_report_send_policy") or "off (default)")
    console.print(table)
    console.print("[muted]omm setting upload <benchmark|usage|crash> --enable/--disable  ·  see PRIVACY.md[/muted]")
```

- Add the usage command:

```python
@upload_app.command(name="usage")
@global_flags
def configure_upload_usage(
    ctx: typer.Context,
    enable: bool = typer.Option(False, "--enable", help="Send the anonymous daily usage batch."),
    disable: bool = typer.Option(False, "--disable", help="Never send usage stats (the default)."),
    reset_id: bool = typer.Option(False, "--reset-id", help="Generate a new random install id, unlinking future rows from past ones."),
) -> None:
    """Anonymous daily usage stats (opt-in, off by default). See PRIVACY.md
    for the exact fields. Run with no flags to print the current policy and
    the exact payload that would be sent next."""
    from omm import usage

    if enable and disable:
        err_console.print("[error]Choose one of --enable or --disable.[/error]")
        raise typer.Exit(1)
    if reset_id:
        config.CLIENT_ID_PATH.unlink(missing_ok=True)
        console.print(f"[success]New install id:[/success] {config.client_id()}")
    if enable:
        config_mod.update_config(usage_stats_policy="enabled")
        console.print("[success]Usage stats enabled.[/success] Turn off any time: `omm setting upload usage --disable`.")
    elif disable:
        config_mod.update_config(usage_stats_policy=None)
        discarded = usage.discard_pending()
        console.print("[success]Usage stats disabled.[/success]" + (f" Discarded {discarded} queued row(s)." if discarded else ""))
    if not (enable or disable or reset_id):
        cfg = load_config()
        console.print(f"Policy: {'enabled' if cfg.get('usage_stats_policy') == 'enabled' else 'off (default)'}")
        console.print(f"Install id: {config.client_id()}")
        console.print("\n[label]Next batch would send:[/label]")
        console.print_json(data=usage.build_payload())
```

- Update `_HELP_ALL_GROUPS`, `_print_full_command_reference`'s `setting` handling if it special-cases leaves, the `omm setup` closing line, and `onboarding.run_wizard`'s trailing "Error reports are off..." note to say `omm setting upload crash --ask`.

- [ ] **Step 4: Run — expect PASS**

Run: `python -m pytest tests/test_cli_setting_upload.py -q`

- [ ] **Step 5: Find and fix callers of the old commands**

```bash
grep -rn '"error-reports"\|setting.*upload\|configure_upload\|configure_error_reports' tests/ src/ docs/
```
Update every test invocation to the new path; update `README` / `docs` references (the `docs/crash-reports.md` rename happens in Task 9 — just fix command paths here).

- [ ] **Step 6: Regression**

Run: `python -m pytest tests/ -q -k "setting or error_report or telemetry or onboarding"`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/omm/cli.py tests/test_cli_setting_upload.py <other touched test files>
git commit -m "feat: consolidate outbound-data settings under 'omm setting upload'"
```

---

### Task 6: `omm setup` consent step

**Files:**
- Modify: `src/omm/onboarding.py`
- Test: `tests/test_onboarding_data_sharing.py` (create)

**Interfaces:**
- `onboarding.run_data_sharing_step(console) -> None` — called from `run_wizard` after the engine checklist, before `run_completion_step`. Yes ⇒ `usage_stats_policy="enabled"` + `error_report_send_policy="ask"`. No / declined / non-TTY ⇒ nothing changed.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_onboarding_data_sharing.py
from omm import onboarding, config


def test_yes_enables_both(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(onboarding, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(onboarding, "_confirm_data_sharing", lambda default: True)
    onboarding.run_data_sharing_step(_console())
    cfg = config.load_config()
    assert cfg["usage_stats_policy"] == "enabled"
    assert cfg["error_report_send_policy"] == "ask"


def test_no_changes_nothing(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(onboarding, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(onboarding, "_confirm_data_sharing", lambda default: False)
    onboarding.run_data_sharing_step(_console())
    cfg = config.load_config()
    assert cfg.get("usage_stats_policy") is None
    assert cfg.get("error_report_send_policy") is None


def test_non_tty_changes_nothing(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(onboarding, "_stdin_is_tty", lambda: False)
    onboarding.run_data_sharing_step(_console())
    assert config.load_config().get("usage_stats_policy") is None


def test_consent_text_lists_every_payload_key(isolated_omm_home):
    from omm import usage
    text = onboarding._DATA_SHARING_TEXT.lower()
    # human-readable coverage, not literal key names
    for concept in ("version", "install", "os", "cpu", "ram", "vram", "gpu", "command"):
        assert concept in text


def _console():
    from rich.console import Console
    return Console()
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement**

```python
from omm import config as config_mod  # if not already imported

_DATA_SHARING_TEXT = """\
omm can send anonymous usage data so we know which versions and hardware to
support, and which commands are breaking.

If you say yes, once a day omm sends ONE batch containing:
  • a random id (not tied to you — reset: omm setting upload usage --reset-id)
  • omm version, install method, OS, CPU architecture
  • RAM / VRAM size range, GPU vendor
  • which commands you ran and whether they succeeded
It never sends model names, search terms, file paths, your IP, or hostname.

Saying yes also turns on crash reports (you're asked before each one is sent).

Default is OFF. Change any time with `omm setting upload`.
Full details: https://github.com/omm-hippo/omm/blob/main/PRIVACY.md"""


def _confirm_data_sharing(default: bool) -> bool:
    import questionary

    q = _add_escape_to_cancel(
        questionary.confirm("Send anonymous usage data + crash reports?", default=default)
    )
    return bool(q.ask())


def run_data_sharing_step(console: Console) -> None:
    """Ask once whether omm may send anonymous usage stats (and crash
    reports). Off unless the user explicitly answers yes on a TTY."""
    if not _stdin_is_tty():
        console.print(
            "[muted]Anonymous usage stats and crash reports are off. "
            "Enable with `omm setting upload`.[/muted]\n"
        )
        return
    from rich.panel import Panel

    console.print(Panel(_DATA_SHARING_TEXT, title="Help improve omm", border_style="muted"))
    current = config_mod.load_config().get("usage_stats_policy") == "enabled"
    try:
        said_yes = _confirm_data_sharing(current)
    except Exception:
        return
    if said_yes:
        config_mod.update_config(usage_stats_policy="enabled", error_report_send_policy="ask")
        console.print("[success]Thanks![/success] Turn it off any time: `omm setting upload`.\n")
    else:
        config_mod.update_config(usage_stats_policy=None)
        console.print("[muted]No data will be sent.[/muted]\n")
```

Wire into `run_wizard` between the engine-install block and `run_completion_step(console)`:

```python
    run_data_sharing_step(console)
    run_completion_step(console)
```

Also drop the now-redundant trailing "Error reports are off unless you turn them on" line (the step covers it), or update it to `omm setting upload`.

- [ ] **Step 4: Run — expect PASS**

Run: `python -m pytest tests/test_onboarding_data_sharing.py tests/test_onboarding*.py -q`

- [ ] **Step 5: Manual**

```bash
OMM_HOME=$(mktemp -d) .venv/bin/omm setup   # walk through, choose No, verify config untouched
```

- [ ] **Step 6: Commit**

```bash
git add src/omm/onboarding.py tests/test_onboarding_data_sharing.py
git commit -m "feat: ask about anonymous data sharing in omm setup (off by default)"
```

---

### Task 7: Cloudflare Worker `/usage` route

**Files:**
- Modify: `cf-worker/src/index.ts`, `cf-worker/src/validate.ts`, `cf-worker/src/rtdb.ts`
- Test: `cf-worker/test/usage.test.ts` (create)

- [ ] **Step 1: Write the failing test** — `cf-worker/test/usage.test.ts`, mirroring `error-report.test.ts`:

```ts
import { describe, it, expect } from "vitest";
import { validateUsageEvent } from "../src/validate";

const base = {
  schema_version: 1,
  client_id: "0123456789abcdef0123456789abcdef",
  client_version: "0.3.11",
  install_source: "pipx",
  os_name: "Darwin",
  os_version: "23.5.0",
  cpu_arch: "arm64",
  ram_gb_bucket: "16-32",
  vram_gb_bucket: "none",
  gpu_vendor: "apple",
  recorded_at: "2026-08-30T00:00:00+00:00",
  update_channel: "stable",
  commands: { "install ok": 3, "search ok": 5 },
};

describe("validateUsageEvent", () => {
  it("accepts a well-formed row", () => {
    expect(validateUsageEvent({ ...base }).valid).toBe(true);
  });
  it("rejects an unknown field", () => {
    expect(validateUsageEvent({ ...base, hostname: "x" }).valid).toBe(false);
  });
  it("rejects a bad bucket", () => {
    expect(validateUsageEvent({ ...base, ram_gb_bucket: "1234gb" }).valid).toBe(false);
  });
  it("rejects an oversized commands map", () => {
    const commands: Record<string, number> = {};
    for (let i = 0; i < 101; i++) commands[`cmd${i} ok`] = 1;
    expect(validateUsageEvent({ ...base, commands }).valid).toBe(false);
  });
  it("rejects a non-integer command count", () => {
    expect(validateUsageEvent({ ...base, commands: { "install ok": 1.5 } }).valid).toBe(false);
  });
});
```

- [ ] **Step 2: Run — expect FAIL**

Run: `cd cf-worker && npm test`

- [ ] **Step 3: Implement**

`cf-worker/src/validate.ts` — add:

```ts
const USAGE_FIELDS = new Set([
  "schema_version", "client_id", "client_version", "install_source",
  "os_name", "os_version", "cpu_arch", "ram_gb_bucket", "vram_gb_bucket",
  "gpu_vendor", "recorded_at", "update_channel", "commands", "errors",
]);
const INSTALL_SOURCES = new Set(["pipx", "homebrew", "npm", "pypi", "git", "unknown"]);
const GPU_VENDORS = new Set(["apple", "nvidia", "amd", "intel", "other", "none"]);
const RAM_BUCKETS = new Set(["<8", "8-16", "16-32", "32-64", "64-128", "128+"]);
const VRAM_BUCKETS = new Set(["none", "<4", "4-8", "8-12", "12-16", "16-24", "24+"]);
const TALLY_KEY_RE = /^[a-z-]+ [a-z_-]+$/;

function validTally(v: unknown): boolean {
  if (typeof v !== "object" || v === null || Array.isArray(v)) return false;
  const entries = Object.entries(v as Record<string, unknown>);
  if (entries.length > 100) return false;
  return entries.every(([k, n]) =>
    typeof k === "string" && k.length <= 80 && TALLY_KEY_RE.test(k) &&
    typeof n === "number" && Number.isInteger(n) && n >= 1 && n <= 100000);
}

export function validateUsageEvent(event: TelemetryEvent): { valid: boolean; reason?: string } {
  for (const key of Object.keys(event)) {
    if (!USAGE_FIELDS.has(key)) return { valid: false, reason: `unknown field: ${key}` };
  }
  if (!hasAll(event, ["schema_version", "client_id", "client_version", "os_name", "recorded_at"]))
    return { valid: false, reason: "missing required usage field" };
  if (num(event, "schema_version") !== 1) return { valid: false, reason: "unsupported schema_version" };
  if (!/^[0-9a-f]{8,64}$/.test(str(event, "client_id"))) return { valid: false, reason: "bad client_id" };
  if (has(event, "install_source") && !INSTALL_SOURCES.has(str(event, "install_source")))
    return { valid: false, reason: "bad install_source" };
  if (has(event, "gpu_vendor") && !GPU_VENDORS.has(str(event, "gpu_vendor")))
    return { valid: false, reason: "bad gpu_vendor" };
  if (has(event, "ram_gb_bucket") && !RAM_BUCKETS.has(str(event, "ram_gb_bucket")))
    return { valid: false, reason: "bad ram_gb_bucket" };
  if (has(event, "vram_gb_bucket") && !VRAM_BUCKETS.has(str(event, "vram_gb_bucket")))
    return { valid: false, reason: "bad vram_gb_bucket" };
  if (has(event, "update_channel") && !["stable", "beta"].includes(str(event, "update_channel")))
    return { valid: false, reason: "bad update_channel" };
  const stringLimits: Record<string, number> = {
    client_version: 100, os_name: 128, os_version: 128, cpu_arch: 64,
  };
  for (const [key, limit] of Object.entries(stringLimits)) {
    if (has(event, key)) {
      const value = str(event, key);
      if (!value || value.length > limit || looksLikePathOrControlChars(value))
        return { valid: false, reason: `invalid ${key}` };
    }
  }
  if (!(str(event, "recorded_at").length >= 20 && str(event, "recorded_at").length <= 50))
    return { valid: false, reason: "invalid recorded_at" };
  if (has(event, "commands") && !validTally(event.commands)) return { valid: false, reason: "invalid commands" };
  if (has(event, "errors") && !validTally(event.errors)) return { valid: false, reason: "invalid errors" };
  return { valid: true };
}
```

(Reuse whatever `str`/`num`/`has`/`hasAll`/`looksLikePathOrControlChars` helpers `validateErrorReport` already uses — check their exact names/signatures first.)

`cf-worker/src/rtdb.ts` — change both `node: "telemetry" | "error_reports"` unions to `node: "telemetry" | "error_reports" | "usage"`.

`cf-worker/src/index.ts`:

```ts
    const node = url.pathname === "/telemetry"
      ? "telemetry"
      : url.pathname === "/error-report"
        ? "error_reports"
        : url.pathname === "/usage"
          ? "usage"
          : null;
```

and the validator dispatch:

```ts
    const result = node === "telemetry"
      ? validateTelemetryEvent(event as Record<string, unknown>)
      : node === "error_reports"
        ? validateErrorReport(event as Record<string, unknown>)
        : validateUsageEvent(event as Record<string, unknown>);
```

(import `validateUsageEvent`.)

- [ ] **Step 4: Run — expect PASS**

Run: `cd cf-worker && npm test && npx tsc -p tsconfig.json`

- [ ] **Step 5: Commit**

```bash
git add cf-worker/src/index.ts cf-worker/src/validate.ts cf-worker/src/rtdb.ts cf-worker/test/usage.test.ts
git commit -m "feat(cf-worker): add /usage route and validator"
```

---

### Task 8: Firebase RTDB rules

**Files:**
- Modify: `database.rules.json`, `scripts/test_firebase_rules.mjs`

- [ ] **Step 1: Add the `usage` rules block** (sibling of `error_reports`, before `$other`):

```json
    "usage": {
      ".read": false,
      ".write": false,
      "$event": {
        ".validate": "newData.hasChildren(['schema_version', 'client_id', 'client_version', 'os_name', 'recorded_at'])",
        "schema_version": { ".validate": "newData.isNumber() && newData.val() == 1" },
        "client_id": { ".validate": "newData.isString() && newData.val().matches(/^[0-9a-f]{8,64}$/)" },
        "client_version": { ".validate": "newData.isString() && newData.val().length > 0 && newData.val().length <= 100" },
        "install_source": { ".validate": "!newData.exists() || newData.val() == 'pipx' || newData.val() == 'homebrew' || newData.val() == 'npm' || newData.val() == 'pypi' || newData.val() == 'git' || newData.val() == 'unknown'" },
        "os_name": { ".validate": "newData.isString() && newData.val().length > 0 && newData.val().length <= 128" },
        "os_version": { ".validate": "!newData.exists() || (newData.isString() && newData.val().length <= 128)" },
        "cpu_arch": { ".validate": "!newData.exists() || (newData.isString() && newData.val().length > 0 && newData.val().length <= 64)" },
        "ram_gb_bucket": { ".validate": "!newData.exists() || newData.val() == '<8' || newData.val() == '8-16' || newData.val() == '16-32' || newData.val() == '32-64' || newData.val() == '64-128' || newData.val() == '128+'" },
        "vram_gb_bucket": { ".validate": "!newData.exists() || newData.val() == 'none' || newData.val() == '<4' || newData.val() == '4-8' || newData.val() == '8-12' || newData.val() == '12-16' || newData.val() == '16-24' || newData.val() == '24+'" },
        "gpu_vendor": { ".validate": "!newData.exists() || newData.val() == 'apple' || newData.val() == 'nvidia' || newData.val() == 'amd' || newData.val() == 'intel' || newData.val() == 'other' || newData.val() == 'none'" },
        "update_channel": { ".validate": "!newData.exists() || newData.val() == 'stable' || newData.val() == 'beta'" },
        "recorded_at": { ".validate": "newData.isString() && newData.val().length >= 20 && newData.val().length <= 50" },
        "commands": { "$k": { ".validate": "newData.isNumber() && newData.val() >= 1 && newData.val() <= 100000" } },
        "errors": { "$k": { ".validate": "newData.isNumber() && newData.val() >= 1 && newData.val() <= 100000" } },
        "$other": { ".validate": false }
      }
    },
```

Note: RTDB `.validate` cannot count `commands` keys or regex-check `$k` — the Worker's `validateUsageEvent` is the real gate (same division of labour the comment in `validate.ts:5` already describes). The rules just bound types and the known fields.

- [ ] **Step 2: Add emulator cases** to `scripts/test_firebase_rules.mjs` — mirror the error-report cases: one accepted well-formed usage row, one rejected (unknown field via `$other`), one rejected (bad bucket enum), one rejected (missing `client_id`).

- [ ] **Step 3: Run**

```bash
npx --yes firebase-tools emulators:exec --only database --project demo-localfit \
  "node scripts/test_firebase_rules.mjs"
```
Expected: PASS (needs Java + node — if unavailable locally, note it and rely on CI).

- [ ] **Step 4: Commit**

```bash
git add database.rules.json scripts/test_firebase_rules.mjs
git commit -m "feat: RTDB rules for the /usage node"
```

---

### Task 9: PRIVACY.md, docs rename, README, CLAUDE.md

**Files:**
- Create: `PRIVACY.md`
- Rename: `docs/error-reports.md` → `docs/crash-reports.md` (`git mv`); fix inbound links
- Modify: `README*`, `CLAUDE.md`

- [ ] **Step 1: `git mv docs/error-reports.md docs/crash-reports.md`**, then `grep -rn "error-reports.md" .` and fix every reference (onboarding text, README, specs, the `docs/crash-reports.md` body's own title).

- [ ] **Step 2: Write `PRIVACY.md`** — the table from the spec's "PRIVACY.md" section, plus: the exact usage field list, bucket definitions, `client-id` explanation + `--reset-id`, and a "turn everything off" block:

```
omm setting upload benchmark --disable
omm setting upload usage --disable
omm setting upload crash --disable
omm setting telemetry --endpoint none
```

- [ ] **Step 3: README** — near the existing telemetry sentence, add one line linking `PRIVACY.md` and naming the three `omm setting upload` channels.

- [ ] **Step 4: CLAUDE.md** — in the Telemetry architecture paragraph, add a sentence: usage stats are a third opt-in outbound channel (`omm/usage.py`, `/usage` Worker route, RTDB `usage` node, `usage_stats_policy`), off by default, daily batch, consented in `omm setup`; all three outbound channels are configured under `omm setting upload {benchmark,usage,crash}`. (This file is tracked and edited by other sessions — make a minimal additive edit and re-check `git status` before committing.)

- [ ] **Step 5: Full verification**

```bash
python -m pytest -q
cd cf-worker && npm test && npx tsc -p tsconfig.json && cd ..
npm --prefix packaging/npm/launcher test
python scripts/npm_package.py validate
OMM_HOME=$(mktemp -d) .venv/bin/omm --help >/dev/null && echo "help ok"
```
All green (sklearn skips are not failures).

- [ ] **Step 6: Commit**

```bash
git add PRIVACY.md docs/crash-reports.md README* CLAUDE.md
git commit -m "docs: PRIVACY.md and rename error-reports doc to crash-reports"
```

---

## Self-Review

**Spec coverage:**
- Snapshot fields + buckets + gpu_vendor → Task 2 (`_snapshot`, `_bucket`, `_gpu_vendor`). ✓
- Command tally + 100-key cap + error class only → Task 2 (`_aggregate`), tests assert no message leak + cap. ✓
- `omm/usage.py` mirrors `error_report.py` (queue, flush, backoff, attempt log) → Tasks 2–3. ✓
- `client_id` in its own file, `--reset-id` → Task 1 + Task 5. ✓
- `usage_stats_policy` None/"enabled", no "ask" → Task 1 + Task 3 (`policy()`). ✓
- Wiring: `main()` records, root callback flushes → Task 4. ✓
- Endpoint baked in, `_post` refuses others → Task 1 + Task 3 (`_post_to` guard + test). ✓
- `omm setting upload {benchmark,usage,crash}`, old commands deleted → Task 5. ✓
- `omm setup` consent, off unless explicit TTY yes, sets both keys → Task 6. ✓
- cf-worker `/usage` + `validateUsageEvent` + node union + tests → Task 7. ✓
- `database.rules.json` + emulator tests → Task 8. ✓
- PRIVACY.md + doc rename + README + CLAUDE.md → Task 9. ✓
- Non-goals (no run-log read, no PII, no dashboard, no deploy) → respected; `_snapshot`/`_aggregate` build fresh, no `runlog` import. ✓

**Placeholder scan:** Task 2 Step 4 and Task 7 Step 3 tell the implementer to confirm real accessor names (`hardware.detect_hardware`, `package_metadata.version`, the cf-worker `str/num/has` helpers) before finalizing — the surrounding code IS given; only the upstream symbol names need a one-line grep. Task 3 Step 1 flags that `test_post_refuses_non_gateway_endpoint` should be simplified to target `_post_to`; Step 3 provides `_post_to` with the guard. Acceptable.

**Type consistency:** `record_run(subcommand, outcome, error_class)`, `build_payload() -> dict`, `flush_pending(force=False) -> bool`, `policy() -> "enabled"|"never"`, `discard_pending() -> int`, `pending_count() -> int`, `config.client_id() -> str`, `config.CLIENT_ID_PATH` — consistent across Tasks 1–6. Event field names match between `_snapshot` (Task 2), `validateUsageEvent` (Task 7), and the rules block (Task 8): `schema_version, client_id, client_version, install_source, os_name, os_version, cpu_arch, ram_gb_bucket, vram_gb_bucket, gpu_vendor, recorded_at, update_channel, commands, errors`. Bucket enumerations identical in Python `_RAM_BUCKETS`/`_VRAM_BUCKETS`, TS `RAM_BUCKETS`/`VRAM_BUCKETS`, and the rules `.validate` strings.
