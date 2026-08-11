# LM Studio install-time load verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After `omm install` links a GGUF into LM Studio, prove it actually
loads by sending a real short generation request to LM Studio's local
server, warning (never failing) the install if it doesn't.

**Architecture:** New private helper functions in `src/omm/linker.py`
(alongside the existing per-engine link functions), wired into
`src/omm/cli.py`'s `install()` command right after the existing Ollama
post-link block. All new functions fail soft (`None`) on anything short of
a confirmed bad generation (`False`), matching the existing
`_ollama_accepts_manifest` convention in the same file.

**Tech Stack:** Python stdlib `subprocess` (talks to the bundled `lms`
CLI) + `requests` (already a project dependency, imported lazily to match
`quality.py`'s `_request_json` style) for the HTTP probe.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-11-lmstudio-install-load-verify-design.md`
- Never raise from any new function — `verify_lmstudio_load` and everything
  it calls must degrade to `None`/`False`/no-op, never an exception that
  could fail `omm install`.
- Never guess the LM Studio server port — always read it from
  `lms server status --json`.
- Only stop the LM Studio server if this code is the one that started it;
  never touch a server the user already had running.
- Model identifier passed to the API/`lms unload` is the `repo` value from
  the existing `_lmstudio_publisher_repo()` (`linker.py:452`) — confirmed
  against a real LM Studio 0.4.20 instance to be exactly what `/v1/models`
  reports as `id`.

---

### Task 1: `lms` CLI discovery + server status query

**Files:**
- Modify: `src/omm/linker.py` (add after `unlink_lmstudio`, currently
  ending around line 505, before the `# --- Ollama ---` section header)
- Test: `tests/test_linker_new_engines.py` (append)

**Interfaces:**
- Consumes: `linker.lmstudio_home_dir()` (existing, `linker.py:71`)
- Produces:
  - `linker._lms_cli_path() -> str | None`
  - `linker._lmstudio_server_status(lms_path: str, timeout: float = 5) -> dict | None`
    (dict shape when not `None`: `{"running": bool, "port": int}`)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_linker_new_engines.py`:

```python
# --- LM Studio load-verification helpers -------------------------------


def test_lms_cli_path_prefers_which(monkeypatch):
    monkeypatch.setattr(linker.shutil, "which", lambda name: "/usr/local/bin/lms" if name == "lms" else None)
    assert linker._lms_cli_path() == "/usr/local/bin/lms"


def test_lms_cli_path_falls_back_to_bootstrap_location(tmp_path, monkeypatch):
    monkeypatch.setattr(linker.shutil, "which", lambda name: None)
    monkeypatch.setattr(linker, "lmstudio_home_dir", lambda: tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    lms_file = bin_dir / "lms"
    lms_file.write_text("#!/bin/sh\n")
    assert linker._lms_cli_path() == str(lms_file)


def test_lms_cli_path_returns_none_when_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(linker.shutil, "which", lambda name: None)
    monkeypatch.setattr(linker, "lmstudio_home_dir", lambda: tmp_path)
    assert linker._lms_cli_path() is None


class _FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_lmstudio_server_status_parses_running_json(monkeypatch):
    monkeypatch.setattr(
        linker.subprocess, "run",
        lambda cmd, **kw: _FakeResult(stdout='{"running": true, "port": 1234}'),
    )
    assert linker._lmstudio_server_status("lms") == {"running": True, "port": 1234}


def test_lmstudio_server_status_returns_none_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(linker.subprocess, "run", lambda cmd, **kw: _FakeResult(returncode=1))
    assert linker._lmstudio_server_status("lms") is None


def test_lmstudio_server_status_returns_none_on_bad_json(monkeypatch):
    monkeypatch.setattr(linker.subprocess, "run", lambda cmd, **kw: _FakeResult(stdout="not json"))
    assert linker._lmstudio_server_status("lms") is None


def test_lmstudio_server_status_returns_none_on_timeout(monkeypatch):
    def _raise(cmd, **kw):
        raise linker.subprocess.TimeoutExpired(cmd, kw.get("timeout", 5))

    monkeypatch.setattr(linker.subprocess, "run", _raise)
    assert linker._lmstudio_server_status("lms") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_linker_new_engines.py -k lms_cli_path or lmstudio_server_status -v`
Expected: FAIL with `AttributeError: module 'omm.linker' has no attribute '_lms_cli_path'` (and similarly for `_lmstudio_server_status`).

- [ ] **Step 3: Write minimal implementation**

Add to `src/omm/linker.py`, right after `unlink_lmstudio` (currently ends
around line 505) and before the `# --- Ollama ---` section:

```python
# --- LM Studio load verification ----------------------------------------
#
# LM Studio has no benchmark path (unlike Ollama, where `omm benchmark`
# already exercises real loading via /api/generate) - so nothing else ever
# proves a linked model actually loads. These functions send a real short
# generation request through LM Studio's own local server to check.
# Everything here fails soft: only a confirmed bad generation returns
# False; every other obstacle (lms missing, server unreachable, ambiguous
# timeout) returns None, matching _ollama_accepts_manifest's convention.


def _lms_cli_path() -> str | None:
    """Locate the `lms` CLI LM Studio bootstraps on first run. Not
    guaranteed to be on PATH in a non-interactive shell even when
    installed, so also check the well-known bootstrap location directly -
    confirmed via `lms bootstrap` against a real LM Studio 0.4.20 install,
    which installs to <lmstudio_home_dir>/bin/lms."""
    found = shutil.which("lms")
    if found is not None:
        return found
    candidate = lmstudio_home_dir() / "bin" / "lms"
    return str(candidate) if candidate.is_file() else None


def _lmstudio_server_status(lms_path: str, timeout: float = 5) -> dict | None:
    """Ask `lms` whether its local server is running and on what port -
    the port is user-configurable, so this is the only reliable source for
    it. None on any failure to ask (never guess a default port)."""
    try:
        result = subprocess.run(
            [lms_path, "server", "status", "--json"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("running"), bool)
        or not isinstance(data.get("port"), int)
    ):
        return None
    return data
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_linker_new_engines.py -k "lms_cli_path or lmstudio_server_status" -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/omm/linker.py tests/test_linker_new_engines.py
git commit -m "feat: add lms CLI discovery and server status query"
```

---

### Task 2: Server start/stop helpers

**Files:**
- Modify: `src/omm/linker.py` (append after Task 1's functions)
- Test: `tests/test_linker_new_engines.py` (append)

**Interfaces:**
- Consumes: `linker._lmstudio_server_status` (Task 1)
- Produces:
  - `linker._start_lmstudio_server(lms_path: str, timeout: float = 30) -> bool`
  - `linker._stop_lmstudio_server(lms_path: str) -> None`

- [ ] **Step 1: Write the failing tests**

```python
def test_start_lmstudio_server_returns_true_once_status_reports_running(monkeypatch):
    calls = {"n": 0}

    def fake_run(cmd, **kw):
        return _FakeResult()

    def fake_status(lms_path, timeout=5):
        calls["n"] += 1
        return {"running": calls["n"] >= 2, "port": 1234}

    monkeypatch.setattr(linker.subprocess, "run", fake_run)
    monkeypatch.setattr(linker, "_lmstudio_server_status", fake_status)
    monkeypatch.setattr(linker.time, "sleep", lambda seconds: None)
    assert linker._start_lmstudio_server("lms", timeout=5) is True


def test_start_lmstudio_server_returns_false_on_timeout(monkeypatch):
    monkeypatch.setattr(linker.subprocess, "run", lambda cmd, **kw: _FakeResult())
    monkeypatch.setattr(linker, "_lmstudio_server_status", lambda lms_path, timeout=5: {"running": False, "port": 1234})
    monkeypatch.setattr(linker.time, "sleep", lambda seconds: None)
    assert linker._start_lmstudio_server("lms", timeout=2) is False


def test_start_lmstudio_server_returns_false_when_start_command_fails(monkeypatch):
    def _raise(cmd, **kw):
        raise OSError("lms not executable")

    monkeypatch.setattr(linker.subprocess, "run", _raise)
    assert linker._start_lmstudio_server("lms", timeout=5) is False


def test_stop_lmstudio_server_swallows_failures(monkeypatch):
    def _raise(cmd, **kw):
        raise OSError("already gone")

    monkeypatch.setattr(linker.subprocess, "run", _raise)
    linker._stop_lmstudio_server("lms")  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_linker_new_engines.py -k "start_lmstudio_server or stop_lmstudio_server" -v`
Expected: FAIL with `AttributeError`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/omm/linker.py`:

```python
_LMSTUDIO_SERVER_START_TIMEOUT_SECONDS = 30
_LMSTUDIO_SERVER_START_POLL_INTERVAL_SECONDS = 1


def _start_lmstudio_server(
    lms_path: str, timeout: float = _LMSTUDIO_SERVER_START_TIMEOUT_SECONDS
) -> bool:
    """Best-effort `lms server start`, polling status until it reports
    running or `timeout` elapses. Bounded - never waits indefinitely."""
    try:
        subprocess.run(
            [lms_path, "server", "start"], capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    elapsed = 0.0
    while elapsed < timeout:
        status = _lmstudio_server_status(lms_path)
        if status is not None and status.get("running"):
            return True
        time.sleep(_LMSTUDIO_SERVER_START_POLL_INTERVAL_SECONDS)
        elapsed += _LMSTUDIO_SERVER_START_POLL_INTERVAL_SECONDS
    return False


def _stop_lmstudio_server(lms_path: str) -> None:
    """Best-effort `lms server stop`. Only ever called for a server this
    module started itself; failures here must never surface as an install
    error."""
    try:
        subprocess.run([lms_path, "server", "stop"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_linker_new_engines.py -k "start_lmstudio_server or stop_lmstudio_server" -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/omm/linker.py tests/test_linker_new_engines.py
git commit -m "feat: add LM Studio server start/stop helpers"
```

---

### Task 3: Generation probe + unload

**Files:**
- Modify: `src/omm/linker.py` (append after Task 2's functions)
- Test: `tests/test_linker_new_engines.py` (append)

**Interfaces:**
- Consumes: none from earlier tasks (standalone HTTP/subprocess calls)
- Produces:
  - `linker._probe_lmstudio_generate(port: int, repo: str, timeout: float = 120) -> bool | None`
  - `linker._lms_unload(lms_path: str, repo: str) -> None`

- [ ] **Step 1: Write the failing tests**

```python
class _FakeHTTPResponse:
    def __init__(self, ok=True, status_code=200, payload=None):
        self.ok = ok
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def test_probe_lmstudio_generate_true_on_real_text(monkeypatch):
    import requests

    payload = {"choices": [{"message": {"content": "OK"}}]}
    monkeypatch.setattr(requests, "post", lambda url, json, timeout: _FakeHTTPResponse(payload=payload))
    assert linker._probe_lmstudio_generate(1234, "tinyllama-test") is True


def test_probe_lmstudio_generate_false_on_empty_content(monkeypatch):
    import requests

    payload = {"choices": [{"message": {"content": "   "}}]}
    monkeypatch.setattr(requests, "post", lambda url, json, timeout: _FakeHTTPResponse(payload=payload))
    assert linker._probe_lmstudio_generate(1234, "tinyllama-test") is False


def test_probe_lmstudio_generate_false_on_http_error(monkeypatch):
    import requests

    monkeypatch.setattr(requests, "post", lambda url, json, timeout: _FakeHTTPResponse(ok=False, status_code=500))
    assert linker._probe_lmstudio_generate(1234, "tinyllama-test") is False


def test_probe_lmstudio_generate_false_on_malformed_json(monkeypatch):
    import requests

    class _BadJSON(_FakeHTTPResponse):
        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(requests, "post", lambda url, json, timeout: _BadJSON())
    assert linker._probe_lmstudio_generate(1234, "tinyllama-test") is False


def test_probe_lmstudio_generate_none_on_connection_error(monkeypatch):
    import requests

    def _raise(url, json, timeout):
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(requests, "post", _raise)
    assert linker._probe_lmstudio_generate(1234, "tinyllama-test") is None


def test_lms_unload_swallows_failures(monkeypatch):
    def _raise(cmd, **kw):
        raise OSError("model not found")

    monkeypatch.setattr(linker.subprocess, "run", _raise)
    linker._lms_unload("lms", "tinyllama-test")  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_linker_new_engines.py -k "probe_lmstudio_generate or lms_unload" -v`
Expected: FAIL with `AttributeError`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/omm/linker.py`:

```python
_LMSTUDIO_PROBE_TIMEOUT_SECONDS = 120
_LMSTUDIO_PROBE_PROMPT = "Reply with the single word OK."
_LMSTUDIO_PROBE_MAX_TOKENS = 8


def _probe_lmstudio_generate(
    port: int, repo: str, timeout: float = _LMSTUDIO_PROBE_TIMEOUT_SECONDS
) -> bool | None:
    """Send a fixed short prompt to LM Studio's OpenAI-compatible endpoint,
    which JIT-loads `repo` if it isn't already resident - confirmed
    against a real LM Studio 0.4.20 instance (a symlinked GGUF at
    models/<publisher>/<repo>/<file>.gguf answered a /v1/chat/completions
    request for model=<repo> with no explicit `lms load` first). True on a
    real text response, False on an HTTP/response-shape failure (model
    didn't load), None on a network error - inconclusive, not proof of
    failure. `timeout` is generous because first-load time on a large
    model, not just generation time, is included."""
    import requests

    try:
        response = requests.post(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            json={
                "model": repo,
                "messages": [{"role": "user", "content": _LMSTUDIO_PROBE_PROMPT}],
                "max_tokens": _LMSTUDIO_PROBE_MAX_TOKENS,
                "stream": False,
            },
            timeout=timeout,
        )
    except requests.RequestException:
        return None
    if not response.ok:
        return False
    try:
        data = response.json()
    except ValueError:
        return False
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return False
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return isinstance(content, str) and len(content.strip()) > 0


def _lms_unload(lms_path: str, repo: str) -> None:
    """Best-effort isolation cleanup after a probe, mirroring
    quality.unload_model's role for Ollama. Confirmed against a real LM
    Studio instance that unloading a not-currently-loaded identifier exits
    cleanly rather than raising, but this still never propagates a
    failure either way."""
    try:
        subprocess.run([lms_path, "unload", repo], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_linker_new_engines.py -k "probe_lmstudio_generate or lms_unload" -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/omm/linker.py tests/test_linker_new_engines.py
git commit -m "feat: add LM Studio generation probe and unload helpers"
```

---

### Task 4: `verify_lmstudio_load` orchestrator

**Files:**
- Modify: `src/omm/linker.py` (append after Task 3's functions)
- Test: `tests/test_linker_new_engines.py` (append)

**Interfaces:**
- Consumes: `_lmstudio_publisher_repo` (existing, `linker.py:452`),
  `_lms_cli_path`, `_lmstudio_server_status`, `_start_lmstudio_server`,
  `_stop_lmstudio_server`, `_probe_lmstudio_generate`, `_lms_unload` (Tasks 1-3)
- Produces: `linker.verify_lmstudio_load(gguf_path: Path, repo_id: str | None) -> bool | None`
  (consumed by Task 5's `cli.py` wiring)

- [ ] **Step 1: Write the failing tests**

```python
def test_verify_lmstudio_load_none_when_lms_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: None)
    called = {"status": False}
    monkeypatch.setattr(linker, "_lmstudio_server_status", lambda *a, **k: called.__setitem__("status", True))
    assert linker.verify_lmstudio_load(tmp_path / "model.gguf", None) is None
    assert called["status"] is False


def test_verify_lmstudio_load_none_when_server_unreachable(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: "lms")
    monkeypatch.setattr(linker, "_lmstudio_server_status", lambda lms_path: None)
    assert linker.verify_lmstudio_load(tmp_path / "model.gguf", None) is None


def test_verify_lmstudio_load_leaves_already_running_server_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: "lms")
    monkeypatch.setattr(linker, "_lmstudio_server_status", lambda lms_path: {"running": True, "port": 1234})
    started = {"called": False}
    stopped = {"called": False}
    monkeypatch.setattr(linker, "_start_lmstudio_server", lambda *a, **k: started.__setitem__("called", True))
    monkeypatch.setattr(linker, "_stop_lmstudio_server", lambda *a, **k: stopped.__setitem__("called", True))
    monkeypatch.setattr(linker, "_probe_lmstudio_generate", lambda port, repo, **k: True)
    monkeypatch.setattr(linker, "_lms_unload", lambda *a, **k: None)

    assert linker.verify_lmstudio_load(tmp_path / "model.gguf", None) is True
    assert started["called"] is False
    assert stopped["called"] is False


def test_verify_lmstudio_load_starts_and_stops_server_when_not_running(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: "lms")
    monkeypatch.setattr(linker, "_lmstudio_server_status", lambda lms_path: {"running": False, "port": 1234})
    monkeypatch.setattr(linker, "_start_lmstudio_server", lambda lms_path: True)
    stopped = {"called": False}
    monkeypatch.setattr(linker, "_stop_lmstudio_server", lambda lms_path: stopped.__setitem__("called", True))
    monkeypatch.setattr(linker, "_probe_lmstudio_generate", lambda port, repo, **k: True)
    unloaded = {"repo": None}
    monkeypatch.setattr(linker, "_lms_unload", lambda lms_path, repo: unloaded.__setitem__("repo", repo))

    result = linker.verify_lmstudio_load(tmp_path / "model.gguf", "acme/widget")
    assert result is True
    assert stopped["called"] is True
    assert unloaded["repo"] == "widget"


def test_verify_lmstudio_load_none_when_server_start_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: "lms")
    monkeypatch.setattr(linker, "_lmstudio_server_status", lambda lms_path: {"running": False, "port": 1234})
    monkeypatch.setattr(linker, "_start_lmstudio_server", lambda lms_path: False)
    probed = {"called": False}
    monkeypatch.setattr(linker, "_probe_lmstudio_generate", lambda *a, **k: probed.__setitem__("called", True))
    assert linker.verify_lmstudio_load(tmp_path / "model.gguf", None) is None
    assert probed["called"] is False


def test_verify_lmstudio_load_false_propagates_and_still_unloads(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: "lms")
    monkeypatch.setattr(linker, "_lmstudio_server_status", lambda lms_path: {"running": True, "port": 1234})
    monkeypatch.setattr(linker, "_probe_lmstudio_generate", lambda port, repo, **k: False)
    unloaded = {"called": False}
    monkeypatch.setattr(linker, "_lms_unload", lambda *a, **k: unloaded.__setitem__("called", True))
    assert linker.verify_lmstudio_load(tmp_path / "model.gguf", None) is False
    assert unloaded["called"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_linker_new_engines.py -k verify_lmstudio_load -v`
Expected: FAIL with `AttributeError: module 'omm.linker' has no attribute 'verify_lmstudio_load'`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/omm/linker.py`:

```python
def verify_lmstudio_load(gguf_path: Path, repo_id: str | None) -> bool | None:
    """Prove a just-linked LM Studio model actually loads. Called once per
    successful link_lmstudio - LM Studio has no benchmark path to exercise
    this later the way Ollama's does. Soft-fails everywhere: only a
    confirmed bad generation returns False; every other obstacle returns
    None so a caller never turns "couldn't check" into "definitely
    broken."""
    _, repo = _lmstudio_publisher_repo(repo_id, gguf_path.name)
    lms_path = _lms_cli_path()
    if lms_path is None:
        return None
    status = _lmstudio_server_status(lms_path)
    if status is None:
        return None

    started_by_us = False
    if not status["running"]:
        if not _start_lmstudio_server(lms_path):
            return None
        started_by_us = True

    try:
        return _probe_lmstudio_generate(status["port"], repo)
    finally:
        _lms_unload(lms_path, repo)
        if started_by_us:
            _stop_lmstudio_server(lms_path)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_linker_new_engines.py -k verify_lmstudio_load -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/omm/linker.py tests/test_linker_new_engines.py
git commit -m "feat: add verify_lmstudio_load orchestrator"
```

---

### Task 5: Wire into `omm install`

**Files:**
- Modify: `src/omm/cli.py:1962` (right after the existing
  `if outcome.linked.get("ollama"):` block)
- Test: `tests/test_install_impl.py` (append)

**Interfaces:**
- Consumes: `linker.verify_lmstudio_load` (Task 4), `InstallOutcome.filename`
  and `InstallOutcome.repo_id` (existing, `cli.py:1448`)
- Produces: nothing further consumes this — it's the end of the chain.

- [ ] **Step 1: Write the failing test**

First check the existing test file's setup pattern:

```bash
grep -n "^def test_install\|def _run_install\|import typer\|CliRunner" tests/test_install_impl.py | head -20
```

Append to `tests/test_install_impl.py` a test that drives the real
`install()` Typer command (or `_install_impl` + the print block, whichever
matches how existing tests in this file invoke installs — follow the
pattern already used by the nearest existing test for printed output, e.g.
one asserting on `outcome.linked.get("ollama")` messaging if one exists).
If no existing test drives print output this way, test the block in
isolation instead:

```python
def test_install_prints_warning_when_lmstudio_load_verification_fails(monkeypatch, capsys):
    from omm import cli, linker

    outcome = cli.InstallOutcome(
        filename="model.gguf",
        repo_id="acme/widget",
        linked={"lmstudio": True},
    )
    monkeypatch.setattr(linker, "verify_lmstudio_load", lambda gguf_path, repo_id: False)

    cli._report_lmstudio_load_verification(outcome)

    captured = capsys.readouterr()
    assert "did not load successfully" in captured.out


def test_install_silent_when_lmstudio_load_verification_inconclusive(monkeypatch, capsys):
    from omm import cli, linker

    outcome = cli.InstallOutcome(
        filename="model.gguf",
        repo_id="acme/widget",
        linked={"lmstudio": True},
    )
    monkeypatch.setattr(linker, "verify_lmstudio_load", lambda gguf_path, repo_id: None)

    cli._report_lmstudio_load_verification(outcome)

    captured = capsys.readouterr()
    assert captured.out == ""


def test_install_skips_lmstudio_load_verification_when_not_linked(monkeypatch, capsys):
    from omm import cli, linker

    outcome = cli.InstallOutcome(filename="model.gguf", repo_id=None, linked={"lmstudio": False})
    called = {"count": 0}
    monkeypatch.setattr(linker, "verify_lmstudio_load", lambda *a, **k: called.__setitem__("count", called["count"] + 1))

    cli._report_lmstudio_load_verification(outcome)

    assert called["count"] == 0
```

This factors the wiring into a small testable `_report_lmstudio_load_verification(outcome)` helper in `cli.py` rather than testing it inline inside the large `install()` Typer command — matches how `install()` already delegates its heavy lifting to `_install_impl` rather than doing everything inline.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_install_impl.py -k lmstudio_load_verification -v`
Expected: FAIL with `AttributeError: module 'omm.cli' has no attribute '_report_lmstudio_load_verification'`.

- [ ] **Step 3: Write minimal implementation**

In `src/omm/cli.py`, add this function right before `def install(` (currently line 1908):

```python
def _report_lmstudio_load_verification(outcome: InstallOutcome) -> None:
    """Best-effort proof that a just-linked LM Studio model actually
    loads - LM Studio has no benchmark path to exercise this later the way
    `omm benchmark` does for Ollama. Only a confirmed failure is reported;
    "couldn't check" (lms missing, server unreachable, timeout) stays
    silent, matching the existing Ollama compat-check convention of never
    surfacing an inconclusive result as a warning."""
    if not outcome.linked.get("lmstudio"):
        return
    result = linker.verify_lmstudio_load(MODELS_DIR / outcome.filename, outcome.repo_id)
    if result is False:
        console.print(
            "[yellow]Warning: LM Studio linked this model but it did not "
            "load successfully in a live test.[/yellow]"
        )
```

Then in `install()`, right after the existing block (`cli.py:1962-1966`):

```python
    console.print(f"[green]Installed {outcome.filename}[/green]")
    if outcome.linked.get("ollama"):
        console.print(f"  Ollama: [green]ollama run {outcome.ollama_tag}[/green]")
    for spec in linker.ENGINES:
        if spec.key != "ollama" and outcome.linked.get(spec.key):
            console.print(f"  {spec.label}: visible in your local models list")
    console.print(f"  Uninstall with: [cyan]omm uninstall {outcome.filename}[/cyan]")
```

add a call to the new helper right after this block:

```python
    console.print(f"  Uninstall with: [cyan]omm uninstall {outcome.filename}[/cyan]")
    _report_lmstudio_load_verification(outcome)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_install_impl.py -k lmstudio_load_verification -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/omm/cli.py tests/test_install_impl.py
git commit -m "feat: warn when LM Studio load verification fails after install"
```

---

### Task 6: Full suite + close issue

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `pytest -q`
Expected: all tests pass, no regressions in `tests/test_linker_new_engines.py`,
`tests/test_install_impl.py`, or elsewhere.

- [ ] **Step 2: Close GitHub issue #19**

```bash
gh issue close 19 --repo omm-hippo/omm --comment "$(cat <<'EOF'
Implemented: `omm install` now runs a real load-verification probe against
LM Studio's local server (JIT-loads via /v1/chat/completions, mirroring
Ollama's own /api/generate auto-load) whenever a model links into LM
Studio. Non-blocking - a confirmed failed load prints a warning but
install still succeeds; anything inconclusive (lms CLI missing, server
unreachable) stays silent, matching the existing Ollama compat-check
convention.

Design: docs/superpowers/specs/2026-08-11-lmstudio-install-load-verify-design.md
Plan: docs/superpowers/plans/2026-08-11-lmstudio-install-load-verify.md

Empirically verified against a real LM Studio 0.4.20 install during
design (server status/start/stop, JIT-load, unload, and the
publisher/repo -> API model-id mapping all confirmed live).
EOF
)"
```
