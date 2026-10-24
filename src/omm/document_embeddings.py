"""Local document embeddings through a loopback Ollama server.

Document text and questions are sent only to the loopback Ollama origin the
user configured (default ``http://127.0.0.1:11434``). Nothing here logs,
uploads or caches request bodies; errors never include response bodies.
"""
from __future__ import annotations

from array import array
import math
import sys

DEFAULT_MODEL = "bge-m3"
DEFAULT_URL = "http://127.0.0.1:11434"
BATCH = 16
# The bytes stored in SQLite are little-endian float32, independent of the host.
_SWAP = sys.byteorder != "little"


class EmbeddingUnavailable(Exception):
    """Semantic search cannot run; the message says why in plain Korean."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def pull_hint(model: str) -> str:
    return f"`ollama pull {model}`로 임베딩 모델을 받아 주세요."


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

    def available(self) -> bool:
        """True when the model is pulled; raises when the server is unreachable."""
        from omm.engines.base import RuntimeAdapterError
        try:
            rows = self._client.request("GET", "/api/tags", timeout=5, default_failure="server_unavailable").data.get("models")
        except RuntimeAdapterError:
            raise EmbeddingUnavailable("server_unavailable", self._server_message()) from None
        if not isinstance(rows, list):
            raise EmbeddingUnavailable("failed", "Ollama 모델 목록을 확인하지 못했어요.")
        return any(isinstance(row, dict) and any(isinstance(row.get(k), str) and _same_model(row[k], self.model) for k in ("name", "model"))
                   for row in rows)

    def _server_message(self) -> str:
        return f"Ollama({self.base_url})에 연결하지 못했어요. Ollama를 켠 뒤 다시 시도해 주세요."

    def embed(self, texts: list[str], *, keep_alive: str | int = 0) -> list[list[float]]:
        from omm.engines.base import RuntimeAdapterError
        vectors: list[list[float]] = []
        for offset in range(0, len(texts), BATCH):
            batch = texts[offset:offset + BATCH]
            last = offset + BATCH >= len(texts)
            payload = {"model": self.model, "input": batch, "truncate": True,
                       # Unload right after the final batch so a later answer
                       # does not find an unexpected model resident in memory.
                       "keep_alive": keep_alive if last else "1m"}
            try:
                data = self._client.request("POST", "/api/embed", payload=payload, timeout=300,
                                            default_failure="unknown").data
            except RuntimeAdapterError as error:
                if error.reason == "server_unavailable":
                    raise EmbeddingUnavailable("server_unavailable", self._server_message()) from None
                if error.reason == "model_not_visible" or "HTTP 404" in str(error):
                    raise EmbeddingUnavailable("model_missing", f"Ollama에 임베딩 모델 {self.model}이(가) 없어요. {pull_hint(self.model)}") from None
                raise EmbeddingUnavailable("failed", f"임베딩 모델 {self.model}로 의미 색인을 만들지 못했어요. 임베딩 전용 모델인지 확인해 주세요.") from None
            rows = data.get("embeddings")
            if not isinstance(rows, list) or len(rows) != len(batch):
                raise EmbeddingUnavailable("failed", "Ollama가 올바른 임베딩을 돌려주지 않았어요.")
            for row in rows:
                if not isinstance(row, list) or not row or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in row):
                    raise EmbeddingUnavailable("failed", "Ollama가 올바른 임베딩을 돌려주지 않았어요.")
                vectors.append(normalize(row))
        if vectors and len({len(v) for v in vectors}) != 1:
            raise EmbeddingUnavailable("failed", "임베딩 차원이 일정하지 않아요.")
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
    return math.fsum(x * y for x, y in zip(a, b))
