"""Local document embeddings through a loopback Ollama server.

Document text and questions are sent only to the loopback Ollama origin the
user configured (default ``http://127.0.0.1:11434``). Nothing here logs,
uploads or caches request bodies; errors never include response bodies.
"""
from __future__ import annotations

from array import array
import math
import re
import sys

from omm.lang import t

DEFAULT_MODEL = "bge-m3"
DEFAULT_URL = "http://127.0.0.1:11434"
BATCH = 16
# How long Ollama keeps the embedding model in memory after a request. A warm
# model answers a question in well under a second; a cold one first spends
# seconds loading. "0" unloads right after every request.
DEFAULT_KEEP_ALIVE = "5m"
_KEEP_ALIVE = re.compile(r"0|[1-9][0-9]{0,3}[smh]")
# The bytes stored in SQLite are little-endian float32, independent of the host.
_SWAP = sys.byteorder != "little"


class EmbeddingUnavailable(Exception):
    """Semantic search cannot run; the message says why in plain Korean."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def keep_alive_value(text: str) -> str | int:
    """Validated keep-alive for Ollama: 0 (unload at once) or e.g. "30s", "5m", "1h"."""
    if not isinstance(text, str) or not _KEEP_ALIVE.fullmatch(text.strip()):
        raise ValueError(t("유지 시간은 0(바로 내리기) 또는 30s, 5m, 1h처럼 숫자와 s/m/h로 적어 주세요.",
                           "Keep-alive must be 0 (unload at once) or a number with s/m/h, like 30s, 5m or 1h."))
    text = text.strip()
    return 0 if text == "0" else text


def pull_hint(model: str) -> str:
    return t(f"`ollama pull {model}`로 임베딩 모델을 받아 주세요.", f"Pull the embedding model with `ollama pull {model}`.")


def _same_model(name: str, model: str) -> bool:
    def base(value: str) -> str:
        return value.removesuffix(":latest")
    return base(name) == base(model)


class OllamaEmbedder:
    def __init__(self, model: str, base_url: str = DEFAULT_URL):
        from omm.engines.base import LoopbackJsonClient
        self.model = model
        self.base_url = base_url
        self._client = LoopbackJsonClient(base_url)

    def available(self, *, timeout: float = 5) -> bool:
        """True when the model is pulled; raises when the server is unreachable."""
        from omm.engines.base import RuntimeAdapterError
        try:
            rows = self._client.request("GET", "/api/tags", timeout=timeout, default_failure="server_unavailable").data.get("models")
        except RuntimeAdapterError:
            raise EmbeddingUnavailable("server_unavailable", self._server_message()) from None
        if not isinstance(rows, list):
            raise EmbeddingUnavailable("failed", t("Ollama 모델 목록을 확인하지 못했어요.", "Could not read the Ollama model list."))
        return any(isinstance(row, dict) and any(isinstance(row.get(k), str) and _same_model(row[k], self.model) for k in ("name", "model"))
                   for row in rows)

    def _server_message(self) -> str:
        return t(f"Ollama({self.base_url})에 연결하지 못했어요. Ollama를 켠 뒤 다시 시도해 주세요.",
                 f"Could not connect to Ollama ({self.base_url}). Start Ollama and try again.")

    def embed(self, texts: list[str], *, keep_alive: str | int = DEFAULT_KEEP_ALIVE, progress=None) -> list[list[float]]:
        """Normalized vectors for ``texts``; ``progress(done, total)`` runs once before the first batch and after each one."""
        from omm.engines.base import RuntimeAdapterError
        vectors: list[list[float]] = []
        if progress is not None:
            progress(0, len(texts))
        for offset in range(0, len(texts), BATCH):
            batch = texts[offset:offset + BATCH]
            last = offset + BATCH >= len(texts)
            payload = {"model": self.model, "input": batch, "truncate": True,
                       # With keep_alive 0 the model still stays between the
                       # batches of one request, then unloads after the last.
                       "keep_alive": keep_alive if last or keep_alive != 0 else "1m"}
            try:
                data = self._client.request("POST", "/api/embed", payload=payload, timeout=300,
                                            default_failure="unknown").data
            except RuntimeAdapterError as error:
                if error.reason == "server_unavailable":
                    raise EmbeddingUnavailable("server_unavailable", self._server_message()) from None
                if error.reason == "model_not_visible" or "HTTP 404" in str(error):
                    raise EmbeddingUnavailable("model_missing", t(f"Ollama에 임베딩 모델 {self.model}이(가) 없어요. {pull_hint(self.model)}",
                                                                     f"Ollama does not have the embedding model {self.model}. {pull_hint(self.model)}")) from None
                raise EmbeddingUnavailable("failed", t(f"임베딩 모델 {self.model}로 의미 색인을 만들지 못했어요. 임베딩 전용 모델인지 확인해 주세요.",
                                                       f"Could not build the semantic index with {self.model}. Check that it is an embedding model.")) from None
            rows = data.get("embeddings")
            if not isinstance(rows, list) or len(rows) != len(batch):
                raise EmbeddingUnavailable("failed", t("Ollama가 올바른 임베딩을 돌려주지 않았어요.", "Ollama did not return valid embeddings."))
            for row in rows:
                if not isinstance(row, list) or not row or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in row):
                    raise EmbeddingUnavailable("failed", t("Ollama가 올바른 임베딩을 돌려주지 않았어요.", "Ollama did not return valid embeddings."))
                vectors.append(normalize(row))
            if progress is not None:
                progress(len(vectors), len(texts))
        if vectors and len({len(v) for v in vectors}) != 1:
            raise EmbeddingUnavailable("failed", t("임베딩 차원이 일정하지 않아요.", "The embedding dimensions are inconsistent."))
        return vectors


def normalize(vector) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    if not norm:
        return [0.0] * len(vector)
    return [x / norm for x in vector]


def pack(vector) -> bytes:
    values = array("f", vector)
    if _SWAP:
        values.byteswap()
    return values.tobytes()


def unpack(blob: bytes) -> array:
    values = array("f")
    values.frombytes(blob)
    if _SWAP:
        values.byteswap()
    return values


def dot(a, b) -> float:
    # Vectors are unit length, so plain float accumulation is precise enough;
    # math.sumprod (3.12+) runs in C, about four times faster per chunk.
    if _SUMPROD is not None:
        return _SUMPROD(a, b)
    return math.fsum(x * y for x, y in zip(a, b))


_SUMPROD = getattr(math, "sumprod", None)
