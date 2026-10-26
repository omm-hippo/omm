"""Talk to the model the cluster serves, for `omm cluster run`.

A Mac in the cluster talks to its own exo on loopback (no token). Any other
PC goes through the head's gateway with the address and employee token that
`omm rag remote set` saved. Requests go straight there: proxy environment
variables are ignored so the token never passes through a proxy.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Callable, Iterator
from urllib import error as urlerror, request as urlrequest

from omm.cluster import settings
from omm.cluster.commands import UsageProblem
from omm.cluster.errors import ClusterError

LOCAL_EXO = "http://127.0.0.1:52415"
LOADED_MODELS_PATH = "/omm/loaded-models"
LIST_TIMEOUT = 15
# Per socket read, not per answer: a long answer streams for minutes, but a
# 100+ GB model can take this long before its first token.
CHAT_TIMEOUT = 600
NO_REMOTE = "Set the cluster head and your token first: omm rag remote set <head address>"
BAD_TOKEN = ("The cluster head rejected your token. Ask the head Mac's admin for a new one "
             "(omm cluster token add <name>), then run: omm rag remote set <head address>")


@dataclass(frozen=True)
class Endpoint:
    url: str
    token: str | None  # None on a cluster Mac: its own exo needs no token

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
    return Endpoint(remote.url, remote.token)


def _open(point: Endpoint, method: str, path: str, body: dict | None, timeout: float):
    headers = {"Accept": "application/json"}
    if point.token is not None:
        headers["Authorization"] = f"Bearer {point.token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urlrequest.Request(point.url + path, data=data, headers=headers, method=method)
    opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))
    try:
        return opener.open(req, timeout=timeout)
    except urlerror.HTTPError as error:
        if error.code == 401:
            raise ClusterError(BAD_TOKEN) from error
        if error.code == 404 and path == LOADED_MODELS_PATH:
            raise ClusterError(f"{point.where} runs an older omm; on the head Mac run omm update, "
                               "then omm cluster init") from error
        if error.code == 502:
            raise ClusterError(f"exo is not running on {point.where}; check it with omm cluster status "
                               "on the head Mac") from error
        raise ClusterError(f"{point.where} answered HTTP {error.code}") from error
    except (urlerror.URLError, OSError) as error:
        if point.token is None:
            raise ClusterError("exo is not running on this Mac; check it with omm cluster status") from error
        raise ClusterError(f"could not reach the cluster head at {point.url}; check the address "
                           "(omm rag remote) and that you are on the office network") from error


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


def loaded_models(point: Endpoint) -> list[str]:
    return list(dict.fromkeys(row.model for row in loaded_rows(point)))


def pick_model(requested: str | None, loaded: list[str]) -> str:
    if not loaded:
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
