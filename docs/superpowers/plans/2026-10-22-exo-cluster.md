# exo Mac Cluster + LAN Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `omm cluster init/join/status/model/token/leave` turn Thunderbolt-5-linked Macs into one exo cluster. Only an authenticated omm gateway is reachable from the office LAN.

**Architecture:** A new package `src/omm/cluster/` wraps the exo macOS app. omm never manages MLX files. exo owns its models, and omm only talks to its loopback API.

- **Head Mac:** runs a stdlib `ThreadingHTTPServer` gateway as a per-user LaunchAgent. The gateway checks per-employee tokens and proxies only the inference routes to `127.0.0.1:52415`.
- **Every Mac:** gets a root LaunchDaemon that loads a pf sub-anchor. The anchor drops exo's ports on every interface except loopback and Thunderbolt.

**Tech Stack:** Python 3.10+ stdlib (`http.server`, `http.client`, `plistlib`, `hashlib`, `secrets`), Typer (existing `cli.py`), `requests` (lazy import, SSE only), macOS `pfctl`/`launchctl`/`networksetup`/`rdma_ctl`, Homebrew cask `exo`.

**Spec:** `docs/superpowers/specs/2026-10-22-exo-cluster-design.md` (issue #503)

## Global Constraints

- macOS only for every `omm cluster` command. On other OSes, exit 2 with: `omm cluster runs on the Macs that form the cluster (macOS 26.2 or later). Employees connect from any OS through the gateway (#502).`
- Minimum macOS: `(26, 2)`.
- exo API upstream: `http://127.0.0.1:52415`. Gateway port: `52440`, bound to `0.0.0.0`.
- Install exo with `brew install --cask exo` only. Do not touch `linker.ENGINES`, `engine_packages.PACKAGES`, `registry.py`, or `linker.py`.
- Cluster state lives under `config.OMM_HOME / "cluster"`. Read `config.OMM_HOME` at call time, never at import.
- Lazy-import `requests` and `questionary` inside functions.
- Never log, upload, or persist request or response bodies. Gateway log fields are limited to: method, path, status, token name, duration_ms.
- Fail closed: an unreadable token store authenticates nobody.
- Prefer exo placements with `sharding == "Pipeline"`.
- Register the hidden `_cluster-gateway-run` in all 5 sets in `cli.py`: `_SKIP_UPDATE_CHECK_SUBCOMMANDS`, `_SKIP_ONBOARDING_SUBCOMMANDS`, `_SKIP_AUTO_IMPORT_SUBCOMMANDS`, `_SKIP_QUEUED_UPLOAD_SUBCOMMANDS`, `_INTERNAL_SUBCOMMANDS`.
- CLI changes ship with regenerated `docs/commands.json` (`python scripts/export_command_reference.py`) and README `## Usage` lines in the `omm cmd [flags]  # description` format. `python scripts/check_docs_sync.py` must pass.
- Branch off `beta`. Use `git add <own files>` only. Pushing is a separate ask.
- Tests must never call real `sudo`, `pfctl`, `launchctl`, `brew`, `networksetup`, or `rdma_ctl`. Inject `run=` callables or monkeypatch `subprocess.run`.

## Review Focus

1. **exo app quit from the menu bar.** `status` shows `exo: not running on this Mac` instead of a traceback. The gateway returns 502 `api_error`. `model` exits 1 with the same message. Tests are in Tasks 1, 4, and 8.
2. **`tokens.json` hand-edited or truncated.** `verify()` returns `None` for every token. The gateway answers 401. `token list` exits 1 naming the file and does not overwrite it. Tests are in Task 3.
3. **`init`/`join` re-run after Ctrl+C mid-way.** The namespace is reused, not regenerated. The firewall and LaunchAgent are re-applied without duplicates. Tests are in Task 8.
4. **A Mac drops out, or the OS versions differ.** This covers a cable pulled or a Mac on 26.2 while the others run 26.3. `status` lists `node <name> missing (seen before)` and `macOS versions differ: …` under problems. Tests are in Task 8.
5. **The Thunderbolt interface set changes after a cable moves to another port.** `firewall.status()` reports `drift`, and re-running `join`/`init` fixes it. Tests are in Task 6.

---

### Task 0: Real-hardware spike (4× M4 Pro Mac mini) — no product code

The spec's "실물로 먼저 확인할 것" list blocks Tasks 1, 2, 4, 6, and 8. Answer it on the real Macs. Use a throwaway login or the school Macs, not the user's daily machine.

**Files:**
- Create: `docs/superpowers/specs/2026-10-22-exo-cluster-spike-findings.md`
- Create: `tests/fixtures/exo/state.json`, `tests/fixtures/exo/models.json`, `tests/fixtures/exo/previews.json`, `tests/fixtures/exo/rdma_ctl_status_enabled.txt`, `tests/fixtures/exo/rdma_ctl_status_disabled.txt`, `tests/fixtures/exo/listallhardwareports.txt`
  - Scrub host names to `mac-a`..`mac-d`, and serials and UUIDs to stable fakes.

**Produces:** answers that later tasks consume as constants:
- `EXO_PORTS` (Task 6)
- `PF_ANCHOR` (Task 6)
- `ALLOWED_ROUTES` (Task 4)
- the namespace mechanism (Task 8)
- the `EXO.app` path (Task 2)
- JSON field mapping (Task 1)

- [ ] **Step 1:** Install with `brew install --cask exo` on all 4 Macs, all on the identical macOS 26.2+ build. Enable RDMA on each one following the exo README. Record:
  - any GUI/permission prompts the install raises
  - the installed app path

- [ ] **Step 2:** With 4 nodes discovered, list the listeners for every exo process. Run `sudo lsof -nP -iTCP -sTCP:LISTEN -a -c exo` and `sudo lsof -nP -iUDP -a -c exo` while a model is loaded across the 4 nodes. Record each port, its bind address, and whether it is fixed or ephemeral.

- [ ] **Step 3:** Hand-load a candidate pf sub-anchor on one Mac:
  - Run `sudo pfctl -E -a com.apple/omm-cluster -f <rules>`, using the Task 6 template with the Step 2 ports.
  - Confirm with `sudo pfctl -a com.apple/omm-cluster -sr`.
  - From a non-cluster laptop on the LAN, run `nc -vz <mac-ip> 52415`. It must be refused or time out.
  - Load a model across 4 nodes with the anchor active on all 4. Generation must still work.
  - Reboot one Mac and recheck whether the anchor survives. Expected: it does not survive without the LaunchDaemon.

  **STOP condition:** discovery or inference needs exo ports on the LAN interface, or ephemeral ports cannot be covered without blocking unrelated services. In either case, stop and report back to the user before Task 6.

- [ ] **Step 4:** Find how to set `EXO_ZENOH_NAMESPACE` for the app without the GUI. Try in this order:
  1. `defaults write <bundle-id> ...`
  2. an env var in the app's LaunchAgent
  3. a config file

  Record the exact working command and the app restart it needs.

- [ ] **Step 5:** Save the fixtures:
  - `curl -s 127.0.0.1:52415/state`, `/models`, and `/instance/previews?model_id=<a 70B-class model>`
  - `rdma_ctl status` output, both enabled and disabled
  - `networksetup -listallhardwareports`

- [ ] **Step 6:** On the head Mac, point Claude Code at exo directly:
  1. Set `ANTHROPIC_BASE_URL=http://127.0.0.1:52415` and `ANTHROPIC_AUTH_TOKEN=x`.
  2. Run one tool-using task to completion.
  3. Record every path and method Claude Code calls, from the exo log or a tcpdump on lo0.
  4. Repeat with Codex (`/v1/responses`) and pi (`/v1/chat/completions`).

  The union of observed calls is `ALLOWED_ROUTES`.

- [ ] **Step 7:** Write the findings doc with one section per question, then commit.

```bash
git add docs/superpowers/specs/2026-10-22-exo-cluster-spike-findings.md tests/fixtures/exo/
git commit -m "docs(cluster): record exo spike findings and API fixtures"
```

---

### Task 1: exo loopback client and parsers

**Files:**
- Create: `src/omm/cluster/__init__.py` (empty), `src/omm/cluster/exo_client.py`
- Test: `tests/test_cluster_exo_client.py`

**Interfaces:**
- Consumes: the Task 0 fixtures and field mapping; `omm.engines.base.LoopbackJsonClient`
- Produces (all frozen dataclasses):
  - `ClusterNode(node_id: str, name: str, memory_total_bytes: int, memory_available_bytes: int, rdma: Literal["enabled","disabled","unknown"], os_version: str | None)`
  - `ClusterLink(source_id: str, sink_id: str, kind: Literal["thunderbolt","rdma","other"])`
  - `ClusterInstance(instance_id: str, model_id: str, node_ids: tuple[str, ...], sharding: str)`
  - `ClusterState(master_id: str | None, nodes: tuple[ClusterNode, ...], links: tuple[ClusterLink, ...], instances: tuple[ClusterInstance, ...])`
  - `ExoModel(model_id: str, size_bytes: int | None)`
  - `Placement(model_id: str, sharding: str, instance: dict, memory_delta_by_node: dict[str, int], error: str | None)`
  - `parse_state(data: dict) -> ClusterState`, `parse_models(data: object) -> list[ExoModel]`, `parse_previews(data: dict) -> list[Placement]`
  - `class ExoUnavailable(RuntimeError)`. Its message is exactly `exo is not running on this Mac`.
  - `class ExoClient(base_url: str = "http://127.0.0.1:52415")` with:
    - `.state() -> ClusterState`
    - `.models() -> list[ExoModel]`
    - `.previews(model_id: str) -> list[Placement]`
    - `.create_instance(placement: Placement) -> str` (returns the command_id)
    - `.await_instance(model_id: str, timeout_seconds: float) -> bool`
    - `.delete_instance(instance_id: str) -> None`

- [ ] **Step 1: Write failing tests**

```python
FIX = Path(__file__).parent / "fixtures" / "exo"

def test_parse_state_fixture_has_four_nodes_with_memory_and_rdma():
    state = exo_client.parse_state(json.loads((FIX / "state.json").read_text(encoding="utf-8")))
    assert len(state.nodes) == 4
    assert all(n.memory_total_bytes > 0 for n in state.nodes)
    assert {n.rdma for n in state.nodes} <= {"enabled", "disabled", "unknown"}

def test_parse_previews_keeps_error_rows_and_pipeline_sharding():
    rows = exo_client.parse_previews(json.loads((FIX / "previews.json").read_text(encoding="utf-8")))
    assert rows and any(r.error is None for r in rows)
    assert all(isinstance(r.memory_delta_by_node, dict) for r in rows)

def test_parse_state_tolerates_missing_optional_fields():
    state = exo_client.parse_state({})
    assert state == exo_client.ClusterState(None, (), (), ())

def test_client_raises_exo_unavailable_when_nothing_listens(monkeypatch):
    client = exo_client.ExoClient("http://127.0.0.1:9")
    with pytest.raises(exo_client.ExoUnavailable, match="exo is not running on this Mac"):
        client.state()
```

- [ ] **Step 2:** Run `python -m pytest tests/test_cluster_exo_client.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3:** Implement `exo_client.py`.
  - Map fields per the Task 0 findings.
  - Unknown or missing fields become the dataclass defaults. Never raise `KeyError`.
  - Use `LoopbackJsonClient` for JSON calls.
  - `await_instance` uses lazily imported `requests` with `stream=True` on `/instance/await?model_id=…&timeout_seconds=…`. It returns `True` on an SSE `"type": "ready"` and `False` on `"timeout"`.
  - A connection refusal or timeout raises `ExoUnavailable`.
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(cluster): add exo loopback client and state parsers`

---

### Task 2: Preflight — macOS version, exo install, RDMA

**Files:**
- Create: `src/omm/cluster/preflight.py`
- Test: `tests/test_cluster_preflight.py`

**Interfaces:**
- Consumes: the Task 0 app path and `rdma_ctl` fixtures
- Produces:
  - `MIN_MACOS = (26, 2)`
  - `EXO_APP_PATH: Path` (from Task 0)
  - `class ClusterError(RuntimeError)`. The CLI prints it plainly. It is the shared error type for every cluster module.
  - `macos_version() -> tuple[int, ...] | None`
  - `require_supported_macos() -> None`. Raises `ClusterError`.
  - `exo_installed() -> bool`
  - `install_exo(on_output: Callable[[str], None], *, run=subprocess.run) -> None`
  - `rdma_status(*, run=subprocess.run) -> Literal["enabled","disabled","unavailable"]`
  - `RDMA_GUIDE: tuple[str, ...]`

- [ ] **Step 1: Write failing tests**

```python
def test_rdma_status_parses_real_outputs():
    for name, expected in (("rdma_ctl_status_enabled.txt", "enabled"), ("rdma_ctl_status_disabled.txt", "disabled")):
        out = (FIX / name).read_text(encoding="utf-8")
        run = lambda *a, **k: subprocess.CompletedProcess(a, 0, out, "")
        assert preflight.rdma_status(run=run) == expected

def test_rdma_status_unavailable_when_binary_missing(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert preflight.rdma_status() == "unavailable"

def test_require_supported_macos_rejects_26_1(monkeypatch):
    monkeypatch.setattr(preflight, "macos_version", lambda: (26, 1))
    with pytest.raises(preflight.ClusterError, match="26.2"):
        preflight.require_supported_macos()

def test_install_exo_runs_exact_brew_cask_command(monkeypatch):
    calls = []
    monkeypatch.setattr(shutil, "which", lambda name: "/opt/homebrew/bin/brew" if name == "brew" else None)
    preflight.install_exo(lambda line: None, run=lambda argv, **k: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    assert calls == [["/opt/homebrew/bin/brew", "install", "--cask", "exo"]]

def test_install_exo_without_brew_raises_with_manual_url(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(preflight.ClusterError, match="https://assets.exolabs.net/EXO-latest.dmg"):
        preflight.install_exo(lambda line: None)
```

- [ ] **Step 2:** Run `python -m pytest tests/test_cluster_preflight.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement.
  - Build `macos_version` from `platform.mac_ver()[0]`. It returns `None` off macOS.
  - The `RDMA_GUIDE` lines copy the six README steps verbatim, plus one more line: `Every Mac must run the exact same macOS build, and each Mac must be cabled to every other Mac with TB5 cables.`
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(cluster): add macOS, exo install and RDMA preflight checks`

---

### Task 3: Employee token store

**Files:**
- Create: `src/omm/cluster/tokens.py`
- Test: `tests/test_cluster_tokens.py`

**Interfaces:**
- Produces:
  - `TOKEN_PREFIX = "omm_"`
  - `class TokenError(ClusterError)`
  - `TokenRecord(name: str, created_at: str)`
  - `tokens_path() -> Path`. Returns `config.OMM_HOME / "cluster" / "tokens.json"`.
  - `add_token(name: str) -> str`. Returns the plaintext token, shown only once.
  - `list_tokens() -> list[TokenRecord]`. Raises `TokenError` on a corrupt file.
  - `revoke_token(name: str) -> bool`
  - `verify(presented: str) -> str | None`. Returns the token name.
  - `class TokenStore`, with `.verify(presented: str) -> str | None`. It caches the parsed file and reloads when `st_mtime_ns` changes.
- File schema: `{"version": 1, "tokens": [{"name": str, "sha256": hex, "created_at": iso8601Z}]}`.
  - Write with `atomic_write_text`, then `chmod 0o600`.
  - Generate tokens as `TOKEN_PREFIX + secrets.token_urlsafe(32)`.
  - Valid names match `^[\w.-]{1,40}$`. Unicode `\w` is allowed, so Korean names work.

- [ ] **Step 1: Write failing tests**

```python
def test_add_then_verify_returns_name_and_never_stores_plaintext(isolated_omm_home):
    token = tokens.add_token("영업팀-kim")
    assert token.startswith("omm_")
    assert tokens.verify(token) == "영업팀-kim"
    assert token not in tokens.tokens_path().read_text(encoding="utf-8")
    assert stat.S_IMODE(tokens.tokens_path().stat().st_mode) == 0o600

def test_revoked_token_is_rejected_by_a_live_store(isolated_omm_home):
    token = tokens.add_token("kim")
    store = tokens.TokenStore()
    assert store.verify(token) == "kim"
    tokens.revoke_token("kim")
    assert store.verify(token) is None

def test_duplicate_and_invalid_names_raise(isolated_omm_home):
    tokens.add_token("kim")
    with pytest.raises(tokens.TokenError): tokens.add_token("kim")
    with pytest.raises(tokens.TokenError): tokens.add_token("bad name")

def test_corrupt_file_fails_closed_and_is_not_overwritten(isolated_omm_home):
    token = tokens.add_token("kim")
    tokens.tokens_path().write_text("{not json", encoding="utf-8")
    assert tokens.verify(token) is None
    assert tokens.TokenStore().verify(token) is None
    with pytest.raises(tokens.TokenError, match="tokens.json"): tokens.list_tokens()
    assert tokens.tokens_path().read_text(encoding="utf-8") == "{not json"
```

- [ ] **Step 2:** Run `python -m pytest tests/test_cluster_tokens.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement.
  - Compare with `hmac.compare_digest` on sha256 hex digests.
  - `add_token` and `revoke_token` hold `atomic.locked(OMM_HOME / "locks" / "cluster-tokens")`.
  - If the file is corrupt, `add_token` raises `TokenError` as well. It never overwrites the file.
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(cluster): add hashed per-employee token store`

---

### Task 4: Authenticating streaming gateway

**Files:**
- Create: `src/omm/cluster/gateway.py`
- Test: `tests/test_cluster_gateway.py`

**Interfaces:**
- Consumes: `tokens.TokenStore` (Task 3); `ALLOWED_ROUTES` from the Task 0 Step 6 findings
- Produces:
  - `GATEWAY_PORT = 52440`
  - `UPSTREAM = "http://127.0.0.1:52415"`
  - `MAX_BODY_BYTES = 16 * 1024 * 1024`
  - `ALLOWED_ROUTES: frozenset[tuple[str, str]]`. The starting value is `{("POST","/v1/messages"),("POST","/v1/chat/completions"),("POST","/v1/responses"),("GET","/v1/models")}`. Replace it with the Task 0 union.
  - `class GatewayServer(ThreadingHTTPServer)`, created with `__init__(self, host: str = "0.0.0.0", port: int = GATEWAY_PORT, *, upstream: str = UPSTREAM, store: TokenStore | None = None)`
  - `run_gateway() -> int`. It blocks; this is the body of `_cluster-gateway-run`.
- Behaviour:
  - Accept the token from `Authorization: Bearer <t>` or `x-api-key: <t>`.
  - Strip both headers, plus `Host` and hop-by-hop headers, before forwarding.
  - Match the path only; ignore the query string.
  - Stream the upstream body back chunk by chunk. Never buffer a whole response.
  - Error bodies are Anthropic-shaped: `{"type":"error","error":{"type": T,"message": M}}`.
    - 401 `authentication_error`: `omm cluster: missing or invalid token`
    - 404 `not_found_error`: `omm cluster: this path is not served by the gateway`
    - 413 `invalid_request_error`: `omm cluster: request body is larger than 16 MiB`
    - 502 `api_error`: `omm cluster: exo is not running on this Mac`
  - Log to `logging.getLogger("omm.cluster.gateway")` with these fields only: method, path, status, token name, duration_ms.

- [ ] **Step 1: Write failing tests.** Run a fake upstream `ThreadingHTTPServer` on `127.0.0.1:0` that records the headers and body it receives and returns a 3-chunk SSE stream. Run the gateway on `127.0.0.1:0` in a thread.

```python
def test_valid_bearer_is_proxied_and_streamed(gateway, upstream, token):
    resp = post(gateway, "/v1/messages", headers={"Authorization": f"Bearer {token}"}, body=b'{"x":1}')
    assert resp.status == 200 and resp.read() == upstream.SSE_BYTES
    assert "authorization" not in {k.lower() for k in upstream.last_headers}
    assert upstream.last_body == b'{"x":1}'

def test_x_api_key_is_accepted(gateway, token):
    assert post(gateway, "/v1/messages", headers={"x-api-key": token}).status == 200

@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer omm_wrong"}])
def test_missing_or_bad_token_is_401_and_never_reaches_upstream(gateway, upstream, headers):
    resp = post(gateway, "/v1/messages", headers=headers)
    assert resp.status == 401
    assert json.loads(resp.read())["error"]["type"] == "authentication_error"
    assert upstream.calls == 0

@pytest.mark.parametrize("method,path", [("POST", "/instance"), ("GET", "/state"), ("DELETE", "/instance/x"), ("POST", "/models/add")])
def test_admin_paths_are_404_even_with_a_valid_token(gateway, upstream, token, method, path):
    assert request(gateway, method, path, headers={"Authorization": f"Bearer {token}"}).status == 404
    assert upstream.calls == 0

def test_oversized_body_is_413(gateway, token):
    assert post(gateway, "/v1/messages", headers={"Authorization": f"Bearer {token}", "Content-Length": str(16 * 1024 * 1024 + 1)}, body=b"").status == 413

def test_upstream_down_is_502_api_error(gateway_without_upstream, token):
    resp = post(gateway_without_upstream, "/v1/messages", headers={"Authorization": f"Bearer {token}"})
    assert resp.status == 502 and "exo is not running" in resp.read().decode()

def test_log_records_contain_no_body(gateway, token, caplog):
    post(gateway, "/v1/messages", headers={"Authorization": f"Bearer {token}"}, body=b'{"secret":"payroll"}')
    assert "payroll" not in caplog.text and token not in caplog.text
```

- [ ] **Step 2:** Run `python -m pytest tests/test_cluster_gateway.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement with `http.client.HTTPConnection` to the upstream. Use `resp.read1(65536)` in a loop, writing each chunk and flushing. If the upstream sends no `Content-Length`, use chunked transfer encoding.
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(cluster): add token-checked streaming gateway in front of exo`

---

### Task 5: Gateway LaunchAgent and hidden run command

**Files:**
- Create: `src/omm/cluster/service.py`
- Modify: `src/omm/cli.py`. Add the hidden `_cluster-gateway-run` command and register it in the 5 skip sets (Global Constraints).
- Test: `tests/test_cluster_service.py`

**Interfaces:**
- Consumes: `gateway.run_gateway` (Task 4)
- Produces:
  - `LAUNCHD_LABEL = "com.omm.cluster-gateway"`
  - `service_argv() -> list[str]`. Same frozen/non-frozen rule as `watch_service.service_argv`, with subcommand `_cluster-gateway-run`.
  - `install(*, run=subprocess.run) -> None`
  - `uninstall(*, run=subprocess.run) -> None`
  - `is_installed() -> bool`
  - `is_running(*, run=subprocess.run) -> bool`
- The plist goes in `~/Library/LaunchAgents/com.omm.cluster-gateway.plist`. It sets `RunAtLoad` true, `KeepAlive` true, and `ThrottleInterval` 10. Build it with `plistlib.dumps`.
- `install()` is idempotent: bootout if loaded, then rewrite the plist, then `launchctl bootstrap gui/<uid> <plist>`.

- [ ] **Step 1: Write failing tests**

```python
def test_plist_runs_hidden_gateway_command_with_keepalive(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    service.install(run=fake_run_ok)
    data = plistlib.loads((tmp_path / "Library/LaunchAgents/com.omm.cluster-gateway.plist").read_bytes())
    assert data["ProgramArguments"][-1] == "_cluster-gateway-run"
    assert data["KeepAlive"] is True and data["ThrottleInterval"] == 10

def test_install_twice_boots_out_before_bootstrap(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    calls = []
    run = lambda argv, **k: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", "")
    service.install(run=run); service.install(run=run)
    assert [c[1] for c in calls if c[0] == "launchctl"] == ["bootout", "bootstrap", "bootout", "bootstrap"]

def test_hidden_command_is_in_all_five_skip_sets():
    for name in ("_SKIP_UPDATE_CHECK_SUBCOMMANDS", "_SKIP_ONBOARDING_SUBCOMMANDS", "_SKIP_AUTO_IMPORT_SUBCOMMANDS",
                 "_SKIP_QUEUED_UPLOAD_SUBCOMMANDS", "_INTERNAL_SUBCOMMANDS"):
        assert "_cluster-gateway-run" in getattr(cli, name)
```

- [ ] **Step 2:** Run `python -m pytest tests/test_cluster_service.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement. The CLI command is `@app.command(name="_cluster-gateway-run", hidden=True)` and calls `raise typer.Exit(gateway.run_gateway())`. Import `omm.cluster.gateway` inside the function.
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(cluster): run the gateway as a per-user LaunchAgent`

---

### Task 6: pf firewall anchor

**Files:**
- Create: `src/omm/cluster/firewall.py`
- Test: `tests/test_cluster_firewall.py`

**Interfaces:**
- Consumes: Task 0 `EXO_PORTS`, `PF_ANCHOR`, and the `listallhardwareports.txt` fixture
- Produces:
  - `EXO_PORTS: tuple[int, ...]`
  - `PF_ANCHOR = "com.apple/omm-cluster"`. Confirm or replace this in Task 0 Step 3.
  - `RULES_PATH = Path("/Library/Application Support/omm/cluster-pf.conf")`
  - `DAEMON_LABEL = "com.omm.cluster-pf"`
  - `DAEMON_PLIST = Path("/Library/LaunchDaemons/com.omm.cluster-pf.plist")`
  - `thunderbolt_interfaces(text: str | None = None, *, run=subprocess.run) -> list[str]`. Device names of `Hardware Port: Thunderbolt <n>` entries. Excludes `Thunderbolt Bridge`. Sorted.
  - `render_rules(exo_ports: Sequence[int], tb_ifaces: Sequence[str]) -> str`
  - `apply(*, run=subprocess.run) -> None`
  - `remove(*, run=subprocess.run) -> None`
  - `status(*, run=subprocess.run) -> Literal["active", "missing", "drift"]`
- `render_rules` output is fixed copy:

```text
# managed by omm cluster - do not edit
exo_ports = "{ 52415, <rest of EXO_PORTS> }"
pass in quick on lo0 proto { tcp udp } to port $exo_ports
pass in quick on { <tb_ifaces space-joined> } proto { tcp udp } to port $exo_ports
block drop in quick proto { tcp udp } to port $exo_ports
```

  Omit the second `pass` line when `tb_ifaces` is empty.

- `apply()` renders rules and the daemon plist into a `tempfile.mkdtemp()` directory, then makes exactly one sudo call:
  - argv is `["sudo", "/bin/sh", "-c", SCRIPT, "omm-cluster-pf", rules_tmp, plist_tmp]`, where `SCRIPT` is a module constant that only uses `"$1"`/`"$2"`.
  - The script installs both files with mode 644, runs `launchctl bootout system/com.omm.cluster-pf` (ignoring failure), then `launchctl bootstrap system "$DAEMON_PLIST"`.
  - The daemon's `ProgramArguments` are `["/sbin/pfctl", "-E", "-a", PF_ANCHOR, "-f", str(RULES_PATH)]`, with `RunAtLoad` true.
- `status()` checks two things:
  - It compares `RULES_PATH` contents to `render_rules(EXO_PORTS, thunderbolt_interfaces())`. A missing file means `missing`; different contents mean `drift`.
  - `sudo -n pfctl -a PF_ANCHOR -sr` must list the block rule. If sudo needs a password, skip this check and use the file comparison alone.

- [ ] **Step 1: Write failing tests**

```python
def test_thunderbolt_interfaces_from_real_fixture_excludes_bridge():
    ifaces = firewall.thunderbolt_interfaces((FIX / "listallhardwareports.txt").read_text(encoding="utf-8"))
    assert ifaces and all(i.startswith("en") for i in ifaces) and "bridge0" not in ifaces

def test_render_rules_exact_text():
    assert firewall.render_rules((52415, 7447), ("en5", "en6")) == (
        "# managed by omm cluster - do not edit\n"
        'exo_ports = "{ 52415, 7447 }"\n'
        "pass in quick on lo0 proto { tcp udp } to port $exo_ports\n"
        "pass in quick on { en5 en6 } proto { tcp udp } to port $exo_ports\n"
        "block drop in quick proto { tcp udp } to port $exo_ports\n"
    )

def test_render_rules_without_thunderbolt_only_allows_loopback():
    assert "{ }" not in firewall.render_rules((52415,), ())

def test_apply_uses_one_sudo_call_with_paths_as_arguments():
    calls = []
    firewall.apply(run=lambda argv, **k: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    sudo = [c for c in calls if c[0] == "sudo"]
    assert len(sudo) == 1 and sudo[0][:3] == ["sudo", "/bin/sh", "-c"] and sudo[0][3] == firewall.SCRIPT

def test_status_reports_drift_when_thunderbolt_set_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(firewall, "RULES_PATH", tmp_path / "pf.conf")
    firewall.RULES_PATH.write_text(firewall.render_rules(firewall.EXO_PORTS, ("en5",)), encoding="utf-8")
    monkeypatch.setattr(firewall, "thunderbolt_interfaces", lambda *a, **k: ["en5", "en6"])
    assert firewall.status(run=fake_sudo_needs_password) == "drift"
```

- [ ] **Step 2:** Run `python -m pytest tests/test_cluster_firewall.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(cluster): confine exo ports to loopback and Thunderbolt with a pf anchor`

---

### Task 7: Cluster model recommendation and placement choice

**Files:**
- Create: `src/omm/cluster/recommend.py`
- Test: `tests/test_cluster_recommend.py`

**Interfaces:**
- Consumes: `ExoModel`, `Placement`, `ClusterState` (Task 1)
- Produces:
  - `HEADROOM = 0.85`
  - `Candidate(model_id: str, size_bytes: int, placement: Placement, memory_used_fraction: float)` (frozen)
  - `choose_placement(placements: Sequence[Placement]) -> Placement | None`
  - `prefilter(models: Sequence[ExoModel], state: ClusterState) -> list[ExoModel]`
  - `rank(models: Sequence[ExoModel], previews: Mapping[str, Sequence[Placement]], state: ClusterState, limit: int = 5) -> list[Candidate]`
- Rules:
  - `choose_placement` keeps rows with `error is None`. It prefers `sharding == "Pipeline"`, then the smallest `max(memory_delta_by_node.values())`.
  - `prefilter` keeps models whose `size_bytes` is known and at most `HEADROOM × sum(memory_available_bytes)`. This avoids calling previews for every model.
  - `rank` keeps models whose chosen placement uses at most `HEADROOM × available` in total. It sorts by `size_bytes` descending, because the goal is the biggest model that fits.

- [ ] **Step 1: Write failing tests.** Build `ClusterState` and `Placement` objects directly; do not use fixtures.

```python
def test_choose_placement_prefers_pipeline_then_smallest_peak():
    p = [pl("Tensor", {"a": 10, "b": 10}), pl("Pipeline", {"a": 30, "b": 5}), pl("Pipeline", {"a": 15, "b": 15}), pl("Pipeline", {"a": 1}, error="no fit")]
    assert choose_placement(p) == p[2]

def test_rank_drops_models_above_headroom_and_sorts_biggest_first():
    state = st(available=[64 * GiB] * 4)              # 256 GiB available
    models = [m("small", 20 * GiB), m("big", 200 * GiB), m("huge", 230 * GiB)]
    previews = {"small": [pl("Pipeline", {"a": 20 * GiB})], "big": [pl("Pipeline", {"a": 50 * GiB, "b": 50 * GiB, "c": 50 * GiB, "d": 50 * GiB})],
                "huge": [pl("Pipeline", {"a": 58 * GiB, "b": 58 * GiB, "c": 58 * GiB, "d": 58 * GiB})]}
    assert [c.model_id for c in rank(models, previews, state)] == ["big", "small"]   # huge = 232/256 > 0.85

def test_prefilter_skips_unknown_sizes():
    assert prefilter([m("x", None)], st(available=[64 * GiB])) == []
```

- [ ] **Step 2:** Run `python -m pytest tests/test_cluster_recommend.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(cluster): recommend the largest exo model that fits the cluster`

---

### Task 8: `omm cluster` command group, settings, docs

**Files:**
- Create: `src/omm/cluster/settings.py`
- Modify: `src/omm/cli.py`. Add `cluster_app` and `cluster_token_app` after `engine_app` (~line 751), with the commands below.
- Modify: `README.md` `## Usage`, `docs/commands.json` (regenerated), `PRIVACY.md` (new "Cluster gateway" section), `CLAUDE.md` (new **Cluster.** architecture paragraph)
- Test: `tests/test_cli_cluster.py`

**Interfaces:**
- Consumes: Tasks 1–7
- Produces in `settings.py`:
  - `ClusterSettings(role: Literal["head","member"], namespace: str, gateway_port: int, known_nodes: tuple[str, ...])`
  - `settings_path() -> Path`. Returns `OMM_HOME/"cluster"/"cluster.json"`.
  - `load() -> ClusterSettings | None`
  - `save(s: ClusterSettings) -> None`
  - `new_namespace() -> str`. Returns `"omm-" + secrets.token_hex(4)`.
  - `NAMESPACE_RE = re.compile(r"^omm-[0-9a-f]{8}$")`
  - `set_exo_namespace(namespace: str, *, run=subprocess.run) -> None`. Uses the Task 0 Step 4 mechanism.
- Commands. Help strings are short and end without a period inside words (click truncation):
  - `omm cluster init`. Runs these steps in order:
    1. `require_supported_macos`
    2. install exo if missing
    3. reuse the namespace from existing settings, else `new_namespace()`; then `set_exo_namespace`
    4. show `rdma_status`; print `RDMA_GUIDE` when not `enabled` and continue
    5. `firewall.apply()`
    6. `service.install()`
    7. save with `role="head"`
    8. print `Run this on every other Mac: omm cluster join <namespace>` and `Then give each employee a token: omm cluster token add <name>`

    If settings already exist with `role="member"`, exit 2 with: `this Mac already joined a cluster as a member; run omm cluster leave first`.
  - `omm cluster join NAMESPACE`. Validate against `NAMESPACE_RE` (exit 2 on mismatch). Run init steps 1–5, then save with `role="member"`. Same idempotency as `init`.
  - `omm cluster status [--json]`. The JSON shape is identical whether or not problems exist:

```json
{"role": "head|member|null", "namespace": "str|null",
 "exo": {"reachable": true, "nodes": [{"name": "", "memory_total_bytes": 0, "memory_available_bytes": 0, "rdma": "", "os_version": null}], "instances": [{"model_id": "", "sharding": "", "node_count": 0}]},
 "firewall": "active|missing|drift", "gateway": "running|stopped|not_head", "problems": ["..."]}
```

    `problems` contains these exact strings:
    - `exo is not running on this Mac`
    - `node <name> missing (seen before)`, for each id in `known_nodes` but not in state
    - `macOS versions differ: <name>=<v>, ...`
    - `RDMA disabled on <name>`
    - `firewall rules are <missing|drift>; run omm cluster <init|join> again`
    - `gateway is not running`

    Every status run on a reachable cluster merges the seen node ids into `known_nodes`. Exit code is 0.
  - `omm cluster model [MODEL_ID] [--unload]`
    - With no arg: `prefilter`, previews for each remaining model, `rank`. Print a table with columns #, model, size, memory used %.
    - With an arg: `choose_placement`, `create_instance`, then `await_instance(timeout_seconds=3600)` behind a spinner. Print `Serving <model> at http://<head LAN IPv4>:<gateway_port>`.
    - `--unload` deletes the instances whose `model_id` matches.
    - Exits 1 on `ExoUnavailable` or when no placement fits. Message for no fit: `<model> does not fit in this cluster's free memory`.
  - `omm cluster token add NAME` prints the token once, with the line `Store it now - omm cannot show it again.`. `token list` prints name and created_at. `token revoke NAME` exits 1 if the name is unknown. All three exit 2 on a member Mac: `tokens are managed on the head Mac`.
  - `omm cluster leave [--yes]`. Confirm first, then: `firewall.remove()`, `service.uninstall()` on the head only, delete `OMM_HOME/cluster/cluster.json` (keep `tokens.json`), and print `exo itself stays installed; remove it from its menu bar icon -> Advanced -> Uninstall`.

- [ ] **Step 1: Write failing tests.** Use `typer.testing.CliRunner`. Monkeypatch `preflight`, `firewall`, `service`, `ExoClient` (feed it parsed fixtures), and `platform.system` to `"Darwin"`.

```python
def test_init_twice_reuses_namespace_and_reapplies(isolated_omm_home, cluster_fakes):
    first = runner.invoke(cli.app, ["cluster", "init"]); second = runner.invoke(cli.app, ["cluster", "init"])
    assert first.exit_code == second.exit_code == 0
    ns = settings.load().namespace
    assert f"omm cluster join {ns}" in first.output and f"omm cluster join {ns}" in second.output
    assert cluster_fakes.firewall_apply_calls == 2 and cluster_fakes.service_install_calls == 2

def test_join_rejects_bad_namespace(isolated_omm_home, cluster_fakes):
    assert runner.invoke(cli.app, ["cluster", "join", "anything"]).exit_code == 2

def test_init_on_member_is_refused(isolated_omm_home, cluster_fakes):
    runner.invoke(cli.app, ["cluster", "join", "omm-0a1b2c3d"])
    r = runner.invoke(cli.app, ["cluster", "init"])
    assert r.exit_code == 2 and "omm cluster leave" in r.output

def test_status_json_lists_missing_node_and_os_mismatch(isolated_omm_home, cluster_fakes):
    runner.invoke(cli.app, ["cluster", "init"])
    runner.invoke(cli.app, ["cluster", "status", "--json"])              # records 4 known nodes
    cluster_fakes.drop_node("mac-d"); cluster_fakes.set_os("mac-b", "26.3")
    data = json.loads(runner.invoke(cli.app, ["cluster", "status", "--json"]).output)
    assert "node mac-d missing (seen before)" in data["problems"]
    assert any(p.startswith("macOS versions differ:") for p in data["problems"])

def test_status_when_exo_is_down_has_same_shape(isolated_omm_home, cluster_fakes):
    cluster_fakes.exo_down()
    data = json.loads(runner.invoke(cli.app, ["cluster", "status", "--json"]).output)
    assert data["exo"] == {"reachable": False, "nodes": [], "instances": []}
    assert "exo is not running on this Mac" in data["problems"]

def test_model_without_fit_exits_1(isolated_omm_home, cluster_fakes):
    cluster_fakes.previews_all_error()
    r = runner.invoke(cli.app, ["cluster", "model", "mlx-community/huge"])
    assert r.exit_code == 1 and "does not fit" in r.output

def test_token_commands_refused_on_member(isolated_omm_home, cluster_fakes):
    runner.invoke(cli.app, ["cluster", "join", "omm-0a1b2c3d"])
    assert runner.invoke(cli.app, ["cluster", "token", "add", "kim"]).exit_code == 2

def test_non_macos_exits_2(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    assert runner.invoke(cli.app, ["cluster", "status"]).exit_code == 2
```

- [ ] **Step 2:** Run `python -m pytest tests/test_cli_cluster.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement `settings.py` and the commands. Import `omm.cluster.*` inside each command function (startup speed).
- [ ] **Step 4:** Run `python -m pytest tests/test_cli_cluster.py tests/test_cluster_*.py -q`. Expected: PASS.
- [ ] **Step 5:** Update the docs:
  1. Regenerate the command reference with `python scripts/export_command_reference.py`.
  2. Add the README Usage lines: `omm cluster init  # make this Mac the cluster head`, `omm cluster join <namespace>  # add this Mac to a cluster`, `omm cluster status [--json]  # nodes, memory, RDMA, firewall, gateway`, `omm cluster model [model] [--unload]  # recommend or load a model on the cluster`, `omm cluster token add|list|revoke  # employee access tokens`, `omm cluster leave  # remove this Mac's cluster setup`.
  3. Add the PRIVACY.md section. It states that nothing leaves the office, that the gateway logs no bodies, and that tokens are stored hashed.
  4. Add the CLAUDE.md **Cluster.** paragraph covering the package layout, pf anchor, gateway, and spec path.

  Then run `python scripts/check_docs_sync.py`. Expected: exit 0.
- [ ] **Step 6:** Run the full suite with `python -m pytest -q`. Expected: only the known pre-existing `FORCE_COLOR` failures, with no new failures. `tests/test_cli_update.py` can wipe the real `~/.omm/src` (CLAUDE.md). Run the suite with `HOME` pointed at a scratch dir, or deselect that file.
- [ ] **Step 7:** Commit: `feat(cluster): add omm cluster init/join/status/model/token/leave`. Use `git add` on the cluster package, the tests, `cli.py`, README.md, docs/commands.json, PRIVACY.md, and CLAUDE.md.

---

### Task 9: Real-hardware acceptance (#503 완료 기준)

Install omm from this branch on the 4 Mac minis with an isolated pipx install: `pipx install --force --editable <clone> --suffix _verify`. Do not use anyone's daily-driver omm.

- [ ] **Step 1:** Run `omm_verify cluster init` on mac-a, then `omm_verify cluster join <ns>` on mac-b, mac-c, and mac-d. Then run `omm_verify cluster status`. Expected: 4 nodes, RDMA `enabled` on all 4, firewall `active`, gateway `running`, and no problems.
- [ ] **Step 2:** Run `omm_verify cluster model`, then load the top candidate that exceeds one Mac's memory (> 64 GiB). Expected: the `Serving … at http://<ip>:52440` line.
- [ ] **Step 3:** From a non-cluster laptop, run `curl` against `/v1/messages` 3 times: with no token, with a revoked token, and with a valid token. Expected: 401, 401, then a streamed 200. Point Claude Code at the gateway with `ANTHROPIC_BASE_URL=http://<ip>:52440` and `ANTHROPIC_AUTH_TOKEN=<token>`, and finish one tool-using task.
- [ ] **Step 4:** From the same laptop, run `nmap -p 1-65535 <each mac ip>`. Expected: no exo port open. Only 52440 on mac-a, plus whatever was open before the cluster was set up.
- [ ] **Step 5:** Measure tok/s for the same model and prompt with RDMA enabled vs disabled (`rdma_ctl disable` in Recovery on all 4). Record model, quant, node count, and sharding.
- [ ] **Step 6:** Unplug mac-d's cables. Expected: `status` lists `node mac-d missing (seen before)`.
- [ ] **Step 7:** Record all results in a "Real-hardware acceptance" section of the spike findings doc and commit it. Then clean up: `omm_verify cluster leave --yes` on all 4 Macs, then `pipx uninstall omm_verify`.
