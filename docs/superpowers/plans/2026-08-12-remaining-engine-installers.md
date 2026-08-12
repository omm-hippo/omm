# Remaining Engine Installers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `linker.install_engine()` (currently Ollama-only) to also silently, automatically install the other 6 engines the onboarding wizard already lists: LM Studio, Jan, AnythingLLM, Msty, text-generation-webui, KoboldCpp. No wizard UI changes — `onboarding.py` already drives every engine through `linker.has_automated_installer(key)`/`linker.install_engine(key)`; this plan only adds branches to those two functions.

**Architecture:** Same shape as the existing Ollama installer: each engine gets an `_install_<engine>()` function in `linker.py` returning `EngineInstallResult`, dispatched from `install_engine()`'s if/elif. Three engines (Jan, AnythingLLM, Msty) share one new helper, `_install_via_package_manager()`, since they all follow the same "brew cask (mac) / winget (Windows) [/ flatpak (Linux)], else unsupported_platform with a manual link" shape. LM Studio gets its own curl/irm-script installer (same shape as Ollama's, kept separate rather than shared — Ollama's function is already shipped/reviewed and out of scope to touch). KoboldCpp and text-generation-webui have no package manager option anywhere, so both download a release asset directly via `curl` and place it where the existing heuristic detection (`find_koboldcpp_binary`/`find_textgenwebui_root`) already looks — text-generation-webui's detection function needs a real update, since it currently only recognizes the old git-clone layout and the new portable-build layout is structurally different (verified directly against a real release archive, not guessed).

**Tech Stack:** Python, `subprocess.Popen` streamed via the existing `_stream_subprocess()` helper (from Ollama's implementation), `requests` (already a dependency) for a small GitHub API JSON call, stdlib `zipfile`/`tarfile` for portable-archive extraction.

## Research findings (grounding for every URL/package name below — do not deviate without re-verifying)

All package names, IDs, and URLs below were verified live on 2026-08-12 (Homebrew cask API, winget-pkgs manifests, GitHub Releases API) — not guessed from training data. Where something couldn't be verified, it's marked unsupported/manual-link instead of guessed.

- **LM Studio**: official headless install script — `curl -fsSL https://lmstudio.ai/install.sh | bash` (macOS/Linux), `irm https://lmstudio.ai/install.ps1 | iex` (Windows PowerShell). Installs the `lms` CLI + daemon (the "llmster" headless core), not necessarily the GUI app — `is_lmstudio_installed()` currently only checks for the GUI app bundle/data dir and must also check for the `lms` CLI (via the existing `_lms_cli_path()` helper) or a real headless install will never be detected as installed.
- **Jan**: Homebrew cask `jan` (macOS), winget `Jan.Jan` (Windows), Flathub `ai.jan.Jan` (Linux, `flatpak install -y flathub ai.jan.Jan`).
- **AnythingLLM**: Homebrew cask `anythingllm` (macOS), winget `MintplexLabs.AnythingLLM` (Windows). No Linux automation — the official Linux installer script is interactive (prompts for an AppArmor profile with sudo) with no documented non-interactive flag; automating it is out of scope, same risk category as text-generation-webui's old git-clone path was before this plan. Linux reports `unsupported_platform` with a link to `https://docs.anythingllm.com/installation-desktop/linux`.
- **Msty**: Homebrew cask `mstystudio` (macOS) — note the app rebranded to "Msty Studio" in 2026; the old `msty` cask and the winget package `CloudStack.Msty` both target the deprecated legacy app and must not be used. No verified Windows or Linux automation (no current winget package, no deb/snap/flatpak) — both report `unsupported_platform` with a link to `https://msty.ai/download`.
- **KoboldCpp**: no package manager anywhere. Direct download from GitHub's stable "latest" URL (`https://github.com/LostRuins/koboldcpp/releases/latest/download/<asset>`, verified working, no version parsing needed): `koboldcpp-mac-arm64` (macOS arm64 only — no Intel Mac build exists, confirmed), `koboldcpp-linux-x64` (Linux x86_64), `koboldcpp.exe` (Windows).
- **text-generation-webui**: repo renamed to `oobabooga/text-generation-webui` → `oobabooga/textgen` (old URL still resolves/redirects). No package manager. Portable prebuilt archives verified via `gh api repos/oobabooga/text-generation-webui/releases/latest` (current tag `v4.9`) — real asset list:
  ```
  textgen-portable-4.9-linux-cpu.tar.gz
  textgen-portable-4.9-linux-cuda12.4.tar.gz
  textgen-portable-4.9-linux-cuda13.1.tar.gz
  textgen-portable-4.9-linux-rocm7.2.tar.gz
  textgen-portable-4.9-linux-vulkan.tar.gz
  textgen-portable-4.9-linux-arm64-cuda13.1.tar.gz
  textgen-portable-4.9-macos-arm64.tar.gz
  textgen-portable-4.9-macos-x86_64.tar.gz
  textgen-portable-4.9-windows-cpu.zip
  textgen-portable-4.9-windows-cuda12.4.zip
  textgen-portable-4.9-windows-cuda13.1.zip
  textgen-portable-4.9-windows-rocm7.2.zip
  textgen-portable-4.9-windows-vulkan.zip
  ```
  The version number (`4.9`) is embedded in every filename, so the URL can't be hardcoded — the exact current filename must come from querying the GitHub Releases API at install time, not from a static "latest/download" guess. Verified directly against the real archive bytes (HTTP range request on the central directory, not documentation): the extracted top-level folder is named `textgen-<version>` (e.g. `textgen-4.9`, no "text-generation-webui"/"oobabooga" substring), contains `user_data/models/` (same relative path the existing linking code already assumes — no change needed there) and `app/server.py` (not `server.py` at the root, and no `one_click.py` at all — the existing detection function's marker-file check only recognizes the old git-clone layout and will never match a portable install).

## Global Constraints

- Every new `_install_<engine>()` function returns `linker.EngineInstallResult(key, status, message)` exactly like `_install_ollama` — `status` one of `"installed"`, `"failed"`, `"unsupported_platform"`.
- `linker.has_automated_installer(key)` is the single source of truth for which engines the wizard offers to auto-install (established in the previous plan specifically to avoid an uncaught `NotImplementedError` mid-wizard) — every engine this plan adds must be added there, in the same commit as its `install_engine()` branch, never one without the other.
- Reuse `linker._stream_subprocess(args, on_output)` for every subprocess call that streams installer output — don't reinvent the Popen-and-iterate pattern per engine.
- No real network calls in tests — every test mocks `subprocess.Popen` (via `linker.subprocess.Popen`) and, for text-generation-webui, the `requests` call that queries the GitHub API.
- No wizard UI changes (`src/omm/onboarding.py` is out of scope for every task in this plan) — the checklist and install-orchestration logic already dispatch generically through `has_automated_installer`/`install_engine`.
- Tests that need `platform.system()`/`platform.machine()` control monkeypatch `linker.platform.system`/`linker.platform.machine` directly (matches the existing convention in `tests/test_linker_new_engines.py` and the Ollama installer's own tests).
- The `tests/conftest.py` `_no_real_engine_writes` fixture already redirects `jan_app_dir`, `anythingllm_app_dir`, `mstystudio_app_dir`, `_HEURISTIC_SEARCH_ROOTS`, and `_APP_BUNDLE_SEARCH_ROOTS` to a throwaway `tmp_path` for every test automatically — new tests for Jan/AnythingLLM/Msty/KoboldCpp/text-generation-webui detection don't need to redo this, but installer tests that actually write a file (KoboldCpp, text-generation-webui) must still use `tmp_path`/`monkeypatch` to avoid touching a real machine.

---

### Task 1: LM Studio installer + detection fix

**Files:**
- Modify: `src/omm/linker.py` (`is_lmstudio_installed` at line ~116; add `_install_lmstudio` near `_install_ollama`; add `"lmstudio"` branches to `install_engine`/`has_automated_installer`)
- Test: Create `tests/test_linker_install_engine.py` additions (same file Task 2 of the previous plan created)

**Interfaces:**
- Consumes: `linker._stream_subprocess(args, on_output) -> int` (exists, from the Ollama installer), `linker._lms_cli_path() -> str | None` (exists, line ~519)
- Produces: `linker._install_lmstudio(*, on_output=None) -> EngineInstallResult`; `has_automated_installer("lmstudio") -> True`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_linker_install_engine.py

def test_is_lmstudio_installed_detects_headless_cli(monkeypatch, tmp_path):
    """A headless llmster install has no GUI app bundle, but is still a
    real, usable install - detection must not require the GUI app."""
    monkeypatch.setattr(linker, "_app_bundle_installed", lambda name: False)
    monkeypatch.setattr(linker, "lmstudio_home_dir", lambda: tmp_path / "nope")
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: "/usr/local/bin/lms")

    assert linker.is_lmstudio_installed() is True


def test_has_automated_installer_true_for_lmstudio():
    assert linker.has_automated_installer("lmstudio") is True


def test_install_lmstudio_mac_linux_streams_output_and_reports_installed(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(linker, "is_lmstudio_installed", lambda: True)
    monkeypatch.setattr(
        linker.subprocess, "Popen", lambda *a, **k: _FakeProc(["Downloading llmster...\n"])
    )
    captured = []

    result = linker.install_engine("lmstudio", on_output=captured.append)

    assert result == linker.EngineInstallResult(
        "lmstudio", "installed", "LM Studio installed successfully."
    )
    assert captured == ["Downloading llmster..."]


def test_install_lmstudio_linux_reports_failed_when_still_not_detected(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Linux")
    monkeypatch.setattr(linker, "is_lmstudio_installed", lambda: False)
    monkeypatch.setattr(linker.subprocess, "Popen", lambda *a, **k: _FakeProc([]))

    result = linker.install_engine("lmstudio")

    assert result.status == "failed"
    assert "lmstudio.ai/download" in result.message


def test_install_lmstudio_windows_uses_powershell(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        linker.shutil, "which", lambda name: "C:\\powershell.exe" if name == "powershell" else None
    )
    monkeypatch.setattr(linker, "is_lmstudio_installed", lambda: True)
    calls = []

    def fake_popen(args, **kwargs):
        calls.append(args)
        return _FakeProc([])

    monkeypatch.setattr(linker.subprocess, "Popen", fake_popen)

    result = linker.install_engine("lmstudio")

    assert result.status == "installed"
    assert calls[0][0] == "C:\\powershell.exe"
    assert "irm https://lmstudio.ai/install.ps1 | iex" in calls[0]


def test_install_lmstudio_windows_without_powershell_is_unsupported(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(linker.shutil, "which", lambda name: None)

    result = linker.install_engine("lmstudio")

    assert result.status == "unsupported_platform"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_linker_install_engine.py -v -k lmstudio`
Expected: FAIL — `has_automated_installer` raises/returns wrong value, `install_engine("lmstudio")` raises `NotImplementedError`, `is_lmstudio_installed` test fails since the CLI check doesn't exist yet.

- [ ] **Step 3: Implement**

In `src/omm/linker.py`, replace `is_lmstudio_installed` (around line 116):

```python
def is_lmstudio_installed() -> bool:
    # A headless llmster install (the `lms` CLI + daemon, no GUI) is a
    # real, usable install with no app bundle at all - check it first.
    if _lms_cli_path() is not None:
        return True
    if platform.system() == "Darwin":
        return _app_bundle_installed("LM Studio")
    return lmstudio_home_dir().exists()
```

Add `has_automated_installer("lmstudio")` support and `_install_lmstudio` next to `_install_ollama` (find `def install_engine` and `def _install_ollama` with `grep -n "def install_engine\|def has_automated_installer\|def _install_ollama" src/omm/linker.py`):

```python
def has_automated_installer(key: str) -> bool:
    if key == "ollama":
        return True
    if key == "lmstudio":
        return True
    return False


def install_engine(
    key: str, *, on_output: Callable[[str], None] | None = None
) -> EngineInstallResult:
    if key == "ollama":
        return _install_ollama(on_output=on_output)
    if key == "lmstudio":
        return _install_lmstudio(on_output=on_output)
    raise NotImplementedError(f"no automated installer for engine: {key}")


def _install_lmstudio(
    *, on_output: Callable[[str], None] | None = None
) -> EngineInstallResult:
    """Installs llmster, LM Studio's headless CLI+daemon core - not the
    GUI app. Same official-script pattern as Ollama's installer; see
    is_lmstudio_installed()'s CLI check for why that's still a real
    install."""
    system = platform.system()
    returncode: int | None = None
    if system in ("Darwin", "Linux"):
        try:
            returncode = _stream_subprocess(
                ["/bin/sh", "-c", "curl -fsSL https://lmstudio.ai/install.sh | bash"],
                on_output,
            )
        except OSError as e:
            return EngineInstallResult("lmstudio", "failed", f"Could not start installer: {e}")
    elif system == "Windows":
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if powershell is None:
            return EngineInstallResult(
                "lmstudio",
                "unsupported_platform",
                "PowerShell not found - install manually from https://lmstudio.ai/download",
            )
        try:
            returncode = _stream_subprocess(
                [powershell, "-NoProfile", "-Command", "irm https://lmstudio.ai/install.ps1 | iex"],
                on_output,
            )
        except OSError as e:
            return EngineInstallResult("lmstudio", "failed", f"Could not start installer: {e}")
    else:
        return EngineInstallResult(
            "lmstudio", "unsupported_platform", f"No automated installer for {system}."
        )

    if is_lmstudio_installed():
        return EngineInstallResult("lmstudio", "installed", "LM Studio installed successfully.")
    detail = f" (installer exited with code {returncode})" if returncode else ""
    return EngineInstallResult(
        "lmstudio",
        "failed",
        f"Installer ran but LM Studio still isn't detected{detail}. "
        "Install manually from https://lmstudio.ai/download",
    )
```

Replace the existing `has_automated_installer`/`install_engine` definitions from the previous plan (which only had the `"ollama"` branch) with the versions above rather than duplicating a second copy — `grep -n "def has_automated_installer\|def install_engine" src/omm/linker.py` first to find the exact current text to replace.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_linker_install_engine.py -v`
Expected: PASS (all Ollama tests still pass + new LM Studio tests pass)

- [ ] **Step 5: Run the full linker/onboarding test suite**

Run: `pytest tests/ -k "linker or onboarding" -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/omm/linker.py tests/test_linker_install_engine.py
git commit -m "feat: add LM Studio auto-installer (llmster headless CLI)"
```

---

### Task 2: `_install_via_package_manager` helper + Jan installer

**Files:**
- Modify: `src/omm/linker.py` (add helper + `_install_jan`, extend `has_automated_installer`/`install_engine`)
- Test: `tests/test_linker_install_engine.py`

**Interfaces:**
- Produces: `linker._install_via_package_manager(*, key, label, manual_url, is_installed, on_output=None, brew_cask=None, winget_id=None, flatpak_id=None) -> EngineInstallResult` — reused by Tasks 3 and 4
- Produces: `linker._install_jan(*, on_output=None) -> EngineInstallResult`; `has_automated_installer("jan") -> True`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_linker_install_engine.py

def test_install_via_package_manager_mac_uses_brew(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(linker.shutil, "which", lambda name: "/usr/local/bin/brew" if name == "brew" else None)
    calls = []

    def fake_popen(args, **kwargs):
        calls.append(args)
        return _FakeProc([])

    monkeypatch.setattr(linker.subprocess, "Popen", fake_popen)

    result = linker._install_via_package_manager(
        key="jan", label="Jan", manual_url="https://jan.ai/download",
        is_installed=lambda: True, brew_cask="jan",
    )

    assert result.status == "installed"
    assert calls[0] == ["brew", "install", "--cask", "jan"]


def test_install_via_package_manager_mac_without_brew_is_unsupported(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(linker.shutil, "which", lambda name: None)

    result = linker._install_via_package_manager(
        key="jan", label="Jan", manual_url="https://jan.ai/download",
        is_installed=lambda: False, brew_cask="jan",
    )

    assert result.status == "unsupported_platform"
    assert "jan.ai/download" in result.message


def test_install_via_package_manager_windows_uses_winget(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(linker.shutil, "which", lambda name: "winget.exe" if name == "winget" else None)
    calls = []

    def fake_popen(args, **kwargs):
        calls.append(args)
        return _FakeProc([])

    monkeypatch.setattr(linker.subprocess, "Popen", fake_popen)

    result = linker._install_via_package_manager(
        key="jan", label="Jan", manual_url="https://jan.ai/download",
        is_installed=lambda: True, winget_id="Jan.Jan",
    )

    assert result.status == "installed"
    assert calls[0] == ["winget", "install", "-e", "--id", "Jan.Jan", "--silent"]


def test_install_via_package_manager_linux_uses_flatpak(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Linux")
    monkeypatch.setattr(linker.shutil, "which", lambda name: "/usr/bin/flatpak" if name == "flatpak" else None)
    calls = []

    def fake_popen(args, **kwargs):
        calls.append(args)
        return _FakeProc([])

    monkeypatch.setattr(linker.subprocess, "Popen", fake_popen)

    result = linker._install_via_package_manager(
        key="jan", label="Jan", manual_url="https://jan.ai/download",
        is_installed=lambda: True, flatpak_id="ai.jan.Jan",
    )

    assert result.status == "installed"
    assert calls[0] == ["flatpak", "install", "-y", "flathub", "ai.jan.Jan"]


def test_install_via_package_manager_no_option_for_platform_is_unsupported(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Linux")

    result = linker._install_via_package_manager(
        key="anythingllm", label="AnythingLLM", manual_url="https://docs.anythingllm.com/x",
        is_installed=lambda: False, brew_cask="anythingllm", winget_id="MintplexLabs.AnythingLLM",
    )

    assert result.status == "unsupported_platform"


def test_has_automated_installer_true_for_jan():
    assert linker.has_automated_installer("jan") is True


def test_install_engine_jan_dispatches_to_package_manager_helper(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(linker.shutil, "which", lambda name: "/usr/local/bin/brew" if name == "brew" else None)
    monkeypatch.setattr(linker, "is_jan_installed", lambda: True)
    monkeypatch.setattr(linker.subprocess, "Popen", lambda *a, **k: _FakeProc([]))

    result = linker.install_engine("jan")

    assert result == linker.EngineInstallResult("jan", "installed", "Jan installed successfully.")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_linker_install_engine.py -v -k "package_manager or jan"`
Expected: FAIL — `_install_via_package_manager` doesn't exist, `"jan"` not dispatched.

- [ ] **Step 3: Implement**

Add to `src/omm/linker.py`, near `_install_ollama`:

```python
def _install_via_package_manager(
    *,
    key: str,
    label: str,
    manual_url: str,
    is_installed: Callable[[], bool],
    on_output: Callable[[str], None] | None = None,
    brew_cask: str | None = None,
    winget_id: str | None = None,
    flatpak_id: str | None = None,
) -> EngineInstallResult:
    """Shared shape for engines whose only automated path is a package
    manager: brew cask on macOS, winget on Windows, flatpak on Linux. Any
    platform without a configured option (a None kwarg, or the package
    manager itself missing) falls back to unsupported_platform with a
    manual link - never guesses a direct download URL."""
    system = platform.system()
    args: list[str] | None = None
    if system == "Darwin" and brew_cask is not None:
        if shutil.which("brew") is None:
            return EngineInstallResult(
                key, "unsupported_platform", f"Homebrew not found - install manually from {manual_url}"
            )
        args = ["brew", "install", "--cask", brew_cask]
    elif system == "Windows" and winget_id is not None:
        if shutil.which("winget") is None:
            return EngineInstallResult(
                key, "unsupported_platform", f"winget not found - install manually from {manual_url}"
            )
        args = ["winget", "install", "-e", "--id", winget_id, "--silent"]
    elif system == "Linux" and flatpak_id is not None:
        if shutil.which("flatpak") is None:
            return EngineInstallResult(
                key, "unsupported_platform", f"flatpak not found - install manually from {manual_url}"
            )
        args = ["flatpak", "install", "-y", "flathub", flatpak_id]
    else:
        return EngineInstallResult(
            key, "unsupported_platform", f"No automated installer for {system} - install manually from {manual_url}"
        )

    try:
        returncode = _stream_subprocess(args, on_output)
    except OSError as e:
        return EngineInstallResult(key, "failed", f"Could not start installer: {e}")

    if is_installed():
        return EngineInstallResult(key, "installed", f"{label} installed successfully.")
    detail = f" (installer exited with code {returncode})" if returncode else ""
    return EngineInstallResult(
        key,
        "failed",
        f"Installer ran but {label} still isn't detected{detail}. Install manually from {manual_url}",
    )


def _install_jan(*, on_output: Callable[[str], None] | None = None) -> EngineInstallResult:
    return _install_via_package_manager(
        key="jan",
        label="Jan",
        manual_url="https://jan.ai/download",
        is_installed=is_jan_installed,
        on_output=on_output,
        brew_cask="jan",
        winget_id="Jan.Jan",
        flatpak_id="ai.jan.Jan",
    )
```

Extend `has_automated_installer`/`install_engine` (from Task 1) with the `"jan"` branch:

```python
    if key == "jan":
        return True
```
```python
    if key == "jan":
        return _install_jan(on_output=on_output)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_linker_install_engine.py -v`
Expected: PASS

- [ ] **Step 5: Run the full linker/onboarding test suite**

Run: `pytest tests/ -k "linker or onboarding" -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/omm/linker.py tests/test_linker_install_engine.py
git commit -m "feat: add shared package-manager installer helper + Jan auto-installer"
```

---

### Task 3: AnythingLLM installer

**Files:**
- Modify: `src/omm/linker.py`
- Test: `tests/test_linker_install_engine.py`

**Interfaces:**
- Consumes: `linker._install_via_package_manager` (Task 2)
- Produces: `linker._install_anythingllm(*, on_output=None) -> EngineInstallResult`; `has_automated_installer("anythingllm") -> True`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_linker_install_engine.py

def test_has_automated_installer_true_for_anythingllm():
    assert linker.has_automated_installer("anythingllm") is True


def test_install_anythingllm_mac_uses_brew_cask(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(linker.shutil, "which", lambda name: "/usr/local/bin/brew" if name == "brew" else None)
    monkeypatch.setattr(linker, "is_anythingllm_installed", lambda: True)
    calls = []

    def fake_popen(args, **kwargs):
        calls.append(args)
        return _FakeProc([])

    monkeypatch.setattr(linker.subprocess, "Popen", fake_popen)

    result = linker.install_engine("anythingllm")

    assert result.status == "installed"
    assert calls[0] == ["brew", "install", "--cask", "anythingllm"]


def test_install_anythingllm_windows_uses_winget(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(linker.shutil, "which", lambda name: "winget.exe" if name == "winget" else None)
    monkeypatch.setattr(linker, "is_anythingllm_installed", lambda: True)
    calls = []

    def fake_popen(args, **kwargs):
        calls.append(args)
        return _FakeProc([])

    monkeypatch.setattr(linker.subprocess, "Popen", fake_popen)

    result = linker.install_engine("anythingllm")

    assert result.status == "installed"
    assert calls[0] == ["winget", "install", "-e", "--id", "MintplexLabs.AnythingLLM", "--silent"]


def test_install_anythingllm_linux_is_unsupported(monkeypatch):
    """The official Linux installer.sh is interactive (sudo AppArmor
    prompt) with no documented non-interactive flag - automating it is
    explicitly out of scope for this plan."""
    monkeypatch.setattr(linker.platform, "system", lambda: "Linux")

    result = linker.install_engine("anythingllm")

    assert result.status == "unsupported_platform"
    assert "anythingllm.com" in result.message
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_linker_install_engine.py -v -k anythingllm`
Expected: FAIL — `"anythingllm"` not dispatched.

- [ ] **Step 3: Implement**

```python
def _install_anythingllm(*, on_output: Callable[[str], None] | None = None) -> EngineInstallResult:
    # Linux deliberately has no flatpak_id/download path here: the only
    # official Linux install method is an interactive installer.sh (sudo
    # AppArmor-profile prompt, no documented silent flag) - same risk
    # class the original design excluded text-generation-webui's git-clone
    # path for. Falls through to unsupported_platform on Linux.
    return _install_via_package_manager(
        key="anythingllm",
        label="AnythingLLM",
        manual_url="https://docs.anythingllm.com/installation-desktop/overview",
        is_installed=is_anythingllm_installed,
        on_output=on_output,
        brew_cask="anythingllm",
        winget_id="MintplexLabs.AnythingLLM",
    )
```

Extend `has_automated_installer`/`install_engine` with the `"anythingllm"` branch (same pattern as `"jan"`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_linker_install_engine.py -v`
Expected: PASS

- [ ] **Step 5: Run the full linker/onboarding test suite**

Run: `pytest tests/ -k "linker or onboarding" -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/omm/linker.py tests/test_linker_install_engine.py
git commit -m "feat: add AnythingLLM auto-installer (macOS/Windows)"
```

---

### Task 4: Msty installer

**Files:**
- Modify: `src/omm/linker.py`
- Test: `tests/test_linker_install_engine.py`

**Interfaces:**
- Consumes: `linker._install_via_package_manager` (Task 2)
- Produces: `linker._install_mstystudio(*, on_output=None) -> EngineInstallResult`; `has_automated_installer("mstystudio") -> True`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_linker_install_engine.py

def test_has_automated_installer_true_for_mstystudio():
    assert linker.has_automated_installer("mstystudio") is True


def test_install_mstystudio_mac_uses_brew_cask(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(linker.shutil, "which", lambda name: "/usr/local/bin/brew" if name == "brew" else None)
    monkeypatch.setattr(linker, "is_mstystudio_installed", lambda: True)
    calls = []

    def fake_popen(args, **kwargs):
        calls.append(args)
        return _FakeProc([])

    monkeypatch.setattr(linker.subprocess, "Popen", fake_popen)

    result = linker.install_engine("mstystudio")

    assert result.status == "installed"
    assert calls[0] == ["brew", "install", "--cask", "mstystudio"]


def test_install_mstystudio_windows_is_unsupported(monkeypatch):
    """No current winget package exists for Msty Studio - the only one in
    winget-pkgs (CloudStack.Msty) targets the deprecated legacy app and
    must not be used."""
    monkeypatch.setattr(linker.platform, "system", lambda: "Windows")

    result = linker.install_engine("mstystudio")

    assert result.status == "unsupported_platform"
    assert "msty.ai" in result.message


def test_install_mstystudio_linux_is_unsupported(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Linux")

    result = linker.install_engine("mstystudio")

    assert result.status == "unsupported_platform"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_linker_install_engine.py -v -k mstystudio`
Expected: FAIL

- [ ] **Step 3: Implement**

```python
def _install_mstystudio(*, on_output: Callable[[str], None] | None = None) -> EngineInstallResult:
    # No winget_id/flatpak_id: the only winget entry for this app family
    # (CloudStack.Msty) targets the deprecated pre-rebrand "Msty" app, not
    # current "Msty Studio" - using it would install the wrong software.
    # No Linux package manager exists at all.
    return _install_via_package_manager(
        key="mstystudio",
        label="Msty",
        manual_url="https://msty.ai/download",
        is_installed=is_mstystudio_installed,
        on_output=on_output,
        brew_cask="mstystudio",
    )
```

Extend `has_automated_installer`/`install_engine` with the `"mstystudio"` branch.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_linker_install_engine.py -v`
Expected: PASS

- [ ] **Step 5: Run the full linker/onboarding test suite**

Run: `pytest tests/ -k "linker or onboarding" -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/omm/linker.py tests/test_linker_install_engine.py
git commit -m "feat: add Msty Studio auto-installer (macOS only)"
```

---

### Task 5: KoboldCpp installer

**Files:**
- Modify: `src/omm/linker.py`
- Test: `tests/test_linker_install_engine.py`

**Interfaces:**
- Consumes: `linker.find_koboldcpp_binary` (exists, `@lru_cache(maxsize=1)`), `linker._HEURISTIC_SEARCH_ROOTS` (exists)
- Produces: `linker._ENGINE_INSTALL_DIR: Path` (new module constant - the single place both installers in this plan and the detection heuristic point at, so a test only ever has to patch one name instead of the real `Path.home()`); `linker._install_koboldcpp(*, on_output=None) -> EngineInstallResult`; `has_automated_installer("koboldcpp") -> True`

`tests/test_linker_install_engine.py` doesn't import `Path` yet (only `pytest` and `from omm import linker`) - this task's tests need `from pathlib import Path` added to that file's imports.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_linker_install_engine.py

def test_has_automated_installer_true_for_koboldcpp():
    assert linker.has_automated_installer("koboldcpp") is True


def test_install_koboldcpp_downloads_to_applications_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "_HEURISTIC_SEARCH_ROOTS", [tmp_path])
    monkeypatch.setattr(linker, "_ENGINE_INSTALL_DIR", tmp_path)
    monkeypatch.setattr(linker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(linker.platform, "machine", lambda: "arm64")
    linker.find_koboldcpp_binary.cache_clear()
    calls = []

    def fake_popen(args, **kwargs):
        calls.append(args)
        # Simulate curl actually creating the file, like the real download would.
        dest = Path(args[args.index("-o") + 1])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake binary")
        return _FakeProc([])

    monkeypatch.setattr(linker.subprocess, "Popen", fake_popen)

    result = linker.install_engine("koboldcpp")

    assert result.status == "installed"
    assert "koboldcpp-mac-arm64" in calls[0][calls[0].index("-o") - 1]
    assert (tmp_path / "koboldcpp" / "koboldcpp").exists()
    linker.find_koboldcpp_binary.cache_clear()


def test_install_koboldcpp_unsupported_arch_is_unsupported_platform(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(linker.platform, "machine", lambda: "x86_64")  # Intel Mac - no build exists

    result = linker.install_engine("koboldcpp")

    assert result.status == "unsupported_platform"
    assert "koboldcpp" in result.message.lower()


def test_install_koboldcpp_reports_failed_when_still_not_detected(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "_HEURISTIC_SEARCH_ROOTS", [tmp_path])
    monkeypatch.setattr(linker, "_ENGINE_INSTALL_DIR", tmp_path)
    monkeypatch.setattr(linker.platform, "system", lambda: "Linux")
    monkeypatch.setattr(linker.platform, "machine", lambda: "x86_64")
    linker.find_koboldcpp_binary.cache_clear()
    # curl "succeeds" (no exception) but never actually writes the file -
    # simulates a network failure that curl itself doesn't turn into an OSError.
    monkeypatch.setattr(linker.subprocess, "Popen", lambda *a, **k: _FakeProc([]))

    result = linker.install_engine("koboldcpp")

    assert result.status == "failed"
    linker.find_koboldcpp_binary.cache_clear()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_linker_install_engine.py -v -k koboldcpp`
Expected: FAIL

- [ ] **Step 3: Implement**

Add `_ENGINE_INSTALL_DIR` as a module constant right next to `_HEURISTIC_SEARCH_ROOTS` (`grep -n "_HEURISTIC_SEARCH_ROOTS = \[" src/omm/linker.py`), and point one entry of that list at it instead of the literal it currently has, so both stay in sync in production while tests only ever patch the one name:

```python
_ENGINE_INSTALL_DIR = Path.home() / "Applications"

_HEURISTIC_SEARCH_ROOTS = [
    Path.home(),
    Path.home() / "Downloads",
    Path.home() / "Documents",
    Path.home() / "Desktop",
    _ENGINE_INSTALL_DIR,
    Path("/Applications"),
]
```

```python
_KOBOLDCPP_ASSET_BY_PLATFORM: dict[tuple[str, str], str] = {
    ("Darwin", "arm64"): "koboldcpp-mac-arm64",  # no Intel Mac build exists, confirmed
    ("Linux", "x86_64"): "koboldcpp-linux-x64",
    ("Windows", "AMD64"): "koboldcpp.exe",
}


def _install_koboldcpp(*, on_output: Callable[[str], None] | None = None) -> EngineInstallResult:
    system = platform.system()
    machine = platform.machine()
    asset = _KOBOLDCPP_ASSET_BY_PLATFORM.get((system, machine))
    if asset is None:
        return EngineInstallResult(
            "koboldcpp",
            "unsupported_platform",
            f"No koboldcpp build for {system}/{machine} - see https://github.com/LostRuins/koboldcpp/releases",
        )

    dest_dir = _ENGINE_INSTALL_DIR / "koboldcpp"
    dest_name = "koboldcpp.exe" if system == "Windows" else "koboldcpp"
    dest_path = dest_dir / dest_name
    url = f"https://github.com/LostRuins/koboldcpp/releases/latest/download/{asset}"

    returncode: int | None = None
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        returncode = _stream_subprocess(["curl", "-fsSL", "-o", str(dest_path), url], on_output)
    except OSError as e:
        return EngineInstallResult("koboldcpp", "failed", f"Could not download koboldcpp: {e}")

    if system != "Windows" and dest_path.exists():
        try:
            dest_path.chmod(dest_path.stat().st_mode | 0o111)
        except OSError:
            pass

    find_koboldcpp_binary.cache_clear()
    if is_koboldcpp_installed():
        return EngineInstallResult("koboldcpp", "installed", "KoboldCpp downloaded successfully.")
    detail = f" (curl exited with code {returncode})" if returncode else ""
    return EngineInstallResult(
        "koboldcpp",
        "failed",
        f"Download ran but koboldcpp still isn't detected{detail}. "
        "Get it manually from https://github.com/LostRuins/koboldcpp/releases",
    )
```

Extend `has_automated_installer`/`install_engine` with the `"koboldcpp"` branch.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_linker_install_engine.py -v`
Expected: PASS

- [ ] **Step 5: Run the full linker/onboarding test suite**

Run: `pytest tests/ -k "linker or onboarding" -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/omm/linker.py tests/test_linker_install_engine.py
git commit -m "feat: add KoboldCpp auto-installer (direct binary download)"
```

---

### Task 6: text-generation-webui installer + portable-build detection

**Files:**
- Modify: `src/omm/linker.py` (`_TEXTGENWEBUI_NAME_HINT` and `find_textgenwebui_root` around line 1421-1439; add `_install_textgenwebui`; extend `has_automated_installer`/`install_engine`)
- Test: `tests/test_linker_new_engines.py` (detection), `tests/test_linker_install_engine.py` (installer)

**Interfaces:**
- Consumes: `omm.hardware.scan_hardware() -> HardwareInfo` (for GPU-variant selection), `requests` (for the GitHub Releases API call), `linker._ENGINE_INSTALL_DIR: Path` (Task 5)
- Produces: `linker._install_textgenwebui(*, on_output=None) -> EngineInstallResult`; `has_automated_installer("textgenwebui") -> True`; `find_textgenwebui_root()` now also recognizes the portable-build layout

**Global note:** the version number is embedded in every release asset filename (`textgen-portable-<version>-<os>-<variant>.<ext>`), so the exact filename must come from `GET https://api.github.com/repos/oobabooga/text-generation-webui/releases/latest` at install time (`requests.get(..., timeout=10).json()["assets"]`), matched by filename substring - never hardcode a version number.

- [ ] **Step 1: Write the failing tests for detection (portable-build layout)**

```python
# append to tests/test_linker_new_engines.py, in the KoboldCpp/textgenwebui section

def test_find_textgenwebui_root_recognizes_portable_build_layout(tmp_path, monkeypatch):
    """Portable releases extract to a `textgen-<version>` folder with
    `app/server.py` (not root-level server.py+one_click.py like the old
    git-clone layout) - verified directly against a real release archive."""
    monkeypatch.setattr(linker, "_HEURISTIC_SEARCH_ROOTS", [tmp_path])
    portable_root = tmp_path / "textgen-4.9"
    (portable_root / "app").mkdir(parents=True)
    (portable_root / "app" / "server.py").touch()
    (portable_root / "user_data" / "models").mkdir(parents=True)

    assert linker.find_textgenwebui_root() == portable_root
    assert linker.textgenwebui_models_dir() == portable_root / "user_data" / "models"


def test_find_textgenwebui_root_still_recognizes_old_git_clone_layout(tmp_path, monkeypatch):
    """Regression guard: existing users with the old git-clone install
    (root-level server.py + one_click.py) must not stop being detected."""
    monkeypatch.setattr(linker, "_HEURISTIC_SEARCH_ROOTS", [tmp_path])
    old_root = tmp_path / "text-generation-webui"
    old_root.mkdir()
    (old_root / "server.py").touch()
    (old_root / "one_click.py").touch()

    assert linker.find_textgenwebui_root() == old_root


def test_find_textgenwebui_root_portable_layout_requires_name_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "_HEURISTIC_SEARCH_ROOTS", [tmp_path])
    wrong_name = tmp_path / "some-other-4.9"
    (wrong_name / "app").mkdir(parents=True)
    (wrong_name / "app" / "server.py").touch()

    assert linker.find_textgenwebui_root() is None
```

- [ ] **Step 2: Run detection tests to verify they fail**

Run: `pytest tests/test_linker_new_engines.py -v -k "portable_build or git_clone_layout"`
Expected: FAIL — the portable-layout tests fail (name regex/marker-file check don't recognize it yet); the git-clone regression test currently passes already (baseline) and must keep passing after Step 3.

- [ ] **Step 3: Implement the detection fix**

In `src/omm/linker.py`, replace `_TEXTGENWEBUI_NAME_HINT` and `find_textgenwebui_root`:

```python
_TEXTGENWEBUI_NAME_HINT = re.compile(
    r"text-generation-webui|oobabooga|textgen", re.IGNORECASE
)


@lru_cache(maxsize=1)
def find_textgenwebui_root() -> Path | None:
    for root in _HEURISTIC_SEARCH_ROOTS:
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not (entry.is_dir() and _TEXTGENWEBUI_NAME_HINT.search(entry.name)):
                continue
            # Old git-clone install: server.py + one_click.py at the root.
            if (entry / "server.py").exists() and (entry / "one_click.py").exists():
                return entry
            # Portable prebuilt release: server.py lives under app/, and
            # there's no one_click.py at all (verified against a real
            # release archive, not the docs).
            if (entry / "app" / "server.py").exists():
                return entry
    return None
```

`textgenwebui_models_dir()` needs no change - `user_data/models` is at the same relative path in both layouts.

- [ ] **Step 4: Run detection tests to verify they pass**

Run: `pytest tests/test_linker_new_engines.py -v`
Expected: PASS (all existing tests + the 3 new ones)

- [ ] **Step 5: Write the failing tests for the installer**

```python
# append to tests/test_linker_install_engine.py

def test_has_automated_installer_true_for_textgenwebui():
    assert linker.has_automated_installer("textgenwebui") is True


def test_install_textgenwebui_picks_cpu_variant_with_no_gpu(monkeypatch, tmp_path):
    monkeypatch.setattr(linker.platform, "system", lambda: "Linux")
    monkeypatch.setattr(linker.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(linker, "_HEURISTIC_SEARCH_ROOTS", [tmp_path])
    monkeypatch.setattr(linker, "_ENGINE_INSTALL_DIR", tmp_path)
    linker.find_textgenwebui_root.cache_clear()

    fake_release = {
        "assets": [
            {"name": "textgen-portable-4.9-linux-cpu.tar.gz", "browser_download_url": "https://example.test/cpu.tar.gz"},
            {"name": "textgen-portable-4.9-linux-cuda12.4.tar.gz", "browser_download_url": "https://example.test/cuda.tar.gz"},
        ]
    }

    class _FakeResponse:
        def json(self):
            return fake_release

        def raise_for_status(self):
            pass

    monkeypatch.setattr(linker.requests, "get", lambda *a, **k: _FakeResponse())

    downloaded_urls = []

    def fake_stream_subprocess(args, on_output):
        downloaded_urls.append(args[-1])
        # Simulate the download landing where curl -o would put it.
        dest = Path(args[args.index("-o") + 1])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake archive")
        return 0

    monkeypatch.setattr(linker, "_stream_subprocess", fake_stream_subprocess)

    def fake_extract(archive_path, dest_dir):
        extracted = dest_dir / "textgen-4.9"
        (extracted / "app").mkdir(parents=True)
        (extracted / "app" / "server.py").touch()
        return extracted

    monkeypatch.setattr(linker, "_extract_textgenwebui_archive", fake_extract)

    result = linker.install_engine("textgenwebui")

    assert result.status == "installed"
    assert downloaded_urls == ["https://example.test/cpu.tar.gz"]
    linker.find_textgenwebui_root.cache_clear()


def test_install_textgenwebui_picks_cuda_variant_with_nvidia_gpu(monkeypatch, tmp_path):
    from omm.hardware import HardwareInfo

    monkeypatch.setattr(linker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(linker.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(linker, "_HEURISTIC_SEARCH_ROOTS", [tmp_path])
    monkeypatch.setattr(linker, "_ENGINE_INSTALL_DIR", tmp_path)
    linker.find_textgenwebui_root.cache_clear()

    fake_hw = HardwareInfo(
        os_name="Windows", os_version="11", cpu="Test CPU",
        ram_total_gb=32.0, ram_available_gb=16.0, unified_memory=False,
        gpu_name="NVIDIA GeForce RTX 4090", vram_total_gb=24.0, vram_free_gb=20.0,
    )
    monkeypatch.setattr(linker, "scan_hardware", lambda: fake_hw)

    fake_release = {
        "assets": [
            {"name": "textgen-portable-4.9-windows-cpu.zip", "browser_download_url": "https://example.test/cpu.zip"},
            {"name": "textgen-portable-4.9-windows-cuda12.4.zip", "browser_download_url": "https://example.test/cuda.zip"},
        ]
    }

    class _FakeResponse:
        def json(self):
            return fake_release

        def raise_for_status(self):
            pass

    monkeypatch.setattr(linker.requests, "get", lambda *a, **k: _FakeResponse())

    downloaded_urls = []

    def fake_stream_subprocess(args, on_output):
        downloaded_urls.append(args[-1])
        dest = Path(args[args.index("-o") + 1])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake archive")
        return 0

    monkeypatch.setattr(linker, "_stream_subprocess", fake_stream_subprocess)

    def fake_extract(archive_path, dest_dir):
        extracted = dest_dir / "textgen-4.9"
        (extracted / "app").mkdir(parents=True)
        (extracted / "app" / "server.py").touch()
        return extracted

    monkeypatch.setattr(linker, "_extract_textgenwebui_archive", fake_extract)

    result = linker.install_engine("textgenwebui")

    assert result.status == "installed"
    assert downloaded_urls == ["https://example.test/cuda.zip"]
    linker.find_textgenwebui_root.cache_clear()


def test_install_textgenwebui_mac_uses_arch_specific_asset(monkeypatch, tmp_path):
    monkeypatch.setattr(linker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(linker.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(linker, "_HEURISTIC_SEARCH_ROOTS", [tmp_path])
    monkeypatch.setattr(linker, "_ENGINE_INSTALL_DIR", tmp_path)
    linker.find_textgenwebui_root.cache_clear()

    fake_release = {
        "assets": [
            {"name": "textgen-portable-4.9-macos-arm64.tar.gz", "browser_download_url": "https://example.test/arm64.tar.gz"},
            {"name": "textgen-portable-4.9-macos-x86_64.tar.gz", "browser_download_url": "https://example.test/x86_64.tar.gz"},
        ]
    }

    class _FakeResponse:
        def json(self):
            return fake_release

        def raise_for_status(self):
            pass

    monkeypatch.setattr(linker.requests, "get", lambda *a, **k: _FakeResponse())
    downloaded_urls = []

    def fake_stream_subprocess(args, on_output):
        downloaded_urls.append(args[-1])
        dest = Path(args[args.index("-o") + 1])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake archive")
        return 0

    monkeypatch.setattr(linker, "_stream_subprocess", fake_stream_subprocess)
    monkeypatch.setattr(
        linker,
        "_extract_textgenwebui_archive",
        lambda archive_path, dest_dir: (dest_dir / "textgen-4.9"),
    )

    linker.install_engine("textgenwebui")

    assert downloaded_urls == ["https://example.test/arm64.tar.gz"]
    linker.find_textgenwebui_root.cache_clear()


def test_install_textgenwebui_reports_failed_when_asset_not_found(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Linux")
    monkeypatch.setattr(linker.platform, "machine", lambda: "x86_64")

    class _FakeResponse:
        def json(self):
            return {"assets": []}

        def raise_for_status(self):
            pass

    monkeypatch.setattr(linker.requests, "get", lambda *a, **k: _FakeResponse())

    result = linker.install_engine("textgenwebui")

    assert result.status == "failed"
```

- [ ] **Step 6: Run installer tests to verify they fail**

Run: `pytest tests/test_linker_install_engine.py -v -k textgenwebui`
Expected: FAIL

- [ ] **Step 7: Implement the installer**

Add `import requests`, `import tarfile`, `import zipfile`, and `from omm.hardware import HardwareInfo, scan_hardware` to `src/omm/linker.py`'s import block - none of these are currently imported there (check first: `grep -n "^import requests\|^import tarfile\|^import zipfile\|from omm.hardware" src/omm/linker.py`).

```python
_TEXTGENWEBUI_RELEASES_API = (
    "https://api.github.com/repos/oobabooga/text-generation-webui/releases/latest"
)


def _textgenwebui_variant(hw: HardwareInfo) -> str:
    """Best-effort GPU-variant choice from already-collected hardware info.
    A wrong guess just means slower (or CPU-mode) inference, never a
    broken install, so this favors safe/broad compatibility over squeezing
    out maximum performance: cuda12.4 over the newer cuda13.1 (needs a
    newer driver), vulkan over guessing at ROCm on Windows (research found
    ROCm offered as Linux-only in the app's own GPU picker)."""
    gpu_name = (hw.gpu_name or "").lower()
    system = platform.system()
    if "nvidia" in gpu_name:
        return "cuda12.4"
    if "amd" in gpu_name or "radeon" in gpu_name:
        return "rocm7.2" if system == "Linux" else "vulkan"
    if gpu_name:
        return "vulkan"
    return "cpu"


def _textgenwebui_asset_name(hw: HardwareInfo) -> str | None:
    system = platform.system()
    if system == "Darwin":
        machine = platform.machine()
        arch = "arm64" if machine == "arm64" else "x86_64"
        return f"macos-{arch}"
    if system == "Windows":
        return f"windows-{_textgenwebui_variant(hw)}"
    if system == "Linux":
        return f"linux-{_textgenwebui_variant(hw)}"
    return None


def _extract_textgenwebui_archive(archive_path: Path, dest_dir: Path) -> Path:
    """Extracts the portable release into dest_dir and returns the
    resulting top-level folder (named textgen-<version> by the archive
    itself - verified against real release bytes)."""
    if archive_path.suffix == ".zip":
        with zipfile.ZipFile(archive_path) as zf:
            top_level = {name.split("/")[0] for name in zf.namelist()}
            zf.extractall(dest_dir)
    else:
        with tarfile.open(archive_path) as tf:
            top_level = {member.name.split("/")[0] for member in tf.getmembers()}
            tf.extractall(dest_dir)
    return dest_dir / next(iter(top_level))


def _install_textgenwebui(
    *, on_output: Callable[[str], None] | None = None
) -> EngineInstallResult:
    hw = scan_hardware()
    platform_tag = _textgenwebui_asset_name(hw)
    if platform_tag is None:
        return EngineInstallResult(
            "textgenwebui",
            "unsupported_platform",
            f"No automated installer for {platform.system()}.",
        )

    try:
        response = requests.get(_TEXTGENWEBUI_RELEASES_API, timeout=10)
        response.raise_for_status()
        assets = response.json().get("assets", [])
    except (requests.RequestException, ValueError) as e:
        return EngineInstallResult("textgenwebui", "failed", f"Could not check for a release: {e}")

    match = next(
        (
            a
            for a in assets
            if platform_tag in a["name"]
            and a["name"].startswith("textgen-portable-")
            and "-ik-" not in a["name"]
        ),
        None,
    )
    if match is None:
        return EngineInstallResult(
            "textgenwebui",
            "failed",
            f"No release build found for {platform_tag} - see https://github.com/oobabooga/text-generation-webui/releases",
        )

    dest_root = _ENGINE_INSTALL_DIR
    dest_root.mkdir(parents=True, exist_ok=True)
    archive_path = dest_root / match["name"]

    try:
        returncode = _stream_subprocess(
            ["curl", "-fsSL", "-o", str(archive_path), match["browser_download_url"]], on_output
        )
    except OSError as e:
        return EngineInstallResult("textgenwebui", "failed", f"Could not download: {e}")

    if not archive_path.exists():
        return EngineInstallResult("textgenwebui", "failed", "Download did not complete.")

    try:
        _extract_textgenwebui_archive(archive_path, dest_root)
    except (zipfile.BadZipFile, tarfile.TarError, OSError) as e:
        return EngineInstallResult("textgenwebui", "failed", f"Could not extract archive: {e}")
    finally:
        archive_path.unlink(missing_ok=True)

    find_textgenwebui_root.cache_clear()
    if is_textgenwebui_installed():
        return EngineInstallResult(
            "textgenwebui", "installed", "text-generation-webui installed successfully."
        )
    detail = f" (curl exited with code {returncode})" if returncode else ""
    return EngineInstallResult(
        "textgenwebui",
        "failed",
        f"Download ran but text-generation-webui still isn't detected{detail}.",
    )
```

Extend `has_automated_installer`/`install_engine` with the `"textgenwebui"` branch.

- [ ] **Step 8: Run tests to verify they pass**

Run: `pytest tests/test_linker_install_engine.py tests/test_linker_new_engines.py -v`
Expected: PASS

- [ ] **Step 9: Run the full test suite**

Run: `pytest tests/ --ignore=tests/test_feature_parity.py -v`
Expected: PASS, no regressions

- [ ] **Step 10: Commit**

```bash
git add src/omm/linker.py tests/test_linker_install_engine.py tests/test_linker_new_engines.py
git commit -m "feat: add text-generation-webui auto-installer (portable release build)"
```

---

## Self-review notes (already applied above)

- Every engine's `has_automated_installer`/`install_engine` branch lands in the *same commit* as its `_install_<engine>` function, per the Global Constraint against splitting that pair.
- `_install_via_package_manager` is introduced in Task 2 (Jan) rather than Task 1 (LM Studio) because LM Studio doesn't use it (curl-script shape, not package-manager shape) - Tasks 3/4 (AnythingLLM, Msty) consume it from Task 2.
- Task 6 is intentionally the last and largest task: it depends on nothing from Tasks 1-5, but is the riskiest change (rewrites a detection function 3 other already-shipped features - `omm scan`, `omm link`, `omm import` - transitively depend on via `is_engine_installed`/`ENGINES`), so it gets the most test coverage and the explicit git-clone-layout regression test.
