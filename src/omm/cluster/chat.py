"""Talk to the model the cluster serves, for `omm cluster run`.

A Mac in the cluster talks to its own exo on loopback (no token). Any other
PC goes through the head's gateway with the address and employee token that
`omm rag remote set` saved, over HTTPS that trusts only the head certificate
pinned there. Requests go straight there: proxy environment variables are
ignored so the token never passes through a proxy.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Callable, Iterator
from urllib import error as urlerror, request as urlrequest

from omm.cluster import settings
from omm.cluster.errors import ClusterError, UsageProblem

LOCAL_EXO = "http://127.0.0.1:52415"
LOADED_MODELS_PATH = "/omm/loaded-models"
LIST_TIMEOUT = 15
# Per socket read, not per answer: a long answer streams for minutes, but a
# 100+ GB model can take this long before its first token.
CHAT_TIMEOUT = 600
NO_REMOTE = "Set the cluster head and your token first: omm rag remote set <head address>"
BAD_TOKEN = ("The cluster head rejected your token. Ask the head Mac's admin for a new one "
             "(omm cluster token add <name>), then run: omm rag remote set <head address>")


WRONG_CERTIFICATE = ("The cluster head at {url} did not show the certificate pinned by omm rag remote set. "
                     "Ask the admin whether the head was set up again; if so, run omm rag remote clear, "
                     "then omm rag remote set {url} and compare the fingerprint")


@dataclass(frozen=True)
class Endpoint:
    url: str
    token: str | None  # None on a cluster Mac: its own exo needs no token
    cafile: str | None = None  # the head certificate `omm rag remote set` pinned

    @property
    def where(self) -> str:
        return "this Mac's cluster" if self.token is None else f"the cluster head at {self.url}"


def endpoint() -> Endpoint:
    if settings.load() is not None:
        return Endpoint(LOCAL_EXO, None)
    from omm import rag_remote

    try:
        remote = rag_remote.load()
    except rag_remote.RemoteError as error:
        raise UsageProblem(str(error)) from error
    if remote is None:
        raise UsageProblem(NO_REMOTE)
    try:
        rag_remote.ssl_context(remote)
    except rag_remote.RemoteError as error:
        raise UsageProblem(_english(str(error))) from error
    return Endpoint(remote.url, remote.token, str(rag_remote.ca_path()))


def _english(message: str) -> str:
    # rag_remote speaks Korean; the cluster commands speak English.
    from omm import rag_remote

    if message == rag_remote.PLAIN_HTTP:
        return ("The cluster gateway now accepts HTTPS only. Run: omm rag remote set https://<head address>:52440")
    if message == rag_remote.NOT_PINNED:
        return ("This PC has not checked the cluster head's certificate yet. Run: omm rag remote set "
                "https://<head address>:52440 and compare the fingerprint with the head's omm cluster status")
    return message


def _open(point: Endpoint, method: str, path: str, body: dict | None, timeout: float):
    headers = {"Accept": "application/json"}
    if point.token is not None:
        headers["Authorization"] = f"Bearer {point.token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urlrequest.Request(point.url + path, data=data, headers=headers, method=method)
    handlers = [urlrequest.ProxyHandler({})]
    if point.cafile is not None:
        from omm.cluster import tls

        handlers.append(urlrequest.HTTPSHandler(context=tls.client_context(point.cafile)))
    opener = urlrequest.build_opener(*handlers)
    try:
        return opener.open(req, timeout=timeout)
    except urlerror.HTTPError as error:
        if error.code == 401:
            raise ClusterError(BAD_TOKEN) from error
        if error.code == 400:
            raise ClusterError(_error_message(error) or f"{point.where} refused the request") from error
        if error.code == 404 and path.startswith("/omm/"):
            raise ClusterError(f"{point.where} runs an older omm; on the head Mac run omm update, "
                               "then omm cluster init") from error
        if error.code == 502:
            raise ClusterError(f"exo is not running on {point.where}; check it with omm cluster status "
                               "on the head Mac") from error
        raise ClusterError(f"{point.where} answered HTTP {error.code}") from error
    except (urlerror.URLError, OSError) as error:
        if point.token is None:
            raise ClusterError("exo is not running on this Mac; check it with omm cluster status") from error
        from omm.cluster import tls

        if tls.is_certificate_error(error):
            raise ClusterError(WRONG_CERTIFICATE.format(url=point.url)) from error
        raise ClusterError(f"could not reach the cluster head at {point.url}; check the address "
                           "(omm rag remote) and that you are on the office network") from error


def _error_message(error) -> str | None:
    # The gateway's own refusals carry {"error": {"message": "omm cluster: ..."}}.
    try:
        message = json.loads(error.read()).get("error", {}).get("message")
    except (ValueError, AttributeError, OSError):
        return None
    return message.removeprefix("omm cluster: ") if isinstance(message, str) and message else None


# The head previews every candidate before it can rank them (up to a minute each).
VIEW_TIMEOUT = 300


def view(point: Endpoint, path: str, timeout: float = VIEW_TIMEOUT, **query: str):
    """One of the head gateway's read-only views (gateway.VIEW_ROUTES) as parsed JSON."""
    from urllib.parse import urlencode

    target = f"{path}?{urlencode(query)}" if query else path
    with _open(point, "GET", target, None, timeout) as response:
        try:
            return json.loads(response.read())
        except ValueError as error:
            raise ClusterError(f"{point.where} sent an unreadable answer") from error


def cluster_configured() -> bool:
    """True on a cluster Mac or a PC with `omm rag remote set`; reads local files only."""
    if settings.load() is not None:
        return True
    from omm import rag_remote

    try:
        return rag_remote.load() is not None
    except rag_remote.RemoteError:
        return False


def loaded_rows(point: Endpoint, timeout: float | None = None):
    """The loaded models as models.ModelRow, from this Mac's exo or the head's gateway."""
    from omm.cluster.models import ModelRow, inventory_from

    if point.token is None:
        from omm.cluster.exo_client import ExoClient

        return [row for row in inventory_from(ExoClient(point.url).raw_state()) if row.state in ("loaded", "loading")]
    with _open(point, "GET", LOADED_MODELS_PATH, None, timeout or LIST_TIMEOUT) as response:
        try:
            data = json.loads(response.read())
            models = data.get("rows") if isinstance(data.get("rows"), list) else data.get("models")
        except (ValueError, AttributeError) as error:
            raise ClusterError(f"{point.where} sent an unreadable model list") from error
    rows = []
    for item in models if isinstance(models, list) else []:
        if isinstance(item, str):
            # A head on an older omm sends bare ids.
            rows.append(ModelRow(item, None, 0, 0, "loaded"))
        elif isinstance(item, dict) and isinstance(item.get("model"), str):
            size, have, total = (item.get(key) for key in ("size_bytes", "nodes_installed", "nodes_total"))
            rows.append(ModelRow(item["model"], size if isinstance(size, int) else None,
                                 have if isinstance(have, int) else 0, total if isinstance(total, int) else 0,
                                 str(item.get("state") or "loaded")))
    return rows


def loaded_models(point: Endpoint) -> tuple[list[str], list[str]]:
    """(ready, still loading) model ids."""
    rows = loaded_rows(point)
    ready = list(dict.fromkeys(row.model for row in rows if row.state == "loaded"))
    return ready, [model for model in dict.fromkeys(row.model for row in rows) if model not in ready]


def pick_model(requested: str | None, loaded: list[str], loading: list[str] = ()) -> str:
    if requested is not None and requested in loading:
        raise ClusterError(f"{requested} is still loading; try again in a minute (omm cluster list shows when "
                           "it is loaded)")
    if not loaded:
        if loading:
            raise ClusterError(f"{', '.join(loading)} is still loading; try again in a minute "
                               "(omm cluster list shows when it is loaded)")
        raise ClusterError("No model is loaded on the cluster. On the head Mac run: omm cluster load <model>")
    if requested is None:
        if len(loaded) == 1:
            return loaded[0]
        raise UsageProblem("The cluster serves several models; pick one:\n"
                           + "\n".join(f"  omm cluster run {model}" for model in loaded))
    if requested not in loaded:
        raise ClusterError(f"{requested} is not loaded on the cluster. Loaded now: {', '.join(loaded)}")
    return requested


def parse_stream(lines: Iterator[bytes]) -> Iterator[tuple[str, str]]:
    """Yield ("reasoning" | "content", text) pieces from an OpenAI-style SSE stream."""
    for raw in lines:
        line = raw.decode("utf-8", "replace").strip()
        # exo also sends ": keep-alive" and ": prefill_progress" comment lines.
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            return
        try:
            event = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if isinstance(event.get("error"), dict):
            raise ClusterError(f"the model stopped with an error: {event['error'].get('message') or 'unknown'}")
        for choice in event.get("choices") or []:
            delta = choice.get("delta") if isinstance(choice, dict) else None
            if not isinstance(delta, dict):
                continue
            for kind, key in (("reasoning", "reasoning_content"), ("content", "content")):
                text = delta.get(key)
                if isinstance(text, str) and text:
                    yield kind, text


def reply(point: Endpoint, model: str, messages: list[dict], on_piece: Callable[[str, str], None]) -> str:
    """Stream one answer, calling on_piece(kind, text); return the answer without its reasoning."""
    body = {"model": model, "messages": messages, "stream": True}
    answer: list[str] = []
    with _open(point, "POST", "/v1/chat/completions", body, CHAT_TIMEOUT) as response:
        for kind, text in parse_stream(iter(response.readline, b"")):
            if kind == "content":
                if not answer:
                    # Qwen-style models start the answer with the space or
                    # newlines that ended their reasoning.
                    text = text.lstrip()
                    if not text:
                        continue
                answer.append(text)
            on_piece(kind, text)
    return "".join(answer)
