"""Explicit employee connections to private OMM gateways; never manage a model.

Only a user action contacts the gateway. Tokens stay in an owner-only local
file; chat text and responses live in memory. The existing local-runtime and
RAG connections are deliberately unchanged.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit
import uuid

from omm import config
from omm.atomic import atomic_write_text

MAX_RESPONSE = 2 * 1024 * 1024
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(x) for x in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fc00::/7"))


class SharedError(ValueError):
    def __init__(self, code, message, action, status=400):
        super().__init__(message)
        self.code, self.action, self.status = code, action, status

    def public(self):
        return {"error": str(self), "code": self.code, "action": self.action}


def problem(code):
    messages = {
        "address": ("공유 서버 주소를 확인해 주세요.", "관리자가 알려 준 사내망 또는 Tailscale 주소와 포트를 입력하세요. 경로·계정·암호는 붙이지 마세요."),
        "network": ("공유 서버에 연결하지 못했어요.", "Tailscale 또는 사내망 연결, 주소·포트, 대표 Mac의 전원과 게이트웨이를 확인하세요. 어느 항목이 원인인지는 아직 확인되지 않았어요."),
        "token": ("이용 토큰이 없거나 서버가 거절했어요.", "관리자에게 받은 OMM 이용 토큰을 입력하세요. Tailscale 장치 공유와 이용 토큰은 별개예요."),
        "protocol": ("이 주소에서 지원되는 공유 AI 응답을 확인하지 못했어요.", "게이트웨이 주소·포트와 서버의 OMM/exo 지원 버전을 관리자에게 확인하세요."),
        "model": ("이 모델의 응답을 시작하지 못했어요.", "모델 이름과 실행 준비 상태를 관리자에게 확인하세요. 목록에 있다는 것만으로 실행 중임을 뜻하지는 않아요."),
        "timeout": ("서버 응답을 기다리다 제한 시간에 도달했어요.", "서버가 계속 처리 중일 수 있어요. 바로 재전송하지 말고 관리자에게 모델 상태를 확인하세요."),
        "quota": ("서버가 요청 한도 또는 이용 제한을 알렸어요.", "자동으로 재시도하지 않아요. 관리자에게 이용 상태를 확인하세요."),
        "server": ("공유 서버가 요청을 처리하지 못했어요.", "관리자에게 게이트웨이와 모델 실행 상태를 확인해 달라고 요청하세요."),
        "changed": ("저장된 연결이 바뀌었어요.", "연결 상태를 다시 읽고 서버와 모델을 확인한 뒤 질문하세요."),
        "busy": ("공유 AI 요청이 이미 진행 중이에요.", "현재 요청의 결과를 기다려 주세요. 중복 전송하지 않아요."),
    }
    message, action = messages[code]
    return SharedError(code, message, action, 409 if code in {"changed", "busy"} else 400)


def normalize_url(value):
    if not isinstance(value, str) or len(value) > 512 or re.search(r"[\s\\\x00-\x1f\x7f]", value.strip()):
        raise problem("address")
    try:
        parsed = urlsplit(value.strip() if "://" in value else "http://" + value.strip())
        port = 52440 if parsed.port is None else parsed.port
        host = parsed.hostname
        if (parsed.scheme not in {"http", "https"} or not host or parsed.username is not None
                or parsed.password is not None or parsed.path not in {"", "/"} or parsed.query
                or parsed.fragment or not 1 <= port <= 65535 or "%" in host):
            raise ValueError
        host = host.encode("idna").decode("ascii").lower()
        formatted = f"[{host}]" if ":" in host else host
        return f"{parsed.scheme}://{formatted}:{port}"
    except (ValueError, UnicodeError):
        raise problem("address") from None


def private_address(host, port):
    """Validate every DNS result and pin the chosen address for this request."""
    try:
        rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise problem("network") from None
    addresses = list(dict.fromkeys(row[4][0] for row in rows))
    if not addresses:
        raise problem("network")
    for value in addresses:
        address = ipaddress.ip_address(value)
        if not any(address in network for network in PRIVATE_NETWORKS if address.version == network.version):
            raise problem("address")
    return addresses[0]


def gateway_call(url, token, path, payload=None):
    """Direct, bounded, pinned HTTP; no proxies, redirects, retries or fallback."""
    parsed = urlsplit(normalize_url(url))
    address = private_address(parsed.hostname, parsed.port)
    cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = cls(parsed.hostname, parsed.port, timeout=10)
    # HTTPSConnection still verifies the original hostname using the default
    # TLS context; only the socket destination is pinned to the validated IP.
    connection._create_connection = lambda _target, timeout=10, source_address=None: socket.create_connection(
        (address, parsed.port), timeout, source_address)
    body = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    deadline = time.monotonic() + (60 if payload is not None else 15)
    try:
        connection.request("GET" if payload is None else "POST", path, body=body, headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            code = {401: "token", 403: "token", 404: "protocol", 405: "protocol", 429: "quota",
                    402: "quota", 502: "server", 503: "model", 504: "timeout"}.get(response.status,
                    "model" if payload is not None and response.status in {400, 422} else "server")
            raise problem(code)
        content = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise problem("timeout")
            if connection.sock is not None:
                connection.sock.settimeout(min(remaining, 10))
            chunk = response.read1(65536)
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > MAX_RESPONSE:
                raise problem("protocol")
        return json.loads(content)
    except SharedError:
        raise
    except (TimeoutError, socket.timeout):
        raise problem("timeout") from None
    except ssl.SSLError:
        raise SharedError("tls", "서버 인증서를 확인하지 못했어요.", "인증서와 서버 주소를 관리자에게 확인하세요. 인증서 검사를 끄지 않아요.") from None
    except (OSError, http.client.HTTPException):
        raise problem("network") from None
    except (ValueError, UnicodeError):
        raise problem("protocol") from None
    finally:
        connection.close()


def model_ids(data):
    if not isinstance(data, dict) or not isinstance(data.get("data"), list) or len(data["data"]) > 1000:
        raise problem("protocol")
    ids = []
    for row in data["data"]:
        key = row.get("id") if isinstance(row, dict) else None
        if isinstance(key, str) and 0 < len(key) <= 256 and not re.search(r"[\x00-\x1f\x7f]", key):
            if key not in ids:
                ids.append(key)
    if len(json.dumps(ids, ensure_ascii=False).encode("utf-8")) > 256 * 1024:
        raise problem("protocol")
    return ids


def valid_token(token):
    return (isinstance(token, str) and 1 <= len(token) <= 4096 and token.isascii()
            and re.search(r"\s|[\x00-\x1f\x7f]", token) is None)


class SharedAI:
    def __init__(self):
        self.lock = threading.RLock()
        self.busy = False
        self.results = OrderedDict()

    @property
    def path(self):
        return config.OMM_HOME / "shared-ai" / "connection.json"

    def _load(self):
        path = self.path
        if path.parent.is_symlink() or path.is_symlink():
            raise SharedError("storage", "공유 AI 설정 경로를 확인해 주세요.", "연결 설정에 심볼릭 링크를 사용할 수 없어요.")
        try:
            if path.stat().st_size > 512 * 1024:
                raise ValueError
            value = json.loads(path.read_text(encoding="utf-8"))
            if (value.get("version") != 1 or not valid_token(value.get("token"))
                    or not isinstance(value.get("id"), str) or not isinstance(value.get("models"), list)
                    or len(value["models"]) > 1000 or not isinstance(value.get("model", ""), str)):
                raise ValueError
            uuid.UUID(value["id"])
            datetime.fromisoformat(value["checked_at"])
            if model_ids({"data": [{"id": key} for key in value["models"]]}) != value["models"]:
                raise ValueError
            normalize_url(value["url"])
            return value
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            raise SharedError("storage", "저장된 연결 설정을 읽지 못했어요.", "저장된 연결을 지운 뒤 다시 입력하세요.") from None

    def _save(self, value):
        path = self.path
        if path.parent.is_symlink() or path.is_symlink():
            raise problem("changed")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        atomic_write_text(path, json.dumps(value, ensure_ascii=False) + "\n")
        os.chmod(path, 0o600)

    def state(self):
        with self.lock:
            value = self._load()
            if value is None:
                return {"configured": False, "busy": self.busy}
            return {"configured": True, "busy": self.busy, "connection_id": value["id"],
                    "url": value["url"], "token_saved": True, "models": value["models"],
                    "model": value.get("model", ""), "checked_at": value["checked_at"],
                    "readiness": "not_checked"}

    def connect(self, body):
        if not isinstance(body, dict) or set(body) - {"url", "token"}:
            raise problem("address")
        url = normalize_url(body.get("url"))
        token = body.get("token", "")
        with self.lock:
            if self.busy:
                raise problem("busy")
            old = self._load()
            if token == "" and old and url == old["url"]:
                token = old["token"]
            if not valid_token(token):
                raise problem("token")
            self.busy = True
        try:
            models = model_ids(gateway_call(url, token, "/v1/models"))
            value = {"version": 1, "id": str(uuid.uuid4()), "url": url, "token": token,
                     "models": models, "model": "", "checked_at": datetime.now(timezone.utc).isoformat()}
            if old and old["url"] == url and old["token"] == token and old.get("model") in models:
                value["model"] = old["model"]
            with self.lock:
                self._save(value)
                self.results.clear()
        finally:
            with self.lock:
                self.busy = False
        return self.state()

    def clear(self):
        with self.lock:
            if self.busy:
                raise problem("busy")
            if self.path.parent.is_symlink():
                raise problem("changed")
            self.path.unlink(missing_ok=True)
            self.results.clear()
            return self.state()

    def ask(self, body):
        if not isinstance(body, dict) or set(body) != {"connection_id", "model", "messages", "request_id"}:
            raise problem("model")
        try:
            request_id = str(uuid.UUID(body["request_id"]))
        except (ValueError, TypeError, AttributeError):
            raise problem("model") from None
        messages = body["messages"]
        if (not isinstance(messages, list) or not 1 <= len(messages) <= 25 or len(messages) % 2 != 1
                or len(json.dumps(messages, ensure_ascii=False)) > 48000):
            raise problem("model")
        for i, message in enumerate(messages):
            if (not isinstance(message, dict) or set(message) != {"role", "content"}
                    or message["role"] != ("user" if i % 2 == 0 else "assistant")
                    or not isinstance(message["content"], str) or not 1 <= len(message["content"]) <= 8192):
                raise problem("model")
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.lock:
            value = self._load()
            if not value or body["connection_id"] != value["id"]:
                raise problem("changed")
            if body["model"] not in value["models"]:
                raise problem("model")
            if request_id in self.results:
                previous, result = self.results[request_id]
                if previous != fingerprint:
                    raise problem("changed")
                if isinstance(result, SharedError):
                    raise result
                return result
            if self.busy:
                raise problem("busy")
            self.busy = True
        result = problem("server")
        try:
            data = gateway_call(value["url"], value["token"], "/v1/chat/completions", {
                "model": body["model"], "messages": messages, "stream": False,
                "enable_thinking": False, "max_tokens": 512})
            try:
                if not isinstance(data, dict) or data.get("model") != body["model"]:
                    raise ValueError
                choice = data["choices"][0]
                text = choice["message"]["content"]
                if not isinstance(text, str) or not text.strip() or len(text) > 16384:
                    raise ValueError
                reason = choice.get("finish_reason")
                if reason not in {"stop", "length"}:
                    raise ValueError
            except (ValueError, KeyError, IndexError, TypeError):
                raise problem("protocol") from None
            result = {"text": text, "model": body["model"], "truncated": reason == "length",
                      "response_verified": True}
            with self.lock:
                value["model"] = body["model"]
                self._save(value)
            return result
        except SharedError as error:
            result = error
            raise
        finally:
            with self.lock:
                self.busy = False
                self.results[request_id] = (fingerprint, result)
                while len(self.results) > 16:
                    self.results.popitem(last=False)
