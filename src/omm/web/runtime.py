"""A web-server-owned Ollama process, separate from short-lived job workers."""
from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
from urllib.parse import urlsplit

from omm import benchmark, engine_security, install_safety, linker
from omm.engines.base import require_loopback_base_url
from omm.engines.ollama import DEFAULT_OLLAMA_URL, OllamaAdapter
from omm.web.recovery import ActionError


class RuntimeManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.process = None
        self.closed = False

    def start(self, *, timeout=20):
        with self.lock:
            if self.closed:
                raise ValueError("관리 화면을 종료하고 있어요.")
            endpoint = os.environ.get("OLLAMA_HOST", DEFAULT_OLLAMA_URL)
            if "://" not in endpoint:
                endpoint = "http://" + endpoint
            endpoint = require_loopback_base_url(endpoint)
            parsed = urlsplit(endpoint)
            # A local connection may use IPv6/TLS; this first helper only
            # launches an explicit IPv4 loopback listener, never 0.0.0.0.
            port = parsed.port
            if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
                    or port is None or not 1 <= port <= 65535):
                raise ValueError("서버 시작 도우미는 HTTP 127.0.0.1 또는 localhost 주소와 명시적인 포트를 사용해요.")
            adapter = OllamaAdapter(endpoint)
            health = adapter.health()
            if health.reachable:
                return {"engine": "ollama", "server_ready": True, "reused": True,
                        "owned": self.process is not None and self.process.poll() is None,
                        "version": health.version}
            executable = linker.find_ollama_executable()
            if executable is None:
                raise ActionError("server_unavailable", "Ollama를 먼저 설치한 뒤 서버를 시작해 주세요.")
            try:
                install_safety.verify_installed("ollama")
            except install_safety.SafetyError:
                install_safety.record_failure("ollama")
                raise
            # Refuse an occupied non-Ollama port. Never terminate its owner.
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                    listener.bind(("127.0.0.1", port))
            except OSError:
                raise ActionError("server_unavailable", "로컬 서버 주소를 다른 프로그램이 사용하고 있어요. 연결 상태를 확인해 주세요.") from None
            if self.process is not None and self.process.poll() is None:
                raise ActionError("server_unavailable", "시작한 서버가 아직 응답하지 않아요. 연결 상태를 다시 확인해 주세요.")
            kwargs = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
                      if os.name == "nt" else {"start_new_session": True})
            proc = subprocess.Popen([str(executable), "serve"], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    env={**os.environ, "OLLAMA_HOST": f"127.0.0.1:{port}"}, **kwargs)
            self.process = proc
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                health = adapter.health()
                if health.reachable and proc.poll() is None:
                    engine_security.record_owned_ollama(proc, str(executable), f"127.0.0.1:{port}")
                    return {"engine": "ollama", "server_ready": True, "owned": True,
                            "reused": False, "version": health.version}
                time.sleep(0.2)
            benchmark.stop_ollama_daemon(proc)
            self.process = None
            raise ActionError("server_unavailable", "Ollama 서버 시작 후 응답을 확인하지 못했어요. 연결 상태와 진단을 확인해 주세요.")

    def close(self):
        with self.lock:
            self.closed = True
            if self.process is not None:
                benchmark.stop_ollama_daemon(self.process)
                self.process = None
