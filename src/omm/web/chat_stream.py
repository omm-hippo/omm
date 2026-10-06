"""Text-only local runtime streams. No tools, redirects, proxy or cloud fallback."""
from __future__ import annotations

import json
import time

from omm.engines.base import RuntimeAdapterError, require_loopback_base_url


class ChatCancelled(Exception):
    pass


def stream_reply(adapter, receipt, messages: list[dict], stop, on_chunk, on_connection):
    import requests
    client = adapter._client
    origin = require_loopback_base_url(client.base_url)
    options = receipt.load_options
    limit = min(1024, max(64, (options.context_length if options else 2048) // 2))
    if adapter.key == "ollama":
        payload = {"model": receipt.model.key, "messages": messages, "stream": True, "think": False,
                   "options": {**(options.ollama_options() if options else {}), "num_predict": limit, "temperature": 0.7}}
        if receipt.loaded_by_omm:
            payload["keep_alive"] = 120
        path = "/api/chat"
    else:
        payload = {"model": receipt.instance_id, "messages": messages, "stream": True,
                   "max_tokens": limit, "temperature": 0.7}
        path = "/v1/chat/completions"
    started = time.monotonic()
    with requests.Session() as session:
        session.trust_env = False
        response = None
        try:
            for attempt in range(2):
                if stop.is_set():
                    raise ChatCancelled()
                response = session.post(origin + path, json=payload, headers=dict(client._headers),
                                        stream=True, timeout=(5, 30), allow_redirects=False)
                on_connection(response)
                # Some Ollama versions reject think=False on non-thinking models.
                if adapter.key == "ollama" and attempt == 0 and response.status_code == 400:
                    response.close()
                    payload.pop("think", None)
                    continue
                break
            if response.status_code != 200:
                raise RuntimeAdapterError("unknown", f"로컬 실행 앱이 요청을 거부했어요 (HTTP {response.status_code}).")
            done = False
            finish_reason = None
            for raw in response.iter_lines(chunk_size=128):
                if stop.is_set():
                    raise ChatCancelled()
                if time.monotonic() - started > 300 or len(raw) > 65536:
                    raise RuntimeAdapterError("generation_timeout", "응답 시간이나 크기 제한에 도달했어요.")
                if not raw:
                    continue
                if adapter.key == "lmstudio":
                    if not raw.startswith(b"data:"):
                        continue
                    raw = raw[5:].strip()
                    if raw == b"[DONE]":
                        done = True
                        break
                try:
                    value = json.loads(raw)
                    if not isinstance(value, dict) or value.get("error"):
                        raise ValueError("invalid stream")
                    if adapter.key == "ollama":
                        text = (value.get("message") or {}).get("content", "")
                        done = value.get("done") is True
                        finish_reason = value.get("done_reason") if done else None
                    else:
                        choices = value.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0]
                        text = (choice.get("delta") or {}).get("content") or ""
                        finish_reason = choice.get("finish_reason")
                        done = finish_reason is not None
                    if not isinstance(text, str):
                        raise ValueError("invalid text")
                except (ValueError, TypeError, AttributeError, IndexError):
                    raise RuntimeAdapterError("unknown", "로컬 실행 앱의 응답 형식을 확인하지 못했어요.") from None
                if text:
                    on_chunk(text)
                if done:
                    break
            if stop.is_set():
                raise ChatCancelled()
            if not done:
                raise RuntimeAdapterError("unknown", "응답 연결이 끝났지만 완료를 확인하지 못했어요.")
            return finish_reason
        except requests.RequestException:
            if stop.is_set():
                raise ChatCancelled() from None
            raise RuntimeAdapterError("generation_timeout", "로컬 응답 연결이 끊겼거나 시간이 초과됐어요.") from None
        finally:
            if response is not None:
                response.close()
            on_connection(None)
