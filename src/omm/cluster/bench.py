"""Time one streamed answer from the cluster, for `omm cluster benchmark`.

exo ends a stream with a ": generation_stats" comment holding its own
prompt and generation speeds (exo 1.0.71); those are used when present,
else the OpenAI `usage` count over omm's own clock. Results stay local:
cluster numbers have no place in the single-machine recommendation data.
"""

from __future__ import annotations

import json
import time

from omm.cluster import chat
from omm.cluster.errors import ClusterError

PROMPT = "Explain in about 200 words how a refrigerator keeps food cold."
MAX_TOKENS = 256
_STATS = ": generation_stats"


def measure(point: chat.Endpoint, model: str, prompt: str = PROMPT, clock=time.monotonic) -> dict:
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "stream": True,
            "max_tokens": MAX_TOKENS, "stream_options": {"include_usage": True}}
    started = clock()
    first = last = None
    pieces = 0
    usage_tokens = None
    stats: dict = {}
    with chat._open(point, "POST", "/v1/chat/completions", body, chat.CHAT_TIMEOUT) as response:
        for raw in iter(response.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith(_STATS):
                try:
                    stats = json.loads(line[len(_STATS):].strip())
                except ValueError:
                    stats = {}
                continue
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                event = json.loads(payload)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if isinstance(event.get("error"), dict):
                raise ClusterError(f"the model stopped with an error: {event['error'].get('message') or 'unknown'}")
            usage = event.get("usage")
            if isinstance(usage, dict) and isinstance(usage.get("completion_tokens"), int):
                usage_tokens = usage["completion_tokens"]
            for choice in event.get("choices") or []:
                delta = choice.get("delta") if isinstance(choice, dict) else None
                if isinstance(delta, dict) and any(isinstance(delta.get(k), str) and delta.get(k)
                                                   for k in ("content", "reasoning_content")):
                    now = clock()
                    first = first if first is not None else now
                    last = now
                    pieces += 1
    if first is None:
        raise ClusterError(f"{model} sent no text back")
    ttft = first - started
    generation = stats.get("generation_tps")
    if isinstance(generation, (int, float)) and generation > 0:
        tokens = stats.get("generation_tokens")
        prompt_tps = stats.get("prompt_tps")
        return {"model": model, "ttft_seconds": ttft, "decode_tokens_per_second": float(generation),
                "prompt_tokens_per_second": float(prompt_tps) if isinstance(prompt_tps, (int, float)) else None,
                "output_tokens": tokens if isinstance(tokens, int) else usage_tokens or pieces, "source": "exo"}
    tokens = usage_tokens if usage_tokens is not None else pieces
    elapsed = (last - first) if last is not None and last > first else 0.0
    # The first token arrives at `first`, so the decode window covers the rest.
    decode = (tokens - 1) / elapsed if elapsed > 0 and tokens > 1 else None
    return {"model": model, "ttft_seconds": ttft, "decode_tokens_per_second": decode,
            "prompt_tokens_per_second": None, "output_tokens": tokens,
            "source": "usage" if usage_tokens is not None else "pieces"}
