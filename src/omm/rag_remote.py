"""Use shared document collections on the cluster head from an employee's PC (#504).

``omm rag remote set <head>`` stores the gateway address and the employee's
cluster token in ``OMM_HOME/rag-remote.json`` (owner-only file, never
printed). While it is set, the collection, add, remove, search, share and
unshare commands talk to the head instead of this PC's document store, so the
originals and the index live only on the head.

Requests go straight to the head: proxy environment variables are ignored so
the token and the documents never pass through a proxy. They use HTTPS and
trust only the head certificate `omm rag remote set` pinned in
``OMM_HOME/rag-remote-ca.pem`` after the user compared its fingerprint (#536).
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import ssl
from urllib import error as urlerror, request as urlrequest
from urllib.parse import quote, urlencode, urlsplit

from omm import config, lang
from omm.lang import t

DEFAULT_PORT = 52440
TIMEOUT = 30
UPLOAD_TIMEOUT = 900  # The head embeds every chunk before it answers.


class RemoteError(ValueError):
    """A user-facing problem; never contains the token. ``status`` is the head's HTTP status, if any."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def plain_http() -> str:
    return t("클러스터 게이트웨이는 이제 HTTPS만 받아요. "
             "`omm rag remote set https://<헤드 주소>:52440` 으로 다시 설정해 주세요.",
             "The cluster gateway now accepts HTTPS only. "
             "Set it again with `omm rag remote set https://<head address>:52440`.")

# Not "`omm rag remote set`을 다시 실행해 주세요": the web page rewrites that
# phrase into its own connect button, which cannot show a fingerprint.
def not_pinned() -> str:
    return t("이 PC가 헤드의 인증서를 아직 확인하지 않았어요. 터미널에서 "
             "`omm rag remote set https://<헤드 주소>:52440` 을 실행해 헤드 Mac이 보여 주는 지문과 같은지 확인해 주세요.",
             "This PC has not checked the head's certificate yet. In a terminal, run "
             "`omm rag remote set https://<head address>:52440` and compare the fingerprint with the one the head Mac shows.")

def wrong_certificate() -> str:
    return t("헤드가 이 PC에 고정된 것과 다른 인증서를 내밀었어요. 헤드를 새로 만든 게 맞는지 관리자에게 확인한 뒤 "
             "`omm rag remote clear` 후 `omm rag remote set https://<헤드 주소>:52440` 으로 지문을 다시 확인해 주세요.",
             "The head offered a different certificate from the one pinned on this PC. Ask the administrator whether "
             "the head was rebuilt, then run `omm rag remote clear` and `omm rag remote set https://<head address>:52440` "
             "to check the fingerprint again.")


@dataclass(frozen=True)
class Remote:
    url: str
    token: str


def remote_path() -> Path:
    return config.OMM_HOME / "rag-remote.json"


def normalize_url(value: str) -> str:
    text = (value or "").strip()
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    if parts.scheme == "http":
        raise RemoteError(plain_http())
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise RemoteError(t("헤드 주소는 https://<헤드 Mac 주소>:52440 형식으로 적어 주세요.",
                            "Write the head address as https://<head Mac address>:52440."))
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise RemoteError(t("헤드 주소에는 경로를 붙이지 말고 https://<주소>:52440 까지만 적어 주세요.",
                            "Write the head address without a path, only up to https://<address>:52440."))
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{parts.scheme}://{host}:{parts.port or DEFAULT_PORT}"


def load() -> Remote | None:
    path = remote_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise RemoteError(t(f"{path} 를 읽지 못했어요. `omm rag remote clear` 후 다시 설정해 주세요.",
                            f"Could not read {path}. Run `omm rag remote clear` and set it up again.")) from error
    if not isinstance(data, dict) or not isinstance(data.get("url"), str) or not isinstance(data.get("token"), str):
        raise RemoteError(t(f"{path} 형식이 맞지 않아요. `omm rag remote clear` 후 다시 설정해 주세요.",
                            f"{path} has the wrong format. Run `omm rag remote clear` and set it up again."))
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
    # Forgetting the head forgets its certificate: the next `set` asks again.
    ca_path().unlink(missing_ok=True)
    return existed


def ca_path() -> Path:
    return config.OMM_HOME / "rag-remote-ca.pem"


def save_ca(pem: str) -> Path:
    from omm.atomic import atomic_write_text

    path = ca_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, pem)
    os.chmod(path, 0o644)
    return path


def pinned_ca() -> str | None:
    try:
        return ca_path().read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return None


def ssl_context(remote: Remote, ca_pem: str | None = None) -> ssl.SSLContext:
    """Trusts only the pinned head certificate (or `ca_pem` while it is being pinned)."""
    if not remote.url.startswith("https://"):
        raise RemoteError(plain_http())
    if ca_pem is None and not ca_path().is_file():
        raise RemoteError(not_pinned())
    try:
        return ssl.create_default_context(cafile=None if ca_pem else str(ca_path()), cadata=ca_pem)
    except (ssl.SSLError, OSError, ValueError):
        raise RemoteError(not_pinned()) from None


def mcp_url(remote: Remote) -> str:
    return remote.url + "/mcp"


class Client:
    def __init__(self, remote: Remote, ca_pem: str | None = None):
        self.remote = remote
        # An empty ProxyHandler turns off HTTP(S)_PROXY for these requests.
        self._opener = urlrequest.build_opener(urlrequest.ProxyHandler({}),
                                               urlrequest.HTTPSHandler(context=ssl_context(remote, ca_pem)))

    def call(self, method: str, path: str, *, query: dict | None = None, json_body=None,
             data: bytes | None = None, headers: dict | None = None, timeout=TIMEOUT):
        url = self.remote.url + path + ("?" + urlencode(query) if query else "")
        # The head answers in this context's language: Korean for the web page, English for the CLI.
        sent = {"Authorization": f"Bearer {self.remote.token}", "Accept": "application/json",
                "Accept-Language": lang.current(), **(headers or {})}
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
            raise RemoteError(_message(error.code, error.read()), error.code) from None
        except (urlerror.URLError, OSError) as error:
            if isinstance(getattr(error, "reason", error), ssl.SSLCertVerificationError):
                raise RemoteError(wrong_certificate()) from None
            raise RemoteError(t(f"헤드({self.remote.url})에 연결하지 못했어요. 같은 사내망인지, 헤드 Mac이 켜져 있는지 확인해 주세요.",
                                 f"Could not connect to the head ({self.remote.url}). Check that you are on the office "
                                 "network and the head Mac is on.")) from None
        try:
            return json.loads(payload or b"{}")
        except ValueError:
            raise RemoteError(t("헤드의 응답을 읽지 못했어요. 헤드의 omm 버전을 확인해 주세요.",
                                 "Could not read the head's reply. Check the omm version on the head.")) from None

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
        return t("헤드가 토큰을 받아 주지 않았어요. 관리자에게 새 토큰을 받아 `omm rag remote set`을 다시 실행해 주세요.",
                 "The head rejected the token. Get a new token from the administrator and run `omm rag remote set` again.")
    try:
        message = json.loads(payload)["error"]["message"]
    except (ValueError, KeyError, TypeError):
        message = None
    if isinstance(message, str) and message:
        return message
    if status == 404:
        return t("헤드가 이 기능을 모르는 것 같아요. 헤드의 omm을 업데이트해 주세요.",
                 "The head does not seem to support this. Update omm on the head.")
    return t(f"헤드가 요청을 처리하지 못했어요 (HTTP {status}).", f"The head could not handle the request (HTTP {status}).")


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
                    raise ValueError(t("비어 있거나 6 MiB를 넘는 문서는 등록할 수 없어요.",
                                     "Documents that are empty or larger than 6 MiB cannot be registered."))
                if progress is not None:
                    progress(path.name, "upload", 0, 0)
                results.append({"path": str(path), **client.upload(collection, path.name, path.read_bytes(), replace=replace)})
                continue
            except (ValueError, OSError) as error:
                problem = str(error) if isinstance(error, ValueError) else t("파일을 읽지 못했어요. 권한을 확인해 주세요.", "Could not read the file. Check permissions.")
        results.append({"path": str(path), "name": path.name, "error": problem})
    return {"collection": collection, "results": results, "skipped": skipped}
