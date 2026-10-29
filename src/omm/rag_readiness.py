"""Is semantic document search ready on this computer (#534)?

Semantic search needs Ollama and the embedding model (default bge-m3) on the
machine that stores the documents: this PC in local mode, the cluster head in
company mode. Without them search quietly falls back to words only, so
``omm rag status``, ``omm cluster status``, the head's RAG API and the web
내 문서 tab report one of:

* ``ready``         - Ollama answers and has the model
* ``model_missing`` - Ollama answers but the model is not pulled
* ``not_running``   - Ollama is installed but its server does not answer
* ``not_installed`` - no Ollama found
* ``error``         - Ollama answered something unexpected

with the exact command that fixes it. The check is read-only and cheap: one
loopback request with a short timeout and a few file-existence tests. It
never starts, installs or registers anything; keeping Ollama on after a
reboot stays the user's choice, so it is only suggested (``persistence``).
"""
from __future__ import annotations

import os
from pathlib import Path
import platform
import shutil

from omm import document_embeddings

READY = "ready"
MODEL_MISSING = "model_missing"
NOT_RUNNING = "not_running"
NOT_INSTALLED = "not_installed"
ERROR = "error"
STATES = (READY, MODEL_MISSING, NOT_RUNNING, NOT_INSTALLED, ERROR)
# Only in an employee's view of the head.
UNSUPPORTED = "unsupported"
UNREACHABLE = "unreachable"
# Short label after "의미 검색: ".
REASONS = {MODEL_MISSING: "임베딩 모델 없음", NOT_RUNNING: "Ollama가 꺼져 있음",
           NOT_INSTALLED: "Ollama 설치 안 됨", ERROR: "Ollama 응답 확인 실패"}
LABELS = {READY: "준비됨", **{state: f"꺼짐 ({reason})" for state, reason in REASONS.items()}}
WORDS_ONLY = "지금은 단어 검색만 해요."
TIMEOUT = 2
_MAC_BINARIES = (Path("/opt/homebrew/bin/ollama"), Path("/usr/local/bin/ollama"))
_MAC_APPS = (Path("/Applications/Ollama.app"), Path.home() / "Applications" / "Ollama.app")


def _system() -> str:
    return platform.system()


def _binary() -> Path | None:
    from omm.linker import find_ollama_executable
    found = find_ollama_executable()
    if found is None and _system() == "Darwin":
        # A LaunchAgent (the cluster gateway) runs with a short PATH that
        # misses Homebrew.
        found = next((path for path in _MAC_BINARIES if path.is_file()), None)
    return found


def _mac_app() -> bool:
    return _system() == "Darwin" and any(path.exists() for path in _MAC_APPS)


def _from_homebrew(binary: Path | None) -> bool:
    if binary is None:
        return False
    paths = [binary.as_posix()]
    try:
        paths.append(binary.resolve().as_posix())
    except OSError:
        pass
    return any("/Cellar/ollama/" in path or path.startswith(("/opt/homebrew/", "/home/linuxbrew/")) for path in paths)


def _brew_available() -> bool:
    return shutil.which("brew") is not None or Path("/opt/homebrew/bin/brew").is_file() \
        or Path("/usr/local/bin/brew").is_file()


def install_info() -> dict:
    binary = _binary()
    app = _mac_app()
    return {"installed": app or binary is not None, "app": app, "homebrew": not app and _from_homebrew(binary)}


def _linux_units() -> tuple[bool, bool, bool]:
    """(system unit exists, system unit enabled, user unit exists)."""
    system = Path("/etc/systemd/system/ollama.service").exists() or Path("/usr/lib/systemd/system/ollama.service").exists()
    enabled = Path("/etc/systemd/system/multi-user.target.wants/ollama.service").exists() \
        or Path("/etc/systemd/system/default.target.wants/ollama.service").exists()
    user = (Path.home() / ".config" / "systemd" / "user" / "ollama.service").exists()
    return system, enabled, user


def _windows_startup_shortcut() -> bool:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return True  # Unknown: say nothing rather than guess.
    return (Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Ollama.lnk").exists()


def _start_hint(info: dict) -> tuple[str, str | None]:
    """(sentence, command) that starts the installed Ollama server."""
    system = _system()
    if system == "Darwin":
        if info["app"]:
            return "`open -a Ollama`로 Ollama 앱을 켜 주세요.", "open -a Ollama"
        if info["homebrew"]:
            return ("`brew services start ollama`로 켜 주세요(재부팅 뒤에도 켜져요). "
                    "이번만 켜려면 `ollama serve`.", "brew services start ollama")
        return "`ollama serve`로 켜 주세요.", "ollama serve"
    if system == "Windows":
        return "시작 메뉴에서 Ollama 앱을 실행해 주세요(또는 터미널에서 `ollama serve`).", "ollama serve"
    unit, _, user = _linux_units()
    if unit:
        return "`sudo systemctl start ollama`로 켜 주세요.", "sudo systemctl start ollama"
    if user:
        return "`systemctl --user start ollama`로 켜 주세요.", "systemctl --user start ollama"
    return "`ollama serve`로 켜 주세요.", "ollama serve"


def _install_hint() -> tuple[str, str | None]:
    system = _system()
    if system == "Darwin":
        if _brew_available():
            return ("`brew install ollama`로 설치하고 `brew services start ollama`로 켜 주세요"
                    "(또는 https://ollama.com/download/mac 에서 앱 설치).", "brew install ollama")
        return "https://ollama.com/download/mac 에서 Ollama 앱을 설치하고 켜 주세요.", None
    if system == "Windows":
        return "https://ollama.com/download/windows 에서 Ollama를 설치해 주세요.", None
    return "https://ollama.com/download/linux 의 공식 설치 방법으로 Ollama를 설치해 주세요.", None


def _persistence_code(info: dict) -> str | None:
    """Which way to keep Ollama on after a reboot, only when it visibly is not set up to."""
    if not info["installed"]:
        return None
    system = _system()
    if system == "Darwin":
        if info["app"] or not info["homebrew"]:
            return None  # Ollama.app adds its own login item; other setups are unknown.
        if (Path.home() / "Library" / "LaunchAgents" / "homebrew.mxcl.ollama.plist").exists():
            return None
        return "brew"
    if system == "Windows":
        return None if _windows_startup_shortcut() else "windows"
    unit, enabled, user = _linux_units()
    if unit:
        return None if enabled else "systemctl"
    if user:
        return None
    return "systemd-service"


_PERSISTENCE = {
    "brew": ("재부팅 뒤에도 켜지게 하려면 `brew services start ollama`를 실행해 주세요"
             "(손으로 켠 `ollama serve`가 있으면 먼저 꺼 주세요).",
             "To keep Ollama on after a reboot, run `brew services start ollama` "
             "(stop any hand-started `ollama serve` first)."),
    "windows": ("재부팅 뒤에도 켜지게 하려면 설정 > 앱 > 시작 프로그램에서 Ollama를 켜 주세요.",
                "To keep Ollama on after a reboot, turn it on in Settings > Apps > Startup."),
    "systemctl": ("재부팅 뒤에도 켜지게 하려면 `sudo systemctl enable ollama`를 실행해 주세요.",
                  "To keep Ollama on after a reboot, run `sudo systemctl enable ollama`."),
    "systemd-service": ("재부팅 뒤에도 켜지게 하려면 Ollama를 systemd 서비스로 등록해 주세요(공식 설치 스크립트가 만들어 줘요).",
                        "To keep Ollama on after a reboot, register it as a systemd service "
                        "(the official install script does this)."),
}


def _persistence_hint(info: dict) -> str | None:
    code = _persistence_code(info)
    return _PERSISTENCE[code][0] if code else None


def persistence_line_en(result: dict) -> str | None:
    """English twin of result["persistence"] for the CLI; None when that is None."""
    if not result.get("persistence"):
        return None
    code = _persistence_code(install_info())
    return _PERSISTENCE[code][1] if code else None


def check(model: str | None = None, base_url: str | None = None) -> dict:
    """Readiness of semantic search on this computer; never raises for Ollama problems."""
    if model is None or base_url is None:
        from omm.documents import DocumentStore
        settings = DocumentStore().embedding_settings()
        model = model or settings["model"]
        base_url = base_url or settings["base_url"]
    result = {"state": ERROR, "ready": False, "model": model, "base_url": base_url,
              "message": "", "command": None, "persistence": None}
    try:
        from omm.documents import _embedder
        available = _embedder(model, base_url).available(timeout=TIMEOUT)
    except document_embeddings.EmbeddingUnavailable as error:
        info = install_info()
        if error.reason != "server_unavailable":
            result["message"] = f"Ollama({base_url}) 응답을 확인하지 못했어요. Ollama를 다시 켜 보세요."
        elif info["installed"]:
            sentence, result["command"] = _start_hint(info)
            result.update(state=NOT_RUNNING, message=f"Ollama가 설치돼 있지만 켜져 있지 않아요. {sentence}",
                          persistence=None if result["command"] == "brew services start ollama" else _persistence_hint(info))
        else:
            sentence, result["command"] = _install_hint()
            result.update(state=NOT_INSTALLED, message=f"Ollama를 찾지 못했어요. {sentence} 그다음 `ollama pull {model}`.")
        return result
    info = install_info()
    if not available:
        result.update(state=MODEL_MISSING, command=f"ollama pull {model}",
                      message=f"Ollama에 임베딩 모델 {model}이(가) 없어요. {document_embeddings.pull_hint(model)}")
    else:
        result.update(state=READY, ready=True, message=f"Ollama · {model}")
    result["persistence"] = _persistence_hint(info)
    return result


def line(result: dict) -> str:
    """One line for this computer: '의미 검색: 준비됨 (Ollama · bge-m3)' or what to do."""
    if result["ready"]:
        return f"의미 검색: {LABELS[READY]} ({result['message']})"
    return f"의미 검색: {LABELS.get(result['state'], LABELS[ERROR])} - {result['message']} {WORDS_ONLY}"


_REASONS_EN = {MODEL_MISSING: "embedding model missing", NOT_RUNNING: "Ollama not running",
               NOT_INSTALLED: "Ollama not installed", ERROR: "Ollama did not respond"}
WORDS_ONLY_EN = "Word search only for now."


def line_en(result: dict) -> str:
    """English twin of line() for the CLI: the same state, the same fix, from
    the structured fields instead of the Korean message."""
    model, command = result["model"], result.get("command")
    if result["ready"]:
        return f"Semantic search: ready ({result['message']})"
    state = result["state"]
    reason = _REASONS_EN.get(state, _REASONS_EN[ERROR])
    if state == MODEL_MISSING:
        detail = f"Embedding model {model} is not pulled. Run `{command}`."
    elif state == NOT_RUNNING:
        detail = f"Ollama is installed but not running. Run `{command}`."
    elif state == NOT_INSTALLED:
        step = f"Run `{command}`" if command else "Install it from https://ollama.com/download"
        detail = f"Ollama was not found. {step}, then `ollama pull {model}`."
    else:
        detail = f"Ollama ({result['base_url']}) did not respond. Restart Ollama."
    return f"Semantic search: off ({reason}) - {detail} {WORDS_ONLY_EN}"


def public(result: dict) -> dict:
    """What the head tells employees: the state and model only, no paths or commands."""
    return {"state": result["state"], "model": result["model"]}


def remote_line(view: dict | None) -> str:
    """One line for an employee about the company head's semantic search."""
    state = (view or {}).get("state")
    if state == READY:
        return f"회사 head의 의미 검색: 준비됨 ({view.get('model')})"
    if state in REASONS:
        return f"회사 head의 의미 검색이 꺼져 있어요({REASONS[state]}) - 관리자에게 알려 주세요. {WORDS_ONLY}"
    if state == UNSUPPORTED:
        return "회사 head의 의미 검색 상태를 알 수 없어요. head의 omm이 이 확인을 지원하지 않아요(업데이트 필요)."
    return "회사 head의 의미 검색 상태를 확인하지 못했어요. head에 연결되는지 확인해 주세요."


def remote_line_en(view: dict | None) -> str:
    """English twin of remote_line() for the CLI."""
    state = (view or {}).get("state")
    if state == READY:
        return f"Company head semantic search: ready ({view.get('model')})"
    if state in _REASONS_EN:
        return f"Company head semantic search is off ({_REASONS_EN[state]}) - tell the admin. {WORDS_ONLY_EN}"
    if state == UNSUPPORTED:
        return "Company head semantic search status is unknown: its omm does not support this check (update needed)."
    return "Could not check the company head's semantic search. Check that the head is reachable."


def remote_view(client) -> dict:
    """The head's public readiness; ``unsupported`` for an older head, ``unreachable`` when the call failed."""
    from omm import rag_remote
    try:
        data = client.call("GET", "/rag/v1/readiness", timeout=8)
    except rag_remote.RemoteError as error:
        return {"state": UNSUPPORTED if error.status == 404 else UNREACHABLE, "model": None}
    if not isinstance(data, dict) or data.get("state") not in STATES:
        return {"state": UNSUPPORTED, "model": None}
    return {"state": data["state"], "model": data.get("model") if isinstance(data.get("model"), str) else None}
