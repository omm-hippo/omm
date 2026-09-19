"""HF_TOKEN header injection, scoped to the Hugging Face host only.

Mirrors the existing LM_API_TOKEN pattern (engines/lmstudio.py:35) instead of
adding storage: no OS keychain, no `omm auth` command group. See issue #358 -
the original design tried both and the storage half was cut back to this
narrower scope before merge. The token never leaves a huggingface.co host,
including across a download redirect - callers must recompute headers per
hop rather than reusing one header set across a redirect chain.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

_HF_HOSTS = {"huggingface.co", "hf.co"}


def _valid_token(token: object) -> str | None:
    if not isinstance(token, str):
        return None
    value = token.strip()
    if not value or len(value) > 4096 or any(character in value for character in "\r\n\0"):
        return None
    return value


def huggingface_token() -> str | None:
    return _valid_token(os.environ.get("HF_TOKEN"))


def huggingface_headers() -> dict[str, str]:
    token = huggingface_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


def headers_for_url(url: str) -> dict[str, str]:
    """Auth headers for `url`, or {} if it isn't a Hugging Face host.

    Intentionally re-derived from the URL on every call - a caller following
    redirects must call this again with the new URL rather than reusing one
    header set past a host change.
    """
    try:
        host = (urlsplit(url).hostname or "").casefold()
    except ValueError:
        return {}
    if host in _HF_HOSTS or host.endswith(".huggingface.co"):
        return huggingface_headers()
    return {}
