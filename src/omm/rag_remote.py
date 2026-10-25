"""Use shared document collections on the cluster head from an employee's PC (#504).

``omm rag remote set <head>`` stores the gateway address and the employee's
cluster token in ``OMM_HOME/rag-remote.json`` (owner-only file, never
printed). While it is set, the collection, add, remove, search, share and
unshare commands talk to the head instead of this PC's document store, so the
originals and the index live only on the head.

Requests go straight to the head: proxy environment variables are ignored so
the token and the documents never pass through a proxy.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from urllib import error as urlerror, request as urlrequest
from urllib.parse import quote, urlencode, urlsplit

from omm import config

DEFAULT_PORT = 52440
TIMEOUT = 30
UPLOAD_TIMEOUT = 900  # The head embeds every chunk before it answers.


class RemoteError(ValueError):
    """A user-facing problem; never contains the token."""


@dataclass(frozen=True)
class Remote:
    url: str
    token: str


def remote_path() -> Path:
    return config.OMM_HOME / "rag-remote.json"


def normalize_url(value: str) -> str:
    text = (value or "").strip()
    if "://" not in text:
        text = "http://" + text
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise RemoteError("헤드 주소는 http://<헤드 Mac 주소>:52440 형식으로 적어 주세요.")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise RemoteError("헤드 주소에는 경로를 붙이지 말고 http://<주소>:52440 까지만 적어 주세요.")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{parts.scheme}://{host}:{parts.port or DEFAULT_PORT}"


def load() -> Remote | None:
    path = remote_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise RemoteError(f"{path} 를 읽지 못했어요. `omm rag remote clear` 후 다시 설정해 주세요.") from error
    if not isinstance(data, dict) or not isinstance(data.get("url"), str) or not isinstance(data.get("token"), str):
        raise RemoteError(f"{path} 형식이 맞지 않아요. `omm rag remote clear` 후 다시 설정해 주세요.")
    return Remote(data["url"], data["token"])


def save(remote: Remote) -> Path:
    from omm.atomic import atomic_write_text

    path = remote_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps({"version": 1, "url": remote.url, "token": remote.token}) + "\n")
    os.chmod(path, 0o600)
    return path


def clear() -> bool:
    path = remote_path()
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed


def mcp_url(remote: Remote) -> str:
    return remote.url + "/mcp"


class Client:
    def __init__(self, remote: Remote):
        self.remote = remote
        # An empty ProxyHandler turns off HTTP(S)_PROXY for these requests.
        self._opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))

    def call(self, method: str, path: str, *, query: dict | None = None, json_body=None,
             data: bytes | None = None, headers: dict | None = None, timeout=TIMEOUT):
        url = self.remote.url + path + ("?" + urlencode(query) if query else "")
        sent = {"Authorization": f"Bearer {self.remote.token}", "Accept": "application/json", **(headers or {})}
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            sent["Content-Type"] = "application/json"
        elif data is not None:
            sent["Content-Type"] = "application/octet-stream"
        req = urlrequest.Request(url, data=data, method=method, headers=sent)
        try:
            with self._opener.open(req, timeout=timeout) as response:
                payload = response.read()
        except urlerror.HTTPError as error:
            raise RemoteError(_message(error.code, error.read())) from None
        except (urlerror.URLError, OSError):
            raise RemoteError(f"헤드({self.remote.url})에 연결하지 못했어요. 같은 사내망인지, 헤드 Mac이 켜져 있는지 확인해 주세요.") from None
        try:
            return json.loads(payload or b"{}")
        except ValueError:
            raise RemoteError("헤드의 응답을 읽지 못했어요. 헤드의 omm 버전을 확인해 주세요.") from None

    # -- operations ----------------------------------------------------
    def whoami(self) -> str:
        return self.call("GET", "/rag/v1/whoami")["name"]

    def collections(self) -> list[dict]:
        return self.call("GET", "/rag/v1/collections")["collections"]

    def create_collection(self, name: str) -> dict:
        return self.call("POST", "/rag/v1/collections", json_body={"name": name})

    def delete_collection(self, name: str) -> dict:
        return self.call("DELETE", "/rag/v1/collections", query={"collection": name})

    def documents(self, collection: str) -> list[dict]:
        return self.call("GET", "/rag/v1/documents", query={"collection": collection})["documents"]

    def upload(self, collection: str, filename: str, content: bytes, *, replace=False) -> dict:
        headers = {"X-OMM-Filename": quote(filename, safe="")}
        if replace:
            headers["X-OMM-Replace"] = "1"
        return self.call("POST", "/rag/v1/documents", query={"collection": collection}, data=content,
                         headers=headers, timeout=UPLOAD_TIMEOUT)

    def remove_document(self, collection: str, name: str) -> dict:
        return self.call("DELETE", "/rag/v1/documents", query={"collection": collection, "name": name})

    def search(self, query: str, collection: str | None, limit: int) -> dict:
        body = {"query": query, "limit": limit}
        if collection is not None:
            body["collection"] = collection
        return self.call("POST", "/rag/v1/search", json_body=body)

    def shares(self, collection: str) -> dict:
        return self.call("GET", "/rag/v1/shares", query={"collection": collection})

    def share(self, collection: str, *, person=None, team=None) -> dict:
        body = {"collection": collection, **({"person": person} if person is not None else {}),
                **({"team": team} if team is not None else {})}
        return self.call("POST", "/rag/v1/shares", json_body=body)

    def unshare(self, collection: str, *, person=None, team=None) -> dict:
        query = {"collection": collection, **({"person": person} if person is not None else {}),
                 **({"team": team} if team is not None else {})}
        return self.call("DELETE", "/rag/v1/shares", query=query)


def _message(status: int, payload: bytes) -> str:
    if status == 401:
        return "헤드가 토큰을 받아 주지 않았어요. 관리자에게 새 토큰을 받아 `omm rag remote set`을 다시 실행해 주세요."
    try:
        message = json.loads(payload)["error"]["message"]
    except (ValueError, KeyError, TypeError):
        message = None
    if isinstance(message, str) and message:
        return message
    if status == 404:
        return "헤드가 이 기능을 모르는 것 같아요. 헤드의 omm을 업데이트해 주세요."
    return f"헤드가 요청을 처리하지 못했어요 (HTTP {status})."


def client() -> Client | None:
    remote = load()
    return Client(remote) if remote else None


def upload_paths(client: Client, collection: str, paths, *, replace=False, progress=None) -> dict:
    """Upload files and directories to a head collection; same report shape as ``import_paths``."""
    from omm import documents

    # Fails early, before reading files, when the collection is not usable.
    client.documents(collection)
    files, skipped = documents.collect_files(paths)
    results = []
    for path, problem in files:
        if problem is None:
            try:
                if path.stat().st_size > documents.MAX_UPLOAD:
                    raise ValueError("비어 있거나 6 MiB를 넘는 문서는 등록할 수 없어요.")
                if progress is not None:
                    progress(path.name, "upload", 0, 0)
                results.append({"path": str(path), **client.upload(collection, path.name, path.read_bytes(), replace=replace)})
                continue
            except (ValueError, OSError) as error:
                problem = str(error) if isinstance(error, ValueError) else "파일을 읽지 못했어요. 권한을 확인해 주세요."
        results.append({"path": str(path), "name": path.name, "error": problem})
    return {"collection": collection, "results": results, "skipped": skipped}
