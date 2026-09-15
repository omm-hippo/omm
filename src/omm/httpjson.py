"""Bounded reading of JSON HTTP responses.

Every caller here talks to a provider or catalog endpoint that is not fully
trusted (rate limits, misconfigured mirrors, a slow/hostile server): a bare
`resp.json()` after a plain `requests.get(...)` buffers the entire response
body in memory before any size check can run, and a `timeout=` alone only
bounds socket idle time, not a slow drip of bytes or a large declared
Content-Length. Callers must pass `stream=True` to `requests.get`/`post` so
the body is read here, chunk by chunk, under an explicit byte ceiling.

Moved verbatim (no logic changes) from predictor.py's former
`_bounded_response_bytes` / `_read_bounded_json_response` so every provider,
search, and rules call site can share one implementation instead of each
re-deriving its own bound.
"""

from __future__ import annotations

import json

MAX_PROVIDER_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_RULES_RESPONSE_BYTES = 1 * 1024 * 1024

_RESPONSE_CHUNK_BYTES = 64 * 1024


def bounded_response_bytes(response, *, maximum: int, label: str) -> bytes:
    """Read an HTTP response without trusting Content-Length or JSON shape."""
    headers = getattr(response, "headers", {})
    raw_length = headers.get("Content-Length") if hasattr(headers, "get") else None
    try:
        declared_length = int(raw_length)
    except (TypeError, ValueError):
        declared_length = None
    if declared_length is not None and declared_length > maximum:
        raise ValueError(f"{label} exceeds the {maximum}-byte safety limit")

    iterator = getattr(response, "iter_content", None)
    if callable(iterator):
        content = bytearray()
        for chunk in iterator(chunk_size=_RESPONSE_CHUNK_BYTES):
            if not isinstance(chunk, bytes):
                raise ValueError(f"{label} response contained non-byte data")
            if len(content) + len(chunk) > maximum:
                raise ValueError(f"{label} exceeds the {maximum}-byte safety limit")
            content.extend(chunk)
        return bytes(content)

    raw_content = getattr(response, "content", None)
    if isinstance(raw_content, bytes):
        if len(raw_content) > maximum:
            raise ValueError(f"{label} exceeds the {maximum}-byte safety limit")
        return raw_content

    raise ValueError(f"{label} response did not provide byte content")


def read_bounded_json_response(response, *, maximum: int, label: str) -> tuple[object, bytes]:
    try:
        content = bounded_response_bytes(response, maximum=maximum, label=label)
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    if len(content) > maximum:
        # Covers json()-only compatibility objects used above.
        raise ValueError(f"{label} exceeds the {maximum}-byte safety limit")
    try:
        return json.loads(content), content
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError(f"{label} is not valid JSON") from error
